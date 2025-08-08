#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Device Registry Module for Wiren Board Alice Integration
Handles device configuration, MQTT-Yandex routing
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import paho.mqtt.subscribe as subscribe

from mqtt_topic import MQTTTopic

logger = logging.getLogger(__name__)


async def read_topic_once(
    topic: str, *, host: str = "localhost", retain: bool = True, timeout: float = 2.0
) -> Optional[Any]:
    """
    Reads a single retained MQTT message in a separate thread
    Returns paho.mqtt.client.MQTTMessage or None on timeout
    """
    logger.debug(
        "Read topic wait %s message on '%s' (retain=%s, %.1fs)",
        "retained" if retain else "live",
        topic,
        retain,
        timeout,
    )

    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(
                subscribe.simple, topic, hostname=host, retained=retain, msg_count=1
            ),
            timeout=timeout,
        )
        logger.debug("Current topic '%s' state: '%s'", topic, res)
        return res
    except asyncio.TimeoutError:
        logger.warning("Read topic timeout waiting '%s'", topic)
        return None


async def read_mqtt_state(
    topic: str, mqtt_host="localhost", timeout=1
) -> Optional[bool]:
    """
    Reads the value of a topic (0/1, "false"/"true", etc.) and returns a Python bool
    Uses subscribe.simple(...) from paho.mqtt, which BLOCKS for the duration of reading
    """
    logger.debug("Read MQTT state trying to read topic: %s", topic)

    try:
        msg = await read_topic_once(topic, timeout=timeout)
        if msg is None:
            logger.debug("Not find retained payload in '%s'", topic)
            return None
    except Exception as e:
        logger.warning("Failed to read topic '%s': %s", topic, e)
        return None

    payload_str = msg.payload.decode().strip().lower()

    # Interpret different payload variants
    if payload_str in {"1", "true", "on"}:
        return True
    elif payload_str in {"0", "false", "off"}:
        return False
    else:
        logger.warning("Unexpected payload in topic '%s': %s", topic, payload_str)
        return False


