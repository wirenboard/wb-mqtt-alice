"""Tests for client startup, exit codes, and connection recovery."""

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from wb.mqtt_alice.client import main as client


def write_configs(tmp_path: Path, *, enabled: bool = False) -> tuple[Path, Path, Path]:
    """
    Write the three configurations required by the client.
    """
    server_config = tmp_path / "server.conf"
    server_config.write_text(json.dumps({"server_address": "alice.example"}), encoding="utf-8")
    client_config = tmp_path / "client.conf"
    client_config.write_text(json.dumps({"client_enabled": enabled}), encoding="utf-8")
    devices_config = tmp_path / "devices.conf"
    devices_config.write_text(json.dumps({"rooms": {}, "devices": {}}), encoding="utf-8")
    return server_config, client_config, devices_config


async def test_disabled_integration_exits_with_code_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A valid disabled integration has no work and must not be reported as failed.
    """
    server_config, client_config, devices_config = write_configs(tmp_path)
    monkeypatch.setattr(client, "SERVER_CONFIG_PATH", str(server_config))
    monkeypatch.setattr(client, "CLIENT_CONFIG_PATH", str(client_config))
    monkeypatch.setattr(client, "DEVICE_PATH", str(devices_config))

    assert await client.main() == 7


async def test_invalid_configuration_exits_with_code_6(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Malformed user configuration is a non-restartable configuration error.
    """
    server_config, client_config, devices_config = write_configs(tmp_path)
    client_config.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(client, "SERVER_CONFIG_PATH", str(server_config))
    monkeypatch.setattr(client, "CLIENT_CONFIG_PATH", str(client_config))
    monkeypatch.setattr(client, "DEVICE_PATH", str(devices_config))

    assert await client.main() == 6


async def test_mqtt_reconnect_restores_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A reconnect while Socket.IO is active must restore clean-session subscriptions.
    """
    context = client.AppContext()
    context.main_loop = asyncio.get_running_loop()
    context.mqtt_connected_event = asyncio.Event()
    context.sio_manager = Mock()
    context.sio_manager.is_connected.return_value = True
    context.sio_handlers = Mock()
    monkeypatch.setattr(client, "ctx", context)

    client.mqtt_on_connect(None, None, {}, 0)
    await asyncio.sleep(0)

    assert context.mqtt_connected_event.is_set()
    context.sio_handlers.subscribe_registry_topics.assert_called_once_with()


async def test_mqtt_authentication_failure_exits_with_code_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Authentication refusal is not retried and is reported as an invalid argument.
    """
    context = client.AppContext()
    context.main_loop = asyncio.get_running_loop()
    context.stop_event = asyncio.Event()
    context.mqtt_connected_event = asyncio.Event()
    context.mqtt_auth_failed_event = asyncio.Event()
    context.mqtt_failed_event = asyncio.Event()
    context.mqtt_client = Mock()
    context.mqtt_client.connect.return_value = client.mqtt_client.MQTT_ERR_SUCCESS
    context.mqtt_client.loop_start.side_effect = lambda: client.mqtt_on_connect(None, None, {}, 5)
    monkeypatch.setattr(client, "ctx", context)

    assert await client.wait_for_mqtt() == 2


async def test_signal_interrupts_initial_cloud_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Shutdown must not wait for an indefinitely unavailable Alice cloud.
    """
    context = client.AppContext()
    context.stop_event = asyncio.Event()
    context.mqtt_auth_failed_event = asyncio.Event()
    context.mqtt_failed_event = asyncio.Event()
    monkeypatch.setattr(client, "ctx", context)

    async def connect_forever(server_address: str, reconnection_interval_min: int) -> None:
        del server_address, reconnection_interval_min
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "connect_controller", connect_forever)
    task = asyncio.create_task(client.wait_for_controller("alice.example", 20))
    await asyncio.sleep(0)
    context.stop_event.set()

    assert await asyncio.wait_for(task, timeout=1) == 7
