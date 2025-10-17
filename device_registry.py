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

    def _merge_color_setting_params(self, capabilities: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Merge all color_setting sub-parameters into one capability
        
        WirenBoard stores color_setting as separate capabilities (rgb, temperature_k, etc.),
        but Yandex expects them merged into single capability with combined parameters
        
        Args:
            capabilities: List of device capabilities from config
        
        Returns:
            Merged color_setting parameters dict, empty if no color capabilities found
        """
        color_params: Dict[str, Any] = {}
        
        for cap in capabilities:
            cap_type = cap.get("type")
            if cap_type is None:
                continue
            if cap_type != CAP_COLOR_SETTING:
                continue
                
            params = dict(cap.get("parameters", {}))
            params.pop("instance", None)  # Don't include 'instance' in discovery
            
            # color_model: "rgb" | "hsv"
            if "color_model" in params and isinstance(params["color_model"], str):
                color_params["color_model"] = params["color_model"]
            
            # temperature_k: {min, max}
            if "temperature_k" in params and isinstance(params["temperature_k"], dict):
                tk = params["temperature_k"]
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
                if normalized_scenes:
                    color_params["color_scene"] = {"scenes": normalized_scenes}
        
        return color_params

    def build_yandex_devices_list(self) -> List[Dict[str, Any]]:
        """
        Build devices list in Yandex Smart Home discovery format
        Answer on discovery endpoint: /user/devices

        Returns:
            List of devices for /user/devices endpoint response
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

            for cap in dev.get("capabilities", []):
                if cap["type"] == CAP_COLOR_SETTING:
                    continue  # Will be merged later
                cap_dict = {"type": cap["type"], "retrievable": True}
                if "parameters" in cap and cap["parameters"]:
                    cap_dict["parameters"] = cap["parameters"].copy()
                caps.append(cap_dict)

            # Merge and append color_setting if present
            color_params = self._merge_color_setting_params(dev.get("capabilities", []))
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

    def _convert_cap_to_yandex(
        self, 
        raw: str, 
        cap_type: str, 
        instance: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Any:
        """
        Convert raw MQTT payload string to Yandex Smart Home capability format

        Args:
            raw: Raw MQTT payload string from WirenBoard device
                Examples: - "1"
                          - "255;128;64" (RGB)
            cap_type: Yandex capability type string
                Examples: - "devices.capabilities.on_off"
                          - "devices.capabilities.range"
            [instance]: Capability instance identifier, specific to capability type
                Examples: - "on" (on_off)
                          - "rgb"/"temperature_k" (color_setting)
                Defaults to None - for capabilities that don't require instance
            [params]: Device-specific capability parameters from configuration
                May contain type-specific settings such as:
                Examples: - temperature_k range: {"temperature_k": {"min": 2700, "max": 6500}}
                          - color_model: {"color_model": "rgb"}
                Defaults to None (empty dict used internally)

        Returns:
            Converted value in Yandex format

        Raises:
            ValueError: If raw value cannot be converted to expected format
        """
        # Use empty dict if None provided
        params = params or {}
        
        if cap_type.endswith("on_off"):
            return convert_to_bool(raw)

        elif cap_type.endswith("float") or cap_type.endswith("range"):
            return float(raw)
        
        elif cap_type.endswith("color_setting"):
            if instance == "rgb":
                rgb_int = convert_rgb_wb_to_int(raw)
                if rgb_int is None:
                    raise ValueError(f"Can't parse RGB value: {raw!r}")
                logger.debug("Successfully parsed RGB: %r to %r", raw, rgb_int)
                return rgb_int
            
            elif instance == "temperature_k":
                # Get temperature range from capability config
                temp_params = params.get("temperature_k", {})
                min_k = temp_params.get("min", 2700)
                max_k = temp_params.get("max", 6500)
                percent_value = float(raw)
                return convert_temp_percent_to_kelvin(percent_value, min_k, max_k)

            else:
                # Other color_setting instances (e.g., color_scene) - passthrough
                return raw

        else:
            # Unknown capability types - passthrough as string
            return raw


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
        try:
            value = self._convert_cap_to_yandex(raw, cap_type, instance, blk.get("parameters"))
            self._send_to_yandex(device_id, cap_type, instance, value)
        except (ValueError, TypeError) as e:
            logger.warning("Failed to convert MQTT→Yandex for topic %r: %r", topic, e)

    def _convert_cap_to_mqtt(
        self, 
        value: Any,
        cap_type: str, 
        instance: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Convert Yandex Smart Home value to MQTT payload string WirenBoard format

        Args:
            value: Value from Yandex in their format
                Examples: - True/False (on_off)
                            - 16744448 (RGB as int 0xFF8000)
                            - 4500 (temperature in Kelvin)
            cap_type: Yandex capability type string
                Examples: - "devices.capabilities.on_off"
                            - "devices.capabilities.color_setting"
            [instance]: Capability instance identifier
                Examples: - "on" (on_off)
                            - "rgb"/"temperature_k" (color_setting)
                Defaults to None for capabilities without instances
            [params]: Device-specific capability parameters
                Examples: - temperature_k range: {"temperature_k": {"min": 2700, "max": 6500}}
                Defaults to None (empty dict used internally)
        """
        params = params or {}

        if cap_type.endswith("on_off"):
            return "1" if value else "0"

        elif cap_type.endswith("color_setting"):
            if instance == "rgb":
                # Yandex sends int, convert to WB format "R;G;B"
                try:
                    v_int = int(value)
                except (ValueError, TypeError):
                    raise ValueError(f"Unexpected RGB value from Yandex: {value!r}")
                return convert_rgb_int_to_wb(v_int)

            elif instance == "temperature_k":
                # Get device config to extract temperature range
                temp_params = params.get("temperature_k", {})
                min_k = temp_params.get("min", 2700)
                max_k = temp_params.get("max", 6500)
                
                # Convert Yandex Kelvin to WB percentage (0-100)
                try:
                    kelvin_value = int(float(value))
                except (ValueError, TypeError):
                    raise ValueError(f"Invalid temperature value from Yandex: {value!r}")
                percent_value = convert_temp_kelvin_to_percent(kelvin_value, min_k, max_k)
                
                logger.debug(
                    "Converted temp %rK → %r%% (range: %r-%rK)",
                    kelvin_value, percent_value, min_k, max_k
                )
                
                return str(percent_value)
            
            else:
                # Other color_setting instances - passthrough
                return str(value)
        
        else:
            # Unknown capability types - passthrough as string
            return str(value)


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

        # Get device parameters for conversion, later extract temperature range
        device = self.devices.get(device_id)
        if not device:
            logger.warning("Device %r not found (key=%r)", device_id, key)
            return None
        cap_params: Optional[Dict[str, Any]] = None
        for cap in device.get("capabilities", []):
            if cap.get("type") == cap_type:
                params = cap.get("parameters", {}) or {}
                cap_instance = params.get("instance")
                if cap_instance == instance:
                    cap_params = params
                    break

        # Convert value to MQTT format
        try:
            payload = self._convert_cap_to_mqtt(value, cap_type, instance, cap_params)
        except (ValueError, TypeError) as e:
            logger.warning(
                "Failed to convert Yandex→MQTT for %r (device=%r, instance=%r, value=%r): %s",
                cap_type, device_id, instance, value, e
            )
            return None

        await self._publish_to_mqtt(cmd_topic, payload)
        logger.debug("Published %r → %r", payload, cmd_topic)

    async def _read_capability_state(self, device_id: str, cap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Read capability state from MQTT and convert to Yandex format
        """
        cap_type = cap["type"]
        instance = cap.get("parameters", {}).get("instance")
        key = (device_id, cap_type, instance)

        topic = self.cap_index.get(key)
        if not topic:
            logger.debug("No MQTT topic found for capability: %r", key)
            return None

        try:
            msg = await read_topic_once(topic, timeout=1)
            if msg is None:
                return None  # topic not found
            raw = msg.payload.decode().strip()
        except Exception as e:
            logger.debug("Failed to read capability topic %r: %r", topic, e)
            return None

        try:
            value = self._convert_cap_to_yandex(raw, cap_type, instance, cap.get("parameters"))
            return {
                "type": cap_type,
                "state": {
                    "instance": instance,
                    "value": value,
                },
            }
        except (ValueError, TypeError) as e:
            logger.warning("Failed to convert value for %r: %r", key, e)
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
