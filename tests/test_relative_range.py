#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tests for relative range commands ("сделай теплее")

Yandex sends an increment with "relative": true, and before the fix it was
published as the new value - so the test drives the whole action handler,
not just the arithmetic. Two things are stubbed: the MQTT publish and the
read of the current value, so no broker is needed
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wb.mqtt_alice.client import device_registry
from wb.mqtt_alice.client.device_registry import DeviceRegistry
from wb.mqtt_alice.client.sio_alice_handlers import SioAliceHandlers

EVENT_RATES_PATH = Path(__file__).resolve().parents[1] / "configs" / "wb-mqtt-alice-event-rates.json"

CAP_RANGE = "devices.capabilities.range"
DEVICE_ID = "ac-test1"
TOPIC = "/devices/AC-test1/controls/temperature"

# Air conditioner with a temperature scale of 16..30 and a step of 1,
# as the webui configurator writes it
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
                    "mqtt": TOPIC,
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


class RelativeRangeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        config_path = Path(config_dir.name) / "devices.conf"
        config_path.write_text(json.dumps(DEVICES_CONFIG, ensure_ascii=False), encoding="utf-8")

        self.published = []  # (topic, payload) of everything the client publishes
        self.current_value = "18"  # what the temperature control reports now

        async def fake_publish(topic, payload):
            self.published.append((topic, payload))

        async def fake_read(topic, **kwargs):
            return self.current_value

        patcher = mock.patch.object(device_registry, "read_retained_value", fake_read)
        patcher.start()
        self.addCleanup(patcher.stop)

        registry = DeviceRegistry(
            str(config_path),
            send_to_yandex=lambda *args, **kwargs: None,
            publish_to_mqtt=fake_publish,
            cfg_events_path=str(EVENT_RATES_PATH),
        )
        self.handlers = SioAliceHandlers(registry=registry, controller_sn="TEST-SN")

    async def send_action(self, value, relative=None):
        """Send one capability action the way Yandex sends it"""
        state = {"instance": "temperature", "value": value}
        if relative is not None:
            state["relative"] = relative
        device = {"id": DEVICE_ID, "capabilities": [{"type": CAP_RANGE, "state": state}]}
        return await self.handlers._handle_single_device_action(device)

    async def test_warmer_adds_the_increment_to_the_current_value(self):
        """Command "сделай теплее" at 18 must set 19, not the increment itself"""
        self.current_value = "18"

        await self.send_action(1, relative=True)

        self.assertEqual(self.published, [(f"{TOPIC}/on", "19")])

    async def test_cooler_subtracts_the_increment_from_the_current_value(self):
        """Command "сделай холоднее" at 18 must set 17"""
        self.current_value = "18"

        await self.send_action(-1, relative=True)

        self.assertEqual(self.published, [(f"{TOPIC}/on", "17")])

    async def test_warmer_stops_at_the_top_of_the_scale(self):
        """A step up at the declared maximum stays at the maximum"""
        self.current_value = "30"

        await self.send_action(1, relative=True)

        self.assertEqual(self.published, [(f"{TOPIC}/on", "30")])

    async def test_value_without_the_relative_flag_is_published_as_is(self):
        """Command "установи 20" worked before the fix and must keep working"""
        await self.send_action(20)

        self.assertEqual(self.published, [(f"{TOPIC}/on", "20")])


if __name__ == "__main__":
    unittest.main()
