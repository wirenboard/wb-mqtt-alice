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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import paho.mqtt.subscribe as subscribe

from converters import (
    convert_rgb_int_to_wb,
    convert_rgb_wb_to_int,
    convert_temp_percent_to_kelvin,
    convert_temp_kelvin_to_percent,
    convert_to_bool,
)
from constants import CAP_COLOR_SETTING, CONFIG_EVENTS_RATE_PATH
from mqtt_topic import MQTTTopic
from wb_alice_device_event_rate import AliceDeviceEventRate


logger = logging.getLogger(__name__)


async def read_topic_once(
    topic: str, *, host: str = "localhost", retain: bool = True, timeout: float = 2.0
) -> Optional[Any]:
    """
    Reads a single retained MQTT message in a separate thread
    Returns paho.mqtt.client.MQTTMessage or None on timeout
    """
    logger.debug(
        "Read topic wait %r message on %r (retain=%r, %.1fs)",
        "retained" if retain else "live",
        topic,
        retain,
        timeout,
    )

    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(subscribe.simple, topic, hostname=host, retained=retain, msg_count=1),
            timeout=timeout,
        )
        if res:
            payload = res.payload.decode().strip()
            logger.debug("Current topic %r state payload: %r", topic, payload)
        else:
            logger.debug("Current topic %r state: None", topic)

        return res
    except asyncio.TimeoutError:
        logger.warning("Read topic timeout waiting %r", topic)
        return None