class DeviceRegistry:
    """
    Parses WB config and routes MQTT to Yandex
    """

    def __init__(
        self,
        cfg_path: str,
        *,
        send_to_yandex: Callable[[str, str, Optional[str], Any], None],
        publish_to_mqtt: Callable[[str, str], None],
    ) -> None:
        self._send_to_yandex = send_to_yandex
        self._publish_to_mqtt = publish_to_mqtt

        self.devices: Dict[str, Dict[str, Any]] = {}  # id → full json block
        self.topic2info: Dict[str, Tuple[str, str, int]] = {}
        self.cap_index: Dict[Tuple[str, str, Optional[str]], str] = {}
        self.rooms: Dict[str, Dict[str, Any]] = {}  # room_id → block

        self._load_config(cfg_path)

    # ---------- config loader ----------
    def _load_config(self, path: str) -> None:
        """
        Read device configuration file and populate internal structures
        - self.devices: full json device description
        - self.topic2info: full_topic → (device_id, 'capabilities' / 'properties', index)
        - self.cap_index: 'capabilities' / 'properties'(device_id, type, instance) → full_topic
        """

        logger.info(f"Try read config file '{path}'")
        try:
            config_data = Path(path).read_text(encoding="utf-8")
            config_data = json.loads(config_data)
            logger.info(
                f"Config loaded: '{json.dumps(config_data, indent=2, ensure_ascii=False)}'"
            )
        except FileNotFoundError:
            logger.error(f"Config file not found: {path}")
            self.devices = {}
            self.topic2info = {}
            self.cap_index = {}
            self.rooms = {}
            return
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config: {e}")
            raise  # Critical error - cannot continue

        self.rooms = config_data.get("rooms", {})
        devices_config = config_data.get("devices", {})
        for device_id, device_data in devices_config.items():
            self.devices[device_id] = device_data

            for i, cap in enumerate(device_data.get("capabilities", [])):
                mqtt_topic = MQTTTopic(cap["mqtt"])  # convert once
                full = mqtt_topic.full  # always full form
                self.topic2info[full] = (device_id, "capabilities", i)
                inst = cap.get("parameters", {}).get("instance")

                # Instance types for each capabilities
                # https://yandex.ru/dev/dialogs/smart-home/doc/en/concepts/capability-types
                self.cap_index[(device_id, cap["type"], inst)] = full

            for i, prop in enumerate(device_data.get("properties", [])):
                mqtt_topic = MQTTTopic(prop["mqtt"])
                full = mqtt_topic.full
                self.topic2info[full] = (device_id, "properties", i)
                index_key = (
                    device_id,
                    prop["type"],
                    prop.get("parameters", {}).get("instance"),
                )
                self.cap_index[index_key] = full

        logger.info(
            f"Devices loaded: {len(self.devices)}, "
            f"mqtt topics: {len(self.topic2info)}"
        )

    def build_yandex_devices_list(self) -> List[Dict[str, Any]]:
        """
        Build devices list in Yandex Smart Home discovery format
        Answer on discovery endpoint: /user/devices
        """

        # "Instance" to "unit" mapping for properties
        INSTANCE_UNITS = {
            "temperature": "unit.temperature.celsius",
            "humidity": "unit.percent",
            "pressure": "unit.pressure.mmhg",
            "illumination": "unit.illumination.lux",
            "voltage": "unit.voltage.volt",
            "current": "unit.amperage.ampere",
            "power": "unit.power.watt",
            "co2_level": "unit.ppm",
            "battery_level": "unit.percent",
        }

        devices_out: List[Dict[str, Any]] = []

        for dev_id, dev in self.devices.items():
            room_name = ""
            room_id = dev.get("room_id")
            if room_id and room_id in self.rooms:
                room_name = self.rooms[room_id].get("name", "")

            device: Dict[str, Any] = {
                "id": dev_id,
                "name": dev.get("name", dev_id),
                "status_info": dev.get("status_info", {"reportable": False}),
                "description": dev.get("description", ""),
                "room": room_name,
                "type": dev["type"],
            }

            caps: List[Dict[str, Any]] = []
            for cap in dev.get("capabilities", []):
                caps.append(
                    {
                        "type": cap["type"],
                        "retrievable": True,
                    }
                )
            if caps:
                device["capabilities"] = caps

            props: List[Dict[str, Any]] = []
            for prop in dev.get("properties", []):
                prop_obj = {
                    "type": prop["type"],
                    "retrievable": True,
                    "reportable": True,
                }
                # Add parameters only if instance exists
                instance = prop.get("parameters", {}).get("instance")
                if instance:
                    unit = INSTANCE_UNITS.get(instance, "unit.temperature.celsius")
                    prop_obj["parameters"] = {
                        "instance": instance,
                        "unit": unit,
                    }
                props.append(prop_obj)
            if props:
                device["properties"] = props

            devices_out.append(device)

        return devices_out

    def forward_mqtt_to_yandex(self, topic: str, raw: str) -> None:
        if topic not in self.topic2info:
            return

        device_id, section, idx = self.topic2info[topic]
        blk = self.devices[device_id][section][idx]

        cap_type = blk["type"]
        instance = blk.get("parameters", {}).get("instance")

        if cap_type.endswith("on_off"):
            value = raw.strip().lower() not in ("0", "false", "off")
        elif cap_type.endswith("float"):
            try:
                value = float(raw)
            except ValueError:
                logger.warning(f"Can't convert '{raw}' to float")
                return
        else:
            value = raw

        self._send_to_yandex(device_id, cap_type, instance, value)

    def forward_yandex_to_mqtt(
        self,
        device_id: str,
        cap_type: str,
        instance: Optional[str],
        value: Any,
    ) -> None:
        key = (device_id, cap_type, instance)
        if key not in self.cap_index:
            logger.warning(f"No mapping for {key}")
            return

        base = self.cap_index[key]  # already full topic
        # Handle topics with or without '/on' suffix
        cmd_topic = base if base.endswith("/on") else f"{base}/on"

        if cap_type.endswith("on_off"):
            payload = "1" if value else "0"
        else:
            payload = str(value)

        self._publish_to_mqtt(cmd_topic, payload)
        logger.debug(f"Published '{payload}' → {cmd_topic}")

    async def _read_capability_state(
        self, device_id: str, cap: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        cap_type = cap["type"]
        instance = cap.get("parameters", {}).get("instance")
        key = (device_id, cap_type, instance)

        topic = self.cap_index.get(key)
        if not topic:
            logger.debug(f"No MQTT topic found for capability: {key}")
            return None
        try:
            value = await read_mqtt_state(topic, mqtt_host="localhost")

            # Normalize boolean values for on_off capabilities
            if cap_type.endswith("on_off"):
                value = bool(value)

            return {
                "type": cap_type,
                "state": {
                    "instance": instance,
                    "value": value,
                },
            }
        except Exception as e:
            logger.debug(f"Failed to read capability topic '{topic}': {e}")
            return None

    async def _read_property_state(
        self, device_id: str, prop: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        prop_type = prop["type"]
        instance = prop.get("parameters", {}).get("instance")
        key = (device_id, prop_type, instance)

        topic = self.cap_index.get(key)
        if not topic:
            logger.debug(f"No MQTT topic found for property: {key}")
            return None
        try:
            msg = await read_topic_once(topic, timeout=1)
            if msg is None:
                logger.debug(f"No retained payload in '{topic}'")
                return None
            raw = msg.payload.decode().strip()
            value = float(raw)  # Currently only float is supported

            return {
                "type": prop_type,
                "state": {
                    "instance": instance,
                    "value": value,
                },
            }
        except Exception as e:
            logger.warning(f"Failed to read property topic '{topic}': {e}")
            return None

    async def get_device_current_state(self, device_id: str) -> Dict[str, Any]:
        device = self.devices.get(device_id)
        if not device:
            logger.warning(f"get_device_current_state: unknown device_id '{device_id}'")
            return {"id": device_id, "error_code": "DEVICE_NOT_FOUND"}

        capabilities_output: List[Dict[str, Any]] = []
        properties_output: List[Dict[str, Any]] = []

        for cap in device.get("capabilities", []):
            logger.debug(f"Reading capability state: '%s'", cap)
            cap_state = await self._read_capability_state(device_id, cap)
            if cap_state:
                capabilities_output.append(cap_state)

        for prop in device.get("properties", []):
            logger.debug(f"Reading property state: '%s'", prop)
            prop_state = await self._read_property_state(device_id, prop)
            if prop_state:
                properties_output.append(prop_state)

        # If nothing was read - mark as unreachable
        if not capabilities_output and not properties_output:
            logger.warning(
                "%s: no live or retained data — marking DEVICE_UNREACHABLE",
                device_id,
            )
            return {
                "id": device_id,
                "error_code": "DEVICE_UNREACHABLE",
                "error_message": "MQTT topics unavailable",
            }

        # If at least something was read - return it
        device_output: Dict[str, Any] = {"id": device_id}
        if capabilities_output:
            device_output["capabilities"] = capabilities_output
        if properties_output:
            device_output["properties"] = properties_output

        return device_output
