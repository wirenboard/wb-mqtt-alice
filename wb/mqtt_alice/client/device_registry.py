#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Device Registry Module for Wiren Board Alice Integration
Handles device configuration, MQTT-Yandex routing
"""

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple
from uuid import uuid4

import paho.mqtt.client as mqtt_client
import paho.mqtt.subscribe as subscribe

from wb.mqtt_alice.common.constants import (
    CAP_COLOR_SETTING,
    CAP_MODE,
    CAP_ON_OFF,
    CONFIG_EVENTS_RATE_PATH,
    ERR_DEVICE_UNREACHABLE,
    ERR_INVALID_ACTION,
    ERR_INVALID_VALUE,
)

from .converters import (
    EventType,
    clamp_range_value,
    convert_mqtt_event_value,
    convert_rgb_int_to_wb,
    convert_rgb_wb_to_int,
    convert_temp_kelvin_to_percent,
    convert_temp_percent_to_kelvin,
    convert_to_bool,
    format_range_payload,
    resolve_relative_range_value,
)
from .mqtt_topic import MQTTTopic
from .wb_alice_device_event_rate import AliceDeviceEventRate

logger = logging.getLogger(__name__)

# How long to wait for the current value when resolving a relative command
# Kept short on purpose: Yandex waits for the action response, and the value is
# retained, so a healthy broker answers immediately
RELATIVE_READ_TIMEOUT_S = 1.0


class ActionError(Exception):
    """
    Action from Yandex that failed in a way the user must be told about

    Carries a Yandex error_code so the action response can say what went wrong
    instead of reporting DONE and leaving the user wondering why nothing moved
    """

    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code


def is_property_event(prop: str = "") -> bool:
    """
    Check if property type is a Yandex Smart Home event property

    Args:
        prop: Property type string to check

    Returns:
        True if property type equals "devices.properties.event" (case-insensitive),
        False otherwise

    Example:
        >>> is_property_event("devices.properties.event")
        True
        >>> is_property_event("devices.properties.float")
        False
    """
    if prop.lower() == "devices.properties.event":
        return True
    return False


def is_event_single_topic(items: Iterable[Dict[Any, Any]]) -> bool:
    """
    Check if all items have single unique value per key

    Determines whether a collection of event properties uses a single-topic pattern
    (one MQTT topic with boolean values) vs multi-topic pattern (multiple topics).

    Args:
        items: Iterable of dictionaries containing event property parameters

    Returns:
        True if each key across all items has only one unique value,
        False if any key has multiple different values

    Note: User implementation patterns may vary, so this function only checks
        whether different values exist for the same key. For example, door open
        events can be implemented in two ways:
        - Single topic with boolean values (single-topic):
            "1" -> "opened"
            "0" -> "closed"
        - Two separate topics (multi-topic):
            Topic "door/opened" with "1" or "0"
            Topic "door/closed" with "1" or "0"
        In the first case the function returns True, in the second case - False.

    Example:
        >>> is_event_single_topic([{"instance": "open"}, {"instance": "open"}])
        True
        >>> is_event_single_topic([{"instance": "open"}, {"instance": "motion"}])
        False
    """
    # Group by instance
    by_instance: Dict[Any, List[Dict[Any, Any]]] = defaultdict(list)
    for item in items:
        instance = item.get("instance")
        by_instance[instance].append(item)

    # For each group compare instance for keys
    for instance, group in by_instance.items():
        values_per_key: Dict[Any, Set[Any]] = defaultdict(set)
        for item in group:
            for key, value in item.items():
                if key != "instance":
                    values_per_key[key].add(value)
                    if len(values_per_key[key]) > 1:
                        return False
    return True


def merge_event_prop_by_instance(props: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge multiple event properties with same instance into single property with events array

    Transforms individual event property entries into Yandex Smart Home format where
    events with the same instance are grouped under a single property with an events array.

    Input format (multiple properties):
        [{"type": "devices.properties.event", "parameters": {"instance": "open", "value": "opened"}},
         {"type": "devices.properties.event", "parameters": {"instance": "open", "value": "closed"}}]

    Output format (merged property):
        [{"type": "devices.properties.event", "parameters": {
            "instance": "open",
            "events": [{"value": "opened"}, {"value": "closed"}]
        }}]

    Args:
        props: List of property dictionaries from device configuration

    Returns:
        List with non-event properties unchanged and event properties merged by instance
    """
    other_props: List[Dict[str, Any]] = []
    events_by_instance: Dict[str, List[str]] = defaultdict(list)

    for cur_prop in props:
        try:
            cur_prop_type = cur_prop.get("type")
        except Exception:
            cur_prop_type = None
        if is_property_event(cur_prop_type):
            params = cur_prop.get("parameters") or {}
            instance = params.get("instance")
            value = params.get("value")
            if instance is None:
                other_props.append(cur_prop)
                continue
            val = extract_event_value(value)
            if val not in events_by_instance[instance]:
                events_by_instance[instance].append(val)
        else:
            other_props.append(cur_prop)

    merged_events_props: List[Dict[str, Any]] = []
    for instance, values in events_by_instance.items():
        events_list = []
        for value in values:
            if value == "":
                continue
            events_list.append({"value": value})
        if not events_list:
            continue
        merged = {
            "type": "devices.properties.event",
            "retrievable": False,
            "reportable": True,
            "parameters": {
                "instance": instance,
                "events": events_list,
            },
        }
        merged_events_props.append(merged)

    return other_props + merged_events_props