class DeviceRegistry:
    """
    Parses WB config and routes MQTT to Yandex
    """

    def __init__(
        self,
        cfg_path: str,
        *,
        send_to_yandex: Callable[[str, str, Optional[str], Any], None],
        publish_to_mqtt: Callable[[str, str], Awaitable[None]],
        cfg_events_path: Optional[str] = CONFIG_EVENTS_RATE_PATH,
    ) -> None:
        self._send_to_yandex = send_to_yandex
        self._publish_to_mqtt = publish_to_mqtt
        self._cfg_events_path = cfg_events_path

        self.devices: Dict[str, Dict[str, Any]] = {}  # "id" to full json block
        self.topic2info: Dict[str, Tuple[str, str, int, AliceDeviceEventRate]] = {}
        self.cap_index: Dict[Tuple[str, str, Optional[str]], str] = {}
        self.rooms: Dict[str, Dict[str, Any]] = {}  # "room_id" to block

        self._load_config(cfg_path)

    def _load_config(self, path: str) -> None:
        """
        Read device configuration file and populate internal structures
        - self.devices: full json device description
        - self.topic2info: full_topic → (device_id, 'capabilities' / 'properties', index, AliceDeviceEventRate)
        - self.cap_index: 'capabilities' / 'properties'(device_id, type, instance) → full_topic
        """

        logger.info("Try read config file %r", path)
        try:
            config_data = Path(path).read_text(encoding="utf-8")
            config_data = json.loads(config_data)
            logger.info(
                "Config loaded: %r",
                json.dumps(config_data, indent=2, ensure_ascii=False),
            )
            logger.info("Try to read event rates from %r", CONFIG_EVENTS_RATE_PATH)
            config_evets = Path(self._cfg_events_path).read_text(encoding="utf-8")
            config_evets = json.loads(config_evets)
            logger.info(
                "Config loaded: %r",
                json.dumps(config_evets, indent=2, ensure_ascii=False),
            )
        except FileNotFoundError:
            logger.error("Config file not found: %r", path)
            self.devices = {}
            self.topic2info = {}
            self.cap_index = {}
            self.rooms = {}
            return None
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in config: %r", e)
            raise  # Critical error - cannot continue

        self.rooms = config_data.get("rooms", {})
        devices_config = config_data.get("devices", {})
        for device_id, device_data in devices_config.items():
            self.devices[device_id] = device_data
            for i, cap in enumerate(device_data.get("capabilities", [])):
                mqtt_topic = MQTTTopic(cap["mqtt"])  # convert once
                full = mqtt_topic.full  # always full form
                # event-rate timer
                event_rate_info = config_evets.get(
                    cap["type"],
                    config_evets.get("devices.capabilities.default", {}),
                )
                event_rate = AliceDeviceEventRate(event_rate_info)
                self.topic2info[full] = (device_id, "capabilities", i, event_rate)
                inst = cap.get("parameters", {}).get("instance")
                # Instance types for each capability
                # https://yandex.ru/dev/dialogs/smart-home/doc/en/concepts/capability-types
                self.cap_index[(device_id, cap["type"], inst)] = full

            for i, prop in enumerate(device_data.get("properties", [])):
                mqtt_topic = MQTTTopic(prop["mqtt"])
                full = mqtt_topic.full
                # event-rate timer
                event_rate_info = config_evets.get(
                    prop["type"],
                    config_evets.get("devices.properties.default", {}),
                )
                event_rate = AliceDeviceEventRate(event_rate_info)
                self.topic2info[full] = (device_id, "properties", i, event_rate)
                index_key = (
                    device_id,
                    prop["type"],
                    prop.get("parameters", {}).get("instance"),
                )
                self.cap_index[index_key] = full

        logger.info(
            "Devices loaded: %r, mqtt topics: %r",
            len(self.devices),
            len(self.topic2info),
        )

    def build_yandex_devices_list(self) -> List[Dict[str, Any]]:
        """
        Build devices list in Yandex Smart Home discovery format
        Answer on discovery endpoint: /user/devices
        """

        logger.info("Building device list from %r devices", len(self.devices))

        devices_out: List[Dict[str, Any]] = []

        for dev_id, dev in self.devices.items():
            logger.debug("Processing device: %r - %r", dev_id, dev.get("name", "No name"))
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
                if cap_type == CAP_COLOR_SETTING:
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

                    # Normalize color_scene data:
                    # - WB frontend write data to config in format:
                    #   "color_scene": {"scenes": [ "ocean", "sunset"]}
                    # - Yandex API expects format:
                    #   color_scene: { scenes: [{"id": "ocean"}, {"id": "sunset"}] }
                    if "color_scene" in params:
                        scenes_list = params["color_scene"].get("scenes", [])
                        normalized_scenes = []
                        for scene in scenes_list:
                            if scene and isinstance(scene, str):
                                normalized_scenes.append({"id": scene})
                            else:
                                logger.warning(
                                    "Unexpected scene type in color_scene: %s - %r",
                                    type(scene).__name__,
                                    scene,
                                )
                        color_params["color_scene"] = {"scenes": normalized_scenes}
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
                        "type": CAP_COLOR_SETTING,
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
                    prop_obj["parameters"] = prop_params
                else:
                    logger.warning(
                        "Property %r on device %r has no 'instance' in parameters",
                        prop.get("type"),
                        dev_id,
                    )
                props.append(prop_obj)
            if props:
                device["properties"] = props

            devices_out.append(device)

        logger.info("Final device list contains %r devices:", len(devices_out))
        for i, device in enumerate(devices_out):
            logger.info("  %r. %r - %r", i + 1, device["id"], device["name"])

        return devices_out


    def forward_mqtt_to_yandex(self, topic: str, raw: str) -> None:
        """
        Forwards MQTT message to Yandex Smart Home.

        Args:
            topic: MQTT topic in full format (/devices/device/controls/control)
            raw: Raw payload string from MQTT message
        """
        if topic not in self.topic2info:
            return None

        device_id, section, idx, _ = self.topic2info[topic]
        blk = self.devices[device_id][section][idx]

        cap_type = blk["type"]
        instance = blk.get("parameters", {}).get("instance")

        if cap_type.endswith("on_off"):
            value = raw.strip().lower() not in ("0", "false", "off")
        elif cap_type.endswith("float") or cap_type.endswith("range"):
            try:
                value = float(raw)
            except ValueError:
                logger.warning("Can't convert %r to float", raw)
                return None
        elif cap_type.endswith("color_setting"):
            if instance == "rgb":
                rgb_int = convert_rgb_wb_to_int(raw)
                if rgb_int is None:
                    logger.warning("RGB payload can't be parsed: %r", raw)
                    return None
                value = rgb_int
            elif instance == "temperature_k":
                # Get temperature range from capability config
                temp_params = blk.get("parameters", {}).get("temperature_k", {})
                min_k = temp_params.get("min", 2700)
                max_k = temp_params.get("max", 6500)

                try:
                    percent_value = float(raw)
                    value = convert_temp_percent_to_kelvin(percent_value, min_k, max_k)
                except ValueError:
                    logger.warning("Can't convert %r to temperature_k", raw)
                    return None
            else:
                # for other color_setting
                value = raw
        else:
            # for any other
            value = raw

        # Send color_setting instances for Yandex send "as it"
        self._send_to_yandex(device_id, cap_type, instance, value)

    async def forward_yandex_to_mqtt(
        self,
        device_id: str,
        cap_type: str,
        instance: Optional[str],
        value: Any,
    ) -> None:
        key = (device_id, cap_type, instance)

        if key not in self.cap_index:
            logger.warning("No mapping for %r", key)
            return None

        base = self.cap_index[key]  # already full topic
        cmd_topic = f"{base}/on"

        if cap_type.endswith("on_off"):
            # Handle topics with or without '/on' suffix
            payload = "1" if value else "0"
        elif cap_type.endswith("color_setting"):
            if instance == "rgb":
                # Yandex sends int, convert to WB format "R;G;B"
                try:
                    v_int = int(value)
                except Exception:
                    logger.warning("Unexpected rgb value from Yandex: %r", value)
                    return None
                payload = convert_rgb_int_to_wb(v_int)
            elif instance == "temperature_k":
                # Get device config to extract temperature range
                if device_id not in self.devices:
                    logger.warning("Device %r not found", device_id)
                    return None
                
                device = self.devices[device_id]
                
                # Find capability with temperature_k parameters
                temp_params = None
                for cap in device.get("capabilities", []):
                    if cap["type"] == cap_type:
                        params = cap.get("parameters", {})
                        if params.get("instance") == "temperature_k":
                            temp_params = params.get("temperature_k", {})
                            break
                
                if not temp_params:
                    logger.warning("No temperature_k parameters for device %r", device_id)
                    return None
                
                min_k = temp_params.get("min", 2700)
                max_k = temp_params.get("max", 6500)
                
                # Convert Yandex Kelvin to WB percentage (0-100)
                kelvin_value = int(float(value))
                percent_value = convert_temp_kelvin_to_percent(kelvin_value, min_k, max_k)
                payload = str(percent_value)
                
                logger.debug(
                    "Converted temp %rK → %r%% (range: %r-%rK)",
                    kelvin_value, percent_value, min_k, max_k
                )
        else:
            payload = str(value)

        await self._publish_to_mqtt(cmd_topic, payload)
        logger.debug("Published %r → %r", payload, cmd_topic)

    async def _read_capability_state(self, device_id: str, cap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cap_type = cap["type"]
        instance = cap.get("parameters", {}).get("instance")
        key = (device_id, cap_type, instance)

        topic = self.cap_index.get(key)
        if not topic:
            logger.debug("No MQTT topic found for capability: %r", key)
            return None

        try:
            if cap_type.endswith("on_off"):
                # read like bool
                msg = await read_topic_once(topic, timeout=1)
                if msg is None:
                    # topic not found
                    return None
                raw = msg.payload.decode().strip()
                value = convert_to_bool(raw)
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
                    parsed = convert_rgb_wb_to_int(raw)
                    if parsed is None:
                        return None
                    logger.debug("Successfully parsed RGB: %r to %r", raw, parsed)
                    value = parsed
                elif instance == "temperature_k":
                    # Get temperature range from capability config
                    temp_params = cap.get("parameters", {}).get("temperature_k", {})
                    min_k = temp_params.get("min", 2700)
                    max_k = temp_params.get("max", 6500)

                    try:
                        percent_value = float(raw)
                        value = convert_temp_percent_to_kelvin(percent_value, min_k, max_k)
                    except (ValueError, TypeError):
                        logger.warning("Invalid temperature_k value: %r", raw)
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
            logger.debug("Failed to read capability topic %r: %r", topic, e)
            return None

    async def _read_property_state(self, device_id: str, prop: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        prop_type = prop["type"]
        instance = prop.get("parameters", {}).get("instance")
        key = (device_id, prop_type, instance)

        topic = self.cap_index.get(key)
        if not topic:
            logger.debug("No MQTT topic found for property: %r", key)
            return None
        try:
            msg = await read_topic_once(topic, timeout=1)
            if msg is None:
                logger.debug("No retained payload in %r", topic)
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
            logger.warning("Failed to read property topic %r: %r", topic, e)
            return None

    async def get_device_current_state(self, device_id: str) -> Dict[str, Any]:
        logger.debug("Reading current state for device: %r", device_id)

        device = self.devices.get(device_id)
        if not device:
            logger.warning("get_device_current_state: unknown device_id %r", device_id)
            return {"id": device_id, "error_code": "DEVICE_NOT_FOUND"}

        capabilities_output: List[Dict[str, Any]] = []
        properties_output: List[Dict[str, Any]] = []

        for cap in device.get("capabilities", []):
            logger.debug("Reading capability state: %r", cap)
            cap_state = await self._read_capability_state(device_id, cap)
            logger.debug("Capability result: %r", cap_state)
            if cap_state:
                capabilities_output.append(cap_state)

        for prop in device.get("properties", []):
            logger.debug("Reading property state: %r", prop)
            prop_state = await self._read_property_state(device_id, prop)
            if prop_state:
                properties_output.append(prop_state)

        # If nothing was read - mark as unreachable
        if not capabilities_output and not properties_output:
            logger.warning(
                "%r: no live or retained data — marking DEVICE_UNREACHABLE",
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
