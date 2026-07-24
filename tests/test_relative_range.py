#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tests for relative range commands - "сделай теплее" and "сделай холоднее".

Yandex sends such a command as an increment with "relative": true, and before
the fix that increment was published as the new value (so "сделай теплее" set
the thermostat to 1). The tests drive the whole action handler, not just the
arithmetic, and stub only the MQTT boundary - the publish and the read of the
current value - so no broker is needed.
"""

import json

import pytest

from wb.mqtt_alice.client import device_registry
from wb.mqtt_alice.client.device_registry import DeviceRegistry
from wb.mqtt_alice.client.sio_alice_handlers import SioAliceHandlers

CAP_RANGE = "devices.capabilities.range"
DEVICE_ID = "ac-test1"
TEMPERATURE_TOPIC = "/devices/AC-test1/controls/temperature"

# Air conditioner with a temperature scale of 16..30 and a step of 1,
# in the shape the webui configurator writes it
DEVICES_CONFIG = {
    "rooms": {},
    "devices": {
        DEVICE_ID: {
            "name": "Кондиционер",
            "room_id": "",
            "type": "devices.types.thermostat.ac",
            "capabilities": [
                {
                    "type": CAP_RANGE,
                    "mqtt": TEMPERATURE_TOPIC,
                    "parameters": {
                        "instance": "temperature",
                        "range": {"min": 16, "max": 30, "precision": 1},
                    },
                }
            ],
            "properties": [],
        }
    },
}


class AliceThermostat:
    """
    Drives one action through the real handler and registry, with the MQTT
    publish and the current-value read replaced by in-memory stubs.
    """

    def __init__(self):
        self.handlers = None  # set by the fixture once the registry is built
        self.published = []  # (topic, payload) of everything the client publishes
        self.current_temperature = "18"  # what the temperature control reports now
        self.last_result = None  # action_result of the last command sent

    def given_current_temperature(self, celsius):
        # What the device reports now - a relative step is added to this
        self.current_temperature = str(celsius)

    async def adjust_temperature(self, delta):
        # Relative command, the way Yandex sends "сделай теплее/холоднее"
        await self._send_action({"instance": "temperature", "value": delta, "relative": True})

    async def set_temperature(self, target):
        # Absolute command, the way Yandex sends "установи N градусов"
        await self._send_action({"instance": "temperature", "value": target})

    async def _send_action(self, state):
        device = {"id": DEVICE_ID, "capabilities": [{"type": CAP_RANGE, "state": state}]}
        response = await self.handlers._handle_single_device_action(device)
        self.last_result = response["capabilities"][0]["state"]["action_result"]

    def assert_temperature_set_to(self, celsius):
        assert self.published == [(f"{TEMPERATURE_TOPIC}/on", str(celsius))]
        assert self.last_result == {"status": "DONE"}

    def assert_rejected_as_out_of_range(self):
        # Nothing is published, and Yandex is told the value was invalid
        assert self.published == []
        assert self.last_result == {"status": "ERROR", "error_code": "INVALID_VALUE"}


@pytest.fixture
def thermostat(tmp_path, monkeypatch):
    devices_conf = tmp_path / "devices.conf"
    devices_conf.write_text(json.dumps(DEVICES_CONFIG, ensure_ascii=False), encoding="utf-8")

    # Event rates are not exercised here, but the registry reads the file on
    # load - keep the test self-contained instead of pointing it at configs/
    event_rates_conf = tmp_path / "event-rates.json"
    event_rates_conf.write_text("{}", encoding="utf-8")

    harness = AliceThermostat()

    async def fake_publish(topic, payload):
        harness.published.append((topic, payload))

    async def fake_read(topic, **kwargs):
        return harness.current_temperature

    monkeypatch.setattr(device_registry, "read_retained_value", fake_read)

    registry = DeviceRegistry(
        str(devices_conf),
        send_to_yandex=lambda *args, **kwargs: None,
        publish_to_mqtt=fake_publish,
        cfg_events_path=str(event_rates_conf),
    )
    harness.handlers = SioAliceHandlers(registry=registry, controller_sn="TEST-SN")
    return harness


async def test_warmer_adds_one_degree_to_the_current_temperature(thermostat):
    thermostat.given_current_temperature(18)
    await thermostat.adjust_temperature(+1)
    thermostat.assert_temperature_set_to(19)


async def test_cooler_subtracts_one_degree_from_the_current_temperature(thermostat):
    thermostat.given_current_temperature(18)
    await thermostat.adjust_temperature(-1)
    thermostat.assert_temperature_set_to(17)


async def test_absolute_command_sets_the_value_directly(thermostat):
    await thermostat.set_temperature(20)
    thermostat.assert_temperature_set_to(20)


async def test_absolute_value_above_the_maximum_is_rejected(thermostat):
    await thermostat.set_temperature(35)  # scale tops out at 30
    thermostat.assert_rejected_as_out_of_range()


async def test_relative_step_past_the_maximum_is_rejected(thermostat):
    thermostat.given_current_temperature(25)
    await thermostat.adjust_temperature(+10)  # 25 + 10 = 35, past the max of 30
    thermostat.assert_rejected_as_out_of_range()