def extract_event_value(value: Any) -> str:
    """
    Convert parameter ``value`` to an event string

    If ``value`` contains a dot, return the substring after the last dot.
    Otherwise return ``str(value)``.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    if "." in value:
        return value.split(".")[-1]
    return value


async def read_topic_once(
    topic: str,
    *,
    host: str = "localhost",
    retain: bool = True,
    timeout: float = 2.0,
    prop_type: str = "",
    instance: Optional[str] = None,
    unit_or_event_value: Optional[str] = None,
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
            if is_property_event(prop_type):
                res.payload = convert_mqtt_event_value(
                    event_type=instance,
                    event_type_value=unit_or_event_value,
                    value=res.payload.decode().strip(),
                ).encode()

            logger.debug("Current topic %r state payload: %r", topic, payload)
        else:
            logger.debug("Current topic %r state: None", topic)

        return res
    except asyncio.TimeoutError:
        logger.warning("Read topic timeout waiting %r", topic)
        return None


async def read_retained_value(
    topic: str,
    *,
    host: str = "localhost",
    timeout: float = RELATIVE_READ_TIMEOUT_S,
) -> Optional[str]:
    """
    Read the current retained payload of a topic, or None if it does not arrive

    Deliberately does not reuse read_topic_once(): that one blocks inside
    subscribe.simple(), so an expired asyncio.wait_for() abandons the coroutine
    while the thread behind asyncio.to_thread() keeps waiting for a message that
    may never come. Here the client is ours, so the timeout path can tear the
    connection down and let the network thread exit

    TODO: this whole helper goes away once DeviceRegistry keeps a state cache -
          see the note in _resolve_relative_value()

    Args:
        topic: Full MQTT topic of the control (without the /on suffix)
        [host]: Broker address, local broker by default
        [timeout]: How long to wait for the retained message

    Returns:
        Decoded and stripped payload, or None on timeout, connection failure
        or undecodable payload
    """
    loop = asyncio.get_running_loop()
    result: "asyncio.Future[Optional[str]]" = loop.create_future()
    client = mqtt_client.Client(client_id=f"wb-alice-read-{uuid4().hex[:8]}")

    def _resolve(value: Optional[str]) -> None:
        if not result.done():
            result.set_result(value)

    def _on_connect(cli: mqtt_client.Client, _userdata: Any, _flags: Dict[str, Any], rc: int) -> None:
        if rc != 0:
            logger.warning("Read of %r failed, broker refused connection with code %r", topic, rc)
            loop.call_soon_threadsafe(_resolve, None)
            return None
        cli.subscribe(topic, qos=0)

    def _on_message(_cli: mqtt_client.Client, _userdata: Any, message: mqtt_client.MQTTMessage) -> None:
        try:
            payload = message.payload.decode().strip()
        except UnicodeDecodeError:
            logger.warning("Cannot decode payload of %r", topic)
            payload = None
        loop.call_soon_threadsafe(_resolve, payload)

    client.on_connect = _on_connect
    client.on_message = _on_message

    try:
        await asyncio.to_thread(client.connect, host)
        client.loop_start()
        payload = await asyncio.wait_for(result, timeout)
        logger.debug("Current value of %r: %r", topic, payload)
        return payload
    except asyncio.TimeoutError:
        logger.warning("Timeout reading current value of %r", topic)
        return None
    except Exception as e:
        logger.warning("Failed to read current value of %r: %r", topic, e)
        return None
    finally:
        # Both calls are needed: disconnect() wakes the network thread up,
        # loop_stop() joins it - otherwise every command leaks one thread
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            logger.debug("Cleanup after reading %r failed", topic, exc_info=True)


class DeviceRegistry:
    """
    Parses WB config and routes MQTT to Yandex
    """

    def __init__(
        self,
        cfg_path: str,
        *,
        send_to_yandex: Callable[[str, str, Optional[str], Any], Optional[Dict[str, Any]]],
        publish_to_mqtt: Callable[[str, str], Awaitable[None]],
        cfg_events_path: Optional[str] = CONFIG_EVENTS_RATE_PATH,
    ) -> None:
        self._send_to_yandex = send_to_yandex
        self._publish_to_mqtt = publish_to_mqtt
        self._cfg_events_path = cfg_events_path

        self.devices: Dict[str, Dict[str, Any]] = {}  # "id" to full json block
        self.topic2info: Dict[str, Tuple[str, str, int, AliceDeviceEventRate]] = {}
        # Map (device_id, type, instance, instance_value) → MQTT topic
        # - device_id: unique device identifier
        # - type: capability/property type (e.g., "devices.capabilities.on_off")
        # - instance: capability instance (e.g., "on", "rgb", "temperature") or None
        # - instance_value: event value (e.g., "opened") for event properties, None for others
        self.cap_index: Dict[Tuple[str, str, Optional[str], Optional[str]], str] = {}
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
            logger.debug(
                "Config loaded: %r",
                json.dumps(config_data, indent=2, ensure_ascii=False),
            )
            logger.info("Try to read event rates from %r", CONFIG_EVENTS_RATE_PATH)
            config_evets = Path(self._cfg_events_path).read_text(encoding="utf-8")
            config_evets = json.loads(config_evets)
            logger.debug(
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
                # Instance types for each capability
                # https://yandex.ru/dev/dialogs/smart-home/doc/en/concepts/capability-types
                instance, instance_value = self._extract_instance_with_value(cap)
                self.cap_index[(device_id, cap["type"], instance, instance_value)] = full

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
                instance, instance_value = self._extract_instance_with_value(prop)
                index_key = (device_id, prop["type"], instance, instance_value)
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

    def _build_mode_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Strip internal mqtt_value_match from each mode — Yandex API only needs `value`.

        WB config stores the mode mapping in format:
            "modes": [{"value": "auto", "mqtt_value_match": "0"}, ...]

        Yandex Smart Home discovery expects:
            "modes": [{"value": "auto"}, ...]
        """
        cleaned = dict(params)
        cleaned["modes"] = [{"value": m["value"]} for m in params.get("modes") or []]
        return cleaned

    def build_yandex_devices_list(self) -> List[Dict[str, Any]]:
        """
        Build devices list in Yandex Smart Home discovery format
        Answer on discovery endpoint: /user/devices

        Returns:
            List of devices for /user/devices endpoint response
        """
        logger.debug("Building device list from %r devices", len(self.devices))
        devices_out: List[Dict[str, Any]] = []

        for dev_id, dev in self.devices.items():
            logger.debug("Processing device: %r - %r", dev_id, dev.get("name", "No name"))

            device = self._create_device_base_info(dev_id, dev)
            caps = self._collect_capabilities(dev)
            if caps:
                device["capabilities"] = caps
            props = self._collect_properties(dev_id, dev)
            if props:
                device["properties"] = props

            devices_out.append(device)

        logger.debug("Final device list contains %r devices:", len(devices_out))
        for i, device in enumerate(devices_out):
            logger.debug("  %r. %r - %r", i + 1, device["id"], device["name"])

        return devices_out

    def _create_device_base_info(self, dev_id: str, dev: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create basic device metadata structure
        """
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

        return device

    def _collect_capabilities(self, dev: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Collect and format device capabilities
        """
        caps: List[Dict[str, Any]] = []

        # Standard capabilities
        for cap in dev.get("capabilities", []):
            if cap["type"] == CAP_COLOR_SETTING:
                continue  # Will be merged later

            cap_dict = {
                "type": cap["type"],
                "retrievable": cap.get("retrievable", True),
                "reportable": cap.get("reportable", True),
            }
            if cap["type"] == CAP_MODE:
                cap_dict["parameters"] = self._build_mode_params(cap.get("parameters") or {})
            elif cap["type"] == CAP_ON_OFF:
                # Always send 'split' explicitly; default False matches Yandex API
                params = (cap.get("parameters") or {}).copy()
                params.setdefault("split", False)
                cap_dict["parameters"] = params
            elif cap.get("parameters"):
                cap_dict["parameters"] = cap["parameters"].copy()
            caps.append(cap_dict)

        # Merge color_setting (rgb / temperature_k / color_scene) into a single
        # Yandex capability — retrievable is AND of all parts since Yandex sees one block
        color_caps = [c for c in dev.get("capabilities", []) if c.get("type") == CAP_COLOR_SETTING]
        color_params = self._merge_color_setting_params(color_caps)
        if color_params:
            caps.append(
                {
                    "type": CAP_COLOR_SETTING,
                    "retrievable": all(c.get("retrievable", True) for c in color_caps),
                    "reportable": all(c.get("reportable", True) for c in color_caps),
                    "parameters": color_params,
                }
            )

        return caps

    def _collect_properties(self, dev_id: str, dev: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Collect and format properties, handling event logic
        """
        props: List[Dict[str, Any]] = []
        for prop in dev.get("properties", []):
            is_event = is_property_event(prop["type"])

            # 'retrievable' tells whether Yandex may query the property state
            # Event properties are forced to false: events may span multiple MQTT
            # topics and we have no local state cache, so the last known value
            # cannot be returned
            # TODO: unlock retrievable for events once a local state store exists
            if is_event:
                if prop.get("retrievable") is True:
                    logger.warning(
                        "Property %r on device %r: retrievable=true is not supported for"
                        " event properties yet (no local state cache); coercing to false",
                        prop.get("type"),
                        dev_id,
                    )
                retrievable = False
            else:
                retrievable = prop.get("retrievable", True)

            # 'reportable' tells whether we push state updates to Yandex
            # Event properties are forced to true: an event only exists as a push,
            # so disabling reporting would make the property useless
            if is_event:
                if prop.get("reportable") is False:
                    logger.warning(
                        "Property %r on device %r: reportable=false is not supported for"
                        " event properties (events only exist as push updates); coercing to true",
                        prop.get("type"),
                        dev_id,
                    )
                reportable = True
            else:
                reportable = prop.get("reportable", True)

            prop_obj = {
                "type": prop["type"],
                "retrievable": retrievable,
                "reportable": reportable,
            }
            # Always send "instance", but "unit" only if present in config
            params = prop.get("parameters", {}) or {}
            instance = params.get("instance")
            if not instance:
                logger.warning(
                    "Property %r on device %r has no 'instance' in parameters",
                    prop.get("type"),
                    dev_id,
                )
                props.append(prop_obj)
                continue

            prop_params: Dict[str, Any] = {"instance": instance}
            if is_event:
                # Event property
                # "value" is required for event properties
                value_cfg = params.get("value")
                if isinstance(value_cfg, str) and value_cfg.strip():
                    prop_params["value"] = value_cfg.strip()
                counter = 0
                for cur_prop in props:
                    if cur_prop["parameters"]["instance"] == instance:
                        counter += 1
                # append default oppozit values for some events
                if counter == 0:
                    _oppozit_obj = dict(prop_obj)
                    _oppozit_params = dict(prop_params)
                    _prefix, _value = value_cfg.strip().split(".")
                    if instance in [EventType.OPEN, EventType.WATER_LEAK, EventType.MOTION]:
                        oppozit_val = convert_mqtt_event_value(
                            event_type=instance,
                            event_type_value=_value,
                            value=0,
                            event_single_topic=True,
                        )
                        _oppozit_params["value"] = _prefix + "." + oppozit_val
                    _oppozit_obj["parameters"] = _oppozit_params
                    props.append(_oppozit_obj)
            else:
                # Float property (or non-event property)
                # "unit" is required for float properties
                unit_cfg = params.get("unit")
                if isinstance(unit_cfg, str) and unit_cfg.strip():
                    prop_params["unit"] = unit_cfg.strip()

            prop_obj["parameters"] = prop_params
            props.append(prop_obj)

        for _prop in props:
            if is_property_event(_prop.get("type", "")):
                return merge_event_prop_by_instance(props)

        return props

    def _convert_cap_to_yandex(
        self, raw: str, cap_type: str, instance: Optional[str] = None, params: Optional[Dict[str, Any]] = None
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

        if cap_type.endswith("on_off") or cap_type.endswith("toggle"):
            return convert_to_bool(raw)

        elif cap_type.endswith("float") or cap_type.endswith("range"):
            return float(raw)

        elif cap_type.endswith("mode"):
            # Look up Yandex mode value by mqtt_value_match in parameters.modes.
            # On malformed config caller catches ValueError and skips the state update.
            for mode in params.get("modes") or []:
                if mode.get("mqtt_value_match") == raw:
                    val = mode.get("value")
                    if val is None:
                        raise ValueError(f"Mode entry malformed (missing 'value'): {mode!r}")
                    return val
            raise ValueError(f"No mode mapping for mqtt_value_match={raw!r}")

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

        elif is_property_event(cap_type):
            # For event properties, we want to send the post processing raw value
            return self._convert_events_to_yandex(instance, params.get("value"), raw)
        else:
            # Unknown capability types - passthrough as string
            return raw

    def _convert_events_to_yandex(self, instance: Optional[str], value: Optional[str], raw: str) -> str:
        """
        Convert raw event property value to Yandex format using handler functions for each instance type

        Args:
            instance: Property instance identifier
            value: Property value from configuration
            raw: Raw MQTT payload string

        Returns:
            Converted value in Yandex format. For unknown or unhandled instances, the raw value is returned as-is.
        """
        # Coerce and normalize raw payload early
        raw = "" if raw is None else raw.strip() if isinstance(raw, str) else raw
        if not instance:
            logger.debug("No instance provided for event property, returning raw value")
            return raw
        # UNCOMMENT if need to mapping of event instance to handler function for extensibility
        # event_handlers = {
        #     "motion": handle_motion,
        #     etc..
        # }
        # handler = event_handlers.get(instance)
        # if handler:
        #     return handler(raw)
        logger.debug("Event occurred for instance %r, value %r", instance, value)
        if isinstance(value, str) and value:
            return extract_event_value(value)
        return raw

    def convert_mqtt_to_yandex_block(self, topic: str, raw: str) -> Optional[Dict[str, Any]]:
        """
        Convert MQTT message to Yandex Smart Home device state block

        Args:
            - topic: MQTT topic in full format (/devices/device/controls/control)
            - raw: Raw payload string from MQTT message

        Returns:
            - Device state block dict
              {"id": ..., "status": ..., "capabilities"/"properties": [...]}
            - or None if topic unknown, conversion failed, or event value is None
        """
        if topic not in self.topic2info:
            return None

        device_id, section, idx, _ = self.topic2info[topic]
        blk = self.devices[device_id][section][idx]

        cap_type = blk["type"]
        instance = blk.get("parameters", {}).get("instance")

        # Honor reportable — skip push when explicitly false
        # Event properties bypass: they are forced reportable in _collect_properties
        if not is_property_event(cap_type) and blk.get("reportable", True) is False:
            logger.debug("Skipping push for non-reportable %r on topic %r", cap_type, topic)
            return None
        try:
            if is_property_event(cap_type):
                param_list = []
                for prop in self.devices[device_id].get("properties", []):
                    param_list.append(prop.get("parameters"))
                event_single_topic = is_event_single_topic(param_list)
                value = convert_mqtt_event_value(
                    event_type=instance,
                    event_type_value=extract_event_value(blk.get("parameters", {}).get("value")),
                    value=raw,
                    event_single_topic=event_single_topic,
                )
                if value is None:
                    # Event not triggered (e.g. button release "0") — skip silently
                    logger.debug("Event value is None for topic %r, skipping", topic)
                    return None
            else:
                value = self._convert_cap_to_yandex(raw, cap_type, instance, blk.get("parameters"))
            return self._send_to_yandex(device_id, cap_type, instance, value)
        except (ValueError, TypeError) as e:
            logger.warning("Failed to convert MQTT→Yandex for topic %r: %r", topic, e)
            return None

    def _convert_cap_to_mqtt(
        self,
        value: Any,
        cap_type: str,
        instance: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
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

        if cap_type.endswith("on_off") or cap_type.endswith("toggle"):
            return "1" if value else "0"

        elif cap_type.endswith("mode"):
            # Look up mqtt_value_match by Yandex mode value in parameters.modes.
            # On malformed config caller catches ValueError and skips the publish.
            for mode in params.get("modes") or []:
                if mode.get("value") == value:
                    mqtt_value_match = mode.get("mqtt_value_match")
                    if mqtt_value_match is None:
                        raise ValueError(f"Mode entry malformed (missing 'mqtt_value_match'): {mode!r}")
                    return mqtt_value_match
            raise ValueError(f"No mqtt_value_match for mode={value!r}")

        elif cap_type.endswith("range"):
            # Relative commands are already resolved to an absolute value by
            # forward_yandex_to_mqtt(), so only clamping is left here. Precision
            # is not applied on purpose: Yandex aligns absolute values to it
            # already, and re-snapping would move a value the user set explicitly
            try:
                range_value = float(value)
            except (ValueError, TypeError):
                raise ValueError(f"Unexpected range value from Yandex: {value!r}")
            return format_range_payload(clamp_range_value(range_value, params.get("range")))

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
                    "Converted temp %rK → %r%% (range: %r-%rK)", kelvin_value, percent_value, min_k, max_k
                )

                return str(percent_value)

            else:
                # Other color_setting instances - passthrough
                return str(value)

        else:
            # Unknown capability types - passthrough as string
            return str(value)

    async def _resolve_relative_value(
        self,
        topic: str,
        cap_type: str,
        cap_params: Optional[Dict[str, Any]],
        delta: Any,
    ) -> float:
        """
        Turn a relative command from Yandex into an absolute value

        Yandex marks incremental commands with "relative": true and sends a
        delta, so "make it warmer" arrives as {"value": 1, "relative": true}
        and means current + 1. Without this the delta itself was published as
        the new value, which is how "make it warmer" ended up setting 1

        Only the range capability can be relative in the Yandex API, so any
        other capability is rejected rather than silently treated as absolute

        TODO: reading the current value from the broker on every command is a
              workaround, and it has two known costs:
              1. an MQTT round trip (up to RELATIVE_READ_TIMEOUT_S) is added to
                 every step, plus a short-lived connection and thread
              2. a burst of commands races with the device: after publishing
                 "warmer" the control may not have republished its state yet,
                 so the next step is computed from a stale value and the steps
                 collapse into one
              The reason it is done this way is that DeviceRegistry has no idea
              what the current value is: mqtt_on_message() drops retained
              messages by design (see main.py) and nothing keeps the live ones,
              so there is simply nothing to read from memory
              Proper fix (planned for 0.14.0) is a state cache in the registry:
              seeded with retained values when _subscribe_registry_topics()
              runs, updated from the MQTT subscription that is already active,
              and updated optimistically right after we publish so a burst of
              steps adds up correctly. The same cache also removes the blocking
              per-capability read in alice_devices_query

        Args:
            topic: Full MQTT topic of the control (without the /on suffix)
            cap_type: Yandex capability type string
            cap_params: Capability parameters from the config, may hold "range"
            delta: Signed increment from Yandex

        Returns:
            Absolute value to publish, clamped to the declared range

        Raises:
            ActionError: capability cannot be changed relatively, current value
                is unavailable, or either value is not a number
        """
        if not cap_type.endswith("range"):
            logger.warning("Relative change is not supported for %r", cap_type)
            raise ActionError(ERR_INVALID_ACTION, f"Relative change unsupported for {cap_type}")

        try:
            delta_value = float(delta)
        except (ValueError, TypeError):
            raise ActionError(ERR_INVALID_VALUE, f"Relative value is not a number: {delta!r}")

        raw = await read_retained_value(topic, timeout=RELATIVE_READ_TIMEOUT_S)
        if raw is None:
            raise ActionError(ERR_DEVICE_UNREACHABLE, f"No current value in topic {topic!r}")

        try:
            current = float(raw)
        except ValueError:
            raise ActionError(ERR_INVALID_VALUE, f"Current value is not a number: {raw!r} in {topic!r}")

        range_params = (cap_params or {}).get("range")
        target = resolve_relative_range_value(current, delta_value, range_params)
        logger.debug("Relative change of %r: %r + %r → %r", topic, current, delta_value, target)
        return target

    async def forward_yandex_to_mqtt(
        self,
        device_id: str,
        cap_type: str,
        instance: Optional[str],
        instance_value: Optional[str],
        value: Any,
        relative: bool = False,
    ) -> None:
        """
        Apply a capability action from Yandex to the mapped MQTT control

        Args:
            device_id: Device identifier from the config
            cap_type: Yandex capability type string
            instance: Capability instance (e.g. "temperature")
            instance_value: Event value for event properties, None for others
            value: Value from Yandex - a target value, or an increment when
                *relative* is set
            [relative]: Value is an increment to the current one, not a target

        Raises:
            ActionError: relative command could not be resolved
        """
        key = (device_id, cap_type, instance, instance_value)

        # TODO: unmapped keys, unknown devices and conversion failures below
        #       return silently, so Yandex is told DONE for a command that was
        #       never published - they should raise ActionError too, but that
        #       touches every capability and is left for the 0.14.0 rework
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

        # Relative command carries a delta - resolve it against the current
        # value before the usual conversion, which expects an absolute one
        if relative:
            value = await self._resolve_relative_value(base, cap_type, cap_params, value)

        # Convert value to MQTT format
        try:
            payload = self._convert_cap_to_mqtt(value, cap_type, instance, cap_params)
        except (ValueError, TypeError) as e:
            logger.warning(
                "Failed to convert Yandex→MQTT for %r (device=%r, instance=%r, value=%r): %s",
                cap_type,
                device_id,
                instance,
                value,
                e,
            )
            return None

        await self._publish_to_mqtt(cmd_topic, payload)
        logger.debug("Published %r → %r", payload, cmd_topic)

    async def _read_capability_state(self, device_id: str, cap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Read capability state from MQTT and convert to Yandex format
        """
        # Capability marked non-retrievable — skip MQTT read
        if cap.get("retrievable", True) is False:
            return None

        cap_type = cap["type"]
        instance = cap.get("parameters", {}).get("instance")
        instance, instance_value = self._extract_instance_with_value(cap)
        key = (device_id, cap_type, instance, instance_value)

        topic = self.cap_index.get(key)
        if not topic:
            logger.warning("No MQTT topic found for capability: %r", key)
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

    def _extract_instance_with_value(self, prop: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract the indexing tuple for a property configuration

        Args:
            prop: Property dictionary from device config. Expected to contain
                "type" and an optional "parameters" dict with "instance" and
                (for events) "value".

        Returns:
            Tuple[Optional[str], Optional[str]]: (instance, unit_or_event_value)
                - instance: the 'instance' parameter (e.g. "open", "temperature"), or None if not present.
                - unit_or_event_value: for event properties, the extracted event
                value (e.g. "opened"); for non-event properties, None.

        Example:
            >>> _extract_instance_with_value({"type": "devices.properties.event",
                            "parameters": {"instance": "open", "value": "value.opened"}})
            ("open", "opened")
            >>> _extract_instance_with_value({"type": "devices.properties.float",
                            "parameters": {"instance": "temperature"}})
            ("temperature", "")
        """
        prop_type = prop["type"]
        instance = prop.get("parameters", {}).get("instance")
        if not instance:
            # we have events => we have Enum values
            instance = prop.get("state", {}).get("instance")
            unit_or_event_value = prop.get("parameters", {}).get("value")
        else:
            # we have digit values
            # TODO (v.fedorov): need to check Float properties with enum values (battery_level, food_level, etc.)
            unit_or_event_value = None
        if is_property_event(prop["type"]):
            unit_or_event_value = extract_event_value(prop.get("parameters", {}).get("value"))
        return instance, unit_or_event_value

    async def _read_property_state(self, device_id: str, prop: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Asynchronously read the current state of a device property from MQTT and convert it to Yandex Smart Home format

        For event properties, returns None since they are not retrievable.

        Args:
            device_id (str): The unique identifier of the device.
            prop (Dict[str, Any]): The property configuration dictionary containing type and parameters.

        Returns:
            Optional[Dict[str, Any]]: A dictionary with 'type' and 'state' keys representing the property state in Yandex format,
            or None if the property is an event, no MQTT topic is found, or reading fails.
        Note:
            Event properties are not retrievable and will always return None.
            key for cap_index: (device_id, prop_type, instance, instance_value)
            cap_index: maps to full MQTT topic.
            instance_value is used only for event properties, for other properties it is None.
        """
        prop_type = prop["type"]
        instance, instance_value = self._extract_instance_with_value(prop)
        key = (device_id, prop_type, instance, instance_value)
        if is_property_event(prop_type):
            # Event properties cannot be queried: no local state cache and
            # events may span multiple MQTT topics
            # TODO: lift once a local state store exists (see _collect_properties)
            logger.debug("Event property not retrievable (no local state cache): %r", key)
            return None
        # Non-event property marked non-retrievable in config — skip MQTT read
        if prop.get("retrievable", True) is False:
            logger.debug("Property marked non-retrievable, skipping read: %r", key)
            return None
        topic = self.cap_index.get(key)
        if not topic:
            logger.warning("No MQTT topic found for property: %r", key)
            return None
        try:
            msg = await read_topic_once(
                topic, timeout=1, prop_type=prop_type, instance=instance, unit_or_event_value=instance_value
            )
            # TODO (victor.fedorov): Differentiate return values for errors vs events.
            #      Event properties are currently non-retrievable and therefore treated
            #      as having no stored state. We should return a different result when
            #      the device exposes only event properties than when it truly has no
            #      MQTT data (unreachable).
            #      Adding retrievable events would require
            #      either supporting single-topic implementations or introducing an
            #      intermediate storage for Yandex-formatted states.
            if msg is None:
                logger.debug("No retained payload in %r", topic)
                return None
            raw = msg.payload.decode().strip()
            try:
                value = float(raw)
            except ValueError:
                value = raw

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
        """
        Build the device state response for Yandex `/user/devices/{id}/query`

        Filter capabilities/properties Yandex may query, then read MQTT state
        for those items. Write-only and event-only devices return an empty
        response (not DEVICE_UNREACHABLE) — they are stateless by design
        """
        logger.debug("Reading current state for device: %r", device_id)

        device = self.devices.get(device_id)
        if not device:
            logger.warning("get_device_current_state: unknown device_id %r", device_id)
            return {"id": device_id, "error_code": "DEVICE_NOT_FOUND"}

        # Capability is queryable when retrievable is not explicitly false
        queryable_capabilities: List[Dict[str, Any]] = []
        for cap in device.get("capabilities", []):
            if cap.get("retrievable", True) is False:
                continue
            queryable_capabilities.append(cap)

        # Event properties have no persistent state (see _collect_properties)
        queryable_properties: List[Dict[str, Any]] = []
        for prop in device.get("properties", []):
            if is_property_event(prop.get("type")):
                continue
            if prop.get("retrievable", True) is False:
                continue
            queryable_properties.append(prop)

        # Nothing to query — stateless by design, not unreachable
        if not queryable_capabilities and not queryable_properties:
            logger.debug(
                "Device %r has no retrievable state (write-only or event-only)",
                device_id,
            )
            return {"id": device_id}

        capabilities_output: List[Dict[str, Any]] = []
        for cap in queryable_capabilities:
            logger.debug("Reading capability state: %r", cap)
            cap_state = await self._read_capability_state(device_id, cap)
            logger.debug("Capability result: %r", cap_state)
            if cap_state:
                capabilities_output.append(cap_state)

        properties_output: List[Dict[str, Any]] = []
        for prop in queryable_properties:
            logger.debug("Reading property state: %r", prop)
            prop_state = await self._read_property_state(device_id, prop)
            if prop_state:
                properties_output.append(prop_state)

        # Expected state but read nothing — device truly unreachable
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

        device_state_answer: Dict[str, Any] = {"id": device_id}
        if capabilities_output:
            device_state_answer["capabilities"] = capabilities_output
        if properties_output:
            device_state_answer["properties"] = properties_output
        return device_state_answer
