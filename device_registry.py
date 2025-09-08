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
from yandex_handlers import (
    int_to_rgb_wb_format,
    parse_rgb_payload,
)

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
        if res:
            payload = res.payload.decode().strip()
            logger.debug("Current topic '%s' state payload: '%s'", topic, payload)
        else:
            logger.debug("Current topic '%s' state: None", topic)

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
        msg = await read_topic_once(topic, host=mqtt_host, timeout=timeout)
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

        self.devices: Dict[str, Dict[str, Any]] = {}  # "id" to full json block
        self.topic2info: Dict[str, Tuple[str, str, int]] = {}
        self.cap_index: Dict[Tuple[str, str, Optional[str]], str] = {}
        self.rooms: Dict[str, Dict[str, Any]] = {}  # "room_id" to block

        self._load_config(cfg_path)

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

        logger.info(f"Building device list from {len(self.devices)} devices")

        devices_out: List[Dict[str, Any]] = []

        for dev_id, dev in self.devices.items():
            logger.debug(f"Processing device: {dev_id} - {dev.get('name', 'No name')}")
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
            color_params: Dict[str, Any] = {}  # merge holder for color_setting

            for cap in dev.get("capabilities", []):
                cap_type = cap["type"]

                # Merge all color_setting sub-parameters into one capability
                if cap_type == "devices.capabilities.color_setting":
                    params = dict(cap.get("parameters", {}))
                    # Do not include 'instance' in discovery parameters
                    params.pop("instance", None)

                    # color_model: "rgb" | "hsv"
                    if "color_model" in params:
                        cm = params["color_model"]
                        if isinstance(cm, str):
                            color_params["color_model"] = cm

                    # temperature_k: {min, max}
                    if "temperature_k" in params:
                        tk = params["temperature_k"]
                        if isinstance(tk, dict):
                            color_params["temperature_k"] = {
                                "min": tk.get("min"),
                                "max": tk.get("max"),
                            }

                    # color_scene: { scenes: [...] } → normalize to [{'id': ...}]
                    if "color_scene" in params:
                        scenes = params["color_scene"].get("scenes", [])
                        norm = [
                            {"id": s} if isinstance(s, str) else s for s in scenes if s
                        ]
                        color_params["color_scene"] = {"scenes": norm}
                    continue  # skip adding separate color_setting capability

                # Non-color capabilities: pass through as-is
                cap_dict = {"type": cap_type, "retrievable": True}
                if "parameters" in cap and cap["parameters"]:
                    cap_dict["parameters"] = cap["parameters"].copy()
                caps.append(cap_dict)

            # Append merged color_setting (if any sub-params were found)
            if color_params:
                caps.append(
                    {
                        "type": "devices.capabilities.color_setting",
                        "retrievable": True,
                        "parameters": color_params,
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
                # Always send "instance", but "unit" only if present in config
                params = prop.get("parameters", {}) or {}
                instance = params.get("instance")
                if instance:
                    prop_params: Dict[str, Any] = {"instance": instance}
                    unit_cfg = params.get("unit")
                    if isinstance(unit_cfg, str) and unit_cfg.strip():
                        prop_params["unit"] = unit_cfg.strip()
                    else:
                        # If unit not present - not send this fields
                        pass
                    prop_obj["parameters"] = prop_params
                else:
                    logger.warning(
                        "Property '%s' on device '%s' has no 'instance' in parameters",
                        prop.get("type"),
                        dev_id,
                    )
                props.append(prop_obj)
            if props:
                device["properties"] = props

            devices_out.append(device)

        logger.info(f"Final device list contains {len(devices_out)} devices:")
        for i, device in enumerate(devices_out):
            logger.info(f"  {i+1}. {device['id']} - {device['name']}")

        return devices_out

    def forward_mqtt_to_yandex(self, topic: str, raw: str) -> None:
        """
        Forwards MQTT message to Yandex Smart Home.

        Args:
            topic: MQTT topic in full format (/devices/device/controls/control)
            raw: Raw payload string from MQTT message
        """
        if topic not in self.topic2info:
            return

        device_id, section, idx = self.topic2info[topic]
        blk = self.devices[device_id][section][idx]

        cap_type = blk["type"]
        instance = blk.get("parameters", {}).get("instance")

        if cap_type.endswith("on_off"):
            value = raw.strip().lower() not in ("0", "false", "off")
        elif cap_type.endswith("float") or cap_type.endswith("range"):
            try:
                value = float(raw)
            except ValueError:
                logger.warning(f"Can't convert '{raw}' to float")
                return
        elif cap_type.endswith("color_setting"):
            if instance == "rgb":
                rgb_int = parse_rgb_payload(raw)
                if rgb_int is None:
                    logger.warning("RGB payload can't be parsed: %r", raw)
                    return
                value = rgb_int
            elif instance == "temperature_k":
                try:
                    value = int(float(raw))
                except ValueError:
                    logger.warning(f"Can't convert '{raw}' to temperature_k")
                    return
            else:
                # for other color_setting
                value = raw
        else:
            # for any other
            value = raw

        # Send color_setting instances for Yandex send "as it"
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
        cmd_topic = f"{base}/on"

        if cap_type.endswith("on_off"):
            # Handle topics with or without '/on' suffix
            payload = "1" if value else "0"
        elif cap_type.endswith("color_setting"):
            cmd_topic = f"{base}"
            if instance == "rgb":
                # Yandex sends int, convert to WB format "R;G;B"
                try:
                    v_int = int(value)
                except Exception:
                    logger.warning("Unexpected rgb value from Yandex: %r", value)
                    return
                payload = int_to_rgb_wb_format(v_int)
            elif instance == "temperature_k":
                payload = str(int(float(value)))
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
            if cap_type.endswith("on_off"):
                # read like bool
                value = await read_mqtt_state(topic, mqtt_host="localhost")
                if value is None:
                    # topic not found
                    return None
                value = bool(value)
            elif cap_type.endswith("range"):
                # Read range value as float
                msg = await read_topic_once(topic, timeout=1)
                if msg is None:
                    return None
                value = float(msg.payload.decode().strip())
            elif cap_type.endswith("color_setting"):
                # Read color value and normalize instance
                msg = await read_topic_once(topic, timeout=1)
                if msg is None:
                    return None
                raw = msg.payload.decode().strip()
                if instance == "rgb":
                    parsed = parse_rgb_payload(raw)
                    if parsed is None:
                        return None
                    logger.debug(f"Successfully parsed RGB: {raw} -> {parsed}")
                    value = parsed
                elif instance == "temperature_k":
                    # Convert to integer for temperature
                    try:
                        value = int(float(raw))
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid temperature_k value: {raw}")
                        return None
                else:
                    value = raw
            else:
                msg = await read_topic_once(topic, timeout=1)
                if msg is None:
                    return None
                value = msg.payload.decode().strip()

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
        logger.debug(f"Reading current state for device: {device_id}")

        device = self.devices.get(device_id)
        if not device:
            logger.warning(f"get_device_current_state: unknown device_id '{device_id}'")
            return {"id": device_id, "error_code": "DEVICE_NOT_FOUND"}

        capabilities_output: List[Dict[str, Any]] = []
        properties_output: List[Dict[str, Any]] = []

        for cap in device.get("capabilities", []):
            logger.debug(f"Reading capability state: '%s'", cap)
            cap_state = await self._read_capability_state(device_id, cap)
            logger.debug(f"Capability result: {cap_state}")
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
