#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

"""
Exit codes and the MQTT lifecycle of the client: missing configs end with 6, a disabled client
with 7, a rejected MQTT login with 2, a stop signal with 0 even while the server connection is
still being retried; a reconnect restores the subscriptions. Only the config reads, the device
registry, the MQTT client and the loop's signal handler registration are stubbed.
"""

import asyncio
import functools
import logging
import runpy
import signal
import sys
from unittest.mock import MagicMock

import pytest

from wb.mqtt_alice.client import main as client_main

SERVER_CFG = {"server_address": "alice.example.org"}
CLIENT_CFG = {"client_enabled": True, "log_level": "INFO"}


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch):
    monkeypatch.setattr(client_main, "ctx", client_main.AppContext())
    monkeypatch.setattr(client_main, "get_client_pkg_ver", lambda: "test")
    monkeypatch.setattr(client_main, "DeviceRegistry", MagicMock())


@pytest.fixture(autouse=True)
def signal_handlers(monkeypatch):
    """
    Handlers main() registers on its loop, by signal number. Registration is recorded instead of
    performed, so no real handler and no real signal ever touch the pytest process; a test stops
    the client with _deliver(), which runs the SIGTERM handler once main() has registered it.
    """
    handlers = {}

    def record(_loop, signum, callback, *args):
        handlers[signum] = functools.partial(callback, *args)

    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", record)
    return handlers


def _deliver(handlers, signum):
    """
    What the kernel does when the signal arrives: runs the handler main() registered for it, and
    fails with KeyError if main() has not registered one yet.
    """
    handlers[signum]()


def _configs(monkeypatch, server=None, client=None):
    by_path = {
        client_main.SERVER_CONFIG_PATH: SERVER_CFG if server is None else server,
        client_main.CLIENT_CONFIG_PATH: CLIENT_CFG if client is None else client,
    }
    monkeypatch.setattr(client_main, "read_config", by_path.get)


def _fake_mqtt(monkeypatch, connack):
    """
    An MQTT client whose start delivers the given CONNACK the way paho's thread would
    """
    fake = MagicMock()
    fake.loop_start.side_effect = lambda: client_main.mqtt_on_connect(fake, None, {}, connack)
    monkeypatch.setattr(client_main, "setup_mqtt_client", MagicMock(return_value=fake))
    return fake


@pytest.mark.parametrize(
    "missing_path, present_content",
    [
        (client_main.SERVER_CONFIG_PATH, None),
        (client_main.SERVER_CONFIG_PATH, {}),
        (client_main.CLIENT_CONFIG_PATH, None),
    ],
    ids=["no-server-config", "no-server-address", "no-client-config"],
)
async def test_missing_or_broken_configs_exit_with_6(monkeypatch, missing_path, present_content):
    by_path = {client_main.SERVER_CONFIG_PATH: SERVER_CFG, client_main.CLIENT_CONFIG_PATH: CLIENT_CFG}
    by_path[missing_path] = present_content
    monkeypatch.setattr(client_main, "read_config", by_path.get)
    assert await client_main.main() == client_main.EXIT_NOTCONFIGURED


async def test_disabled_client_exits_with_7(monkeypatch):
    _configs(monkeypatch, client={"client_enabled": False})
    assert await client_main.main() == client_main.EXIT_NOTRUNNING


async def test_broken_devices_config_exits_with_6(monkeypatch):
    _configs(monkeypatch)
    monkeypatch.setattr(client_main, "DeviceRegistry", MagicMock(side_effect=ValueError("bad json")))
    assert await client_main.main() == client_main.EXIT_NOTCONFIGURED


@pytest.mark.parametrize("connack", client_main.MQTT_AUTH_ERRORS)
async def test_rejected_mqtt_login_exits_with_2(monkeypatch, connack):
    _configs(monkeypatch)
    fake = _fake_mqtt(monkeypatch, connack)

    assert await client_main.main() == client_main.EXIT_INVALIDARGUMENT

    fake.connect_async.assert_called_once_with(
        client_main.MQTT_HOST, client_main.MQTT_PORT, client_main.MQTT_KEEPALIVE
    )
    fake.loop_stop.assert_called_once_with()


async def test_signal_while_waiting_for_the_broker_exits_with_0(monkeypatch, signal_handlers):
    _configs(monkeypatch)
    fake = MagicMock()  # never answers: the broker is down, paho keeps retrying
    monkeypatch.setattr(client_main, "setup_mqtt_client", MagicMock(return_value=fake))
    asyncio.get_running_loop().call_later(0.05, _deliver, signal_handlers, signal.SIGTERM)

    assert await asyncio.wait_for(client_main.main(), timeout=2) == client_main.EXIT_SUCCESS
    fake.loop_stop.assert_called_once_with()


async def test_signal_while_connecting_to_the_server_exits_with_0(monkeypatch, signal_handlers):
    _configs(monkeypatch)
    fake = _fake_mqtt(monkeypatch, 0)
    attempted = asyncio.Event()

    async def connect_forever(_server_address):
        attempted.set()
        await asyncio.Event().wait()  # the controller is not linked: no attempt ever succeeds

    monkeypatch.setattr(client_main, "connect_controller", connect_forever)
    asyncio.get_running_loop().call_later(0.05, _deliver, signal_handlers, signal.SIGTERM)

    assert await asyncio.wait_for(client_main.main(), timeout=2) == client_main.EXIT_SUCCESS
    assert attempted.is_set()
    fake.loop_stop.assert_called_once_with()


@pytest.mark.parametrize(
    "code, expected",
    [
        (client_main.EXIT_INVALIDARGUMENT, "will not be restarted"),
        (client_main.EXIT_NOTCONFIGURED, "will not be restarted"),
        (client_main.EXIT_NOTRUNNING, "nothing to do"),
        (1, "systemd will restart"),
    ],
    ids=["rejected-login", "broken-config", "disabled", "crash"],
)
def test_exit_message_matches_the_unit_restart_policy(monkeypatch, caplog, code, expected):
    """
    The `__main__` block logs what systemd does next with the code main() returned: 2 and 6 are
    RestartPreventExitStatus in the unit, 7 is a SuccessExitStatus, anything else is restarted. The
    module runs as `__main__` through runpy with asyncio.run replaced, so no loop, signal or broker.
    """

    def return_code(coro, **_kwargs):
        coro.close()  # main() itself is not under test here
        return code

    monkeypatch.setattr(asyncio, "run", return_code)
    monkeypatch.setattr(logging, "basicConfig", lambda **_kwargs: None)  # keeps caplog's handler
    monkeypatch.delitem(sys.modules, client_main.__name__)  # runpy warns about an imported module
    caplog.set_level(logging.INFO)

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("wb.mqtt_alice.client.main", run_name="__main__")

    assert exc.value.code == code
    assert expected in caplog.text


async def test_reconnect_restores_the_registry_subscriptions():
    ctx = client_main.ctx
    ctx.main_loop = asyncio.get_running_loop()
    ctx.mqtt_connected = asyncio.Event()
    ctx.sio_handlers = MagicMock()
    ctx.time_rate_sender = MagicMock(running=True)

    client_main.mqtt_on_connect(None, None, {}, 0)
    await asyncio.sleep(0)

    ctx.sio_handlers.subscribe_registry_topics.assert_called_once_with()
    assert ctx.mqtt_connected.is_set()
