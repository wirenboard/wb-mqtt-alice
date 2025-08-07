#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Wiren Board Alice Integration Client
This script provides integration between Wiren Board controllers
and "Yandex smart home" platform with Alice

Usage:
    python3 wb-alice-client.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from typing import Any, Callable, Dict, Optional, Tuple

import paho.mqtt.client as mqtt_client
import paho.mqtt.subscribe as subscribe
import socketio

from mqtt_topic import MQTTTopic

logging.basicConfig(level=logging.DEBUG, force=True)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)

logger.info("socketio module path: %s", socketio.__file__)
from importlib.metadata import PackageNotFoundError, version

try:
    logger.info("python-socketio version: %s", version("python-socketio"))
except PackageNotFoundError:
    logger.warning("python-socketio is not installed.")

# Configuration file paths
SHORT_SN_PATH = "/var/lib/wirenboard/short_sn.conf"
CONFIG_PATH = "/etc/wb-alice-client.conf"


class AppContext:
    def __init__(self):
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None
        """
        Global asyncio event loop, used to safely schedule coroutines from non-async
        threads (e.g. MQTT callbacks) via `asyncio.run_coroutine_threadsafe()`
        """

        self.stop_event: Optional[asyncio.Event] = None
        """
        Event that signals the main loop to wake up and initiate shutdown
        """

        self.sio: Optional[socketio.AsyncClient] = None
        """
        SocketIO async client instance for handling real-time communication
        """

        self.registry: Optional[DeviceRegistry] = None
        self.mqtt_client: Optional[mqtt_client.Client] = None
        self.controller_sn: Optional[str] = None


ctx = AppContext()


def _emit_async(event: str, data: dict) -> None:
    """
    Safely schedules a Socket.IO event to be emitted
    from any thread (async or not).
    """
    if not ctx.sio or not ctx.sio.connected:
        logger.warning("[SOCKET.IO] Not connected, skipping emit '%s'", event)
        logger.debug("            Payload: %s", json.dumps(data))
        return
    logger.debug("[SOCKET.IO] Connected status: %s", ctx.sio.connected)

    if not hasattr(ctx.sio, "namespaces") or "/" not in ctx.sio.namespaces:
        logger.warning("[SOCKET.IO] Namespace not ready, skipping emit '%s'", event)
        return

    # BUG: Additional check for version SocketIO 5.0.3 (may delete when upgrade)
    if hasattr(ctx.sio.eio, "write_loop_task") and ctx.sio.eio.write_loop_task is None:
        logger.warning(
            "[SOCKET.IO] Write loop task is None, connection unstable - skipping emit '%s'",
            event,
        )
        return

    logger.debug("[SOCKET.IO] Attempting to emit '%s' with payload: %s", event, data)

    try:
        # We're in an asyncio thread – safe to call create_task directly
        asyncio.get_running_loop()
        asyncio.create_task(ctx.sio.emit(event, data))
        logger.debug("[SOCKET.IO] Scheduled emit '%s' via asyncio task", event)

    except RuntimeError:
        # No running loop in current thread – fallback to ctx.main_loop
        logger.debug(
            "[SOCKET.IO] No running loop in current thread – using ctx.main_loop"
        )

        if ctx.main_loop is None:
            logger.warning(
                "[SOCKET.IO] ctx.main_loop not available – dropping event '%s'", event
            )
            return

        if ctx.main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(
                ctx.sio.emit(event, data), ctx.main_loop
            )

            # Log if the future raises an exception
            def log_emit_exception(f: asyncio.Future):
                exc = f.exception()
                if exc:
                    logger.error(
                        "[SOCKET.IO] Emit '%s' failed: %s", event, exc, exc_info=True
                    )

            fut.add_done_callback(log_emit_exception)
        else:
            logger.error(
                "[SOCKET.IO] ctx.main_loop is not running – cannot emit '%s'", event
            )


async def read_topic_once(
    topic: str, *, host: str = "localhost", retain: bool = True, timeout: float = 2.0
):
    """
    Reads a single retained MQTT message in a separate thread
    Returns paho.mqtt.client.MQTTMessage or None on timeout
    """
    logger.debug(
        "[read_topic_once] wait %s message on '%s' (retain=%s, %.1fs)",
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
        logger.warning("[read_topic_once] timeout waiting '%s'", topic)
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
        publish_to_mqtt: Callable[[str, str], None],
    ) -> None:
        self._send_to_yandex = send_to_yandex
        self._publish_to_mqtt = publish_to_mqtt

        self.devices: dict[str, dict] = {}  # id → full json block
        self.topic2info: dict[str, Tuple[str, str, int]] = {}
        self.cap_index: dict[Tuple[str, str, Optional[str]], str] = {}
        self.rooms: dict[str, dict] = {}  # room_id → block

        self._load_config(cfg_path)

    # ---------- config loader ----------
    def _load_config(self, path: str) -> None:
        """
        Read device configuration file and populate internal structures
        - self.devices: full json device description
        - self.topic2info: full_topic → (device_id, 'capabilities' / 'properties', index)
        - self.cap_index: 'capabilities' / 'properties'(device_id, type, instance) → full_topic
        """

        logger.info(f"[REG] Try read config file '{path}'")
        try:
            with open(path, "r") as f:
                config_data = json.load(f)
                logger.info(
                    f"[REG] Config loaded: '{json.dumps(config_data, indent=2, ensure_ascii=False)}'"
                )
        except FileNotFoundError:
            logger.error(f"[REG] Config file not found: {path}")
            self.devices = {}
            self.topic2info = {}
            self.cap_index = {}
            self.rooms = {}
            return
        except json.JSONDecodeError as e:
            logger.error(f"[REG] Invalid JSON in config: {e}")
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
                self.cap_index[
                    (
                        device_id,
                        prop["type"],
                        prop.get("parameters", {}).get("instance"),
                    )
                ] = full

        logger.info(
            f"[REG] Devices loaded: {len(self.devices)}, "
            f"mqtt topics: {len(self.topic2info)}"
        )

    def build_yandex_devices_list(self) -> list[dict]:
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

        devices_out: list[dict] = []

        for dev_id, dev in self.devices.items():
            room_name = ""
            room_id = dev.get("room_id")
            if room_id and room_id in self.rooms:
                room_name = self.rooms[room_id].get("name", "")

            device: dict[str, Any] = {
                "id": dev_id,
                "name": dev.get("name", dev_id),
                "status_info": dev.get("status_info", {"reportable": False}),
                "description": dev.get("description", ""),
                "room": room_name,
                "type": dev["type"],
            }

            # ---- capabilities ----
            caps = []
            for cap in dev.get("capabilities", []):
                caps.append(
                    {
                        "type": cap["type"],
                        "retrievable": True,
                    }
                )
            if caps:
                device["capabilities"] = caps

            # ---- properties ----
            props = []
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
                logger.warning(f"[REG] Can't convert '{raw}' to float")
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
            logger.warning(f"[REG] No mapping for {key}")
            return

        base = self.cap_index[key]  # already full topic
        # Handle topics with or without '/on' suffix
        cmd_topic = base if base.endswith("/on") else f"{base}/on"

        if cap_type.endswith("on_off"):
            payload = "1" if value else "0"
        else:
            payload = str(value)

        self._publish_to_mqtt(cmd_topic, payload)
        logger.debug(f"[REG] Published '{payload}' → {cmd_topic}")

    async def _read_capability_state(self, device_id: str, cap: dict) -> Optional[dict]:
        cap_type = cap["type"]
        instance = cap.get("parameters", {}).get("instance")
        key = (device_id, cap_type, instance)

        topic = self.cap_index.get(key)
        if not topic:
            logger.debug(f"[REG] No MQTT topic found for capability: {key}")
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
            logger.debug(f"[REG] Failed to read capability topic '{topic}': {e}")
            return None

    async def _read_property_state(self, device_id: str, prop: dict) -> Optional[dict]:
        prop_type = prop["type"]
        instance = prop.get("parameters", {}).get("instance")
        key = (device_id, prop_type, instance)

        topic = self.cap_index.get(key)
        if not topic:
            logger.debug(f"[REG] No MQTT topic found for property: {key}")
            return None
        try:
            msg = await read_topic_once(topic, timeout=1)
            if msg is None:
                logger.debug(f"[REG] No retained payload in '{topic}'")
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
            logger.warning(f"[REG] Failed to read property topic '{topic}': {e}")
            return None

    async def get_device_current_state(self, device_id: str) -> dict:
        device = self.devices.get(device_id)
        if not device:
            logger.warning(
                f"[REG] get_device_current_state: unknown device_id '{device_id}'"
            )
            return {"id": device_id, "error_code": "DEVICE_NOT_FOUND"}

        capabilities_output: list[dict] = []
        properties_output: list[dict] = []

        for cap in device.get("capabilities", []):
            logger.debug(f"[SOCKET.IO] Reading capability state: '%s'", cap)
            cap_state = await self._read_capability_state(device_id, cap)
            if cap_state:
                capabilities_output.append(cap_state)

        for prop in device.get("properties", []):
            logger.debug(f"[SOCKET.IO] Reading property state: '%s'", prop)
            prop_state = await self._read_property_state(device_id, prop)
            if prop_state:
                properties_output.append(prop_state)

        # If nothing was read - mark as unreachable
        if not capabilities_output and not properties_output:
            logger.warning(
                "[REG] %s: no live or retained data — marking DEVICE_UNREACHABLE",
                device_id,
            )
            return {
                "id": device_id,
                "error_code": "DEVICE_UNREACHABLE",
                "error_message": "MQTT topics unavailable",
            }

        # If at least something was read - return it
        device_output = {"id": device_id}
        if capabilities_output:
            device_output["capabilities"] = capabilities_output
        if properties_output:
            device_output["properties"] = properties_output

        return device_output


def publish_to_mqtt(topic: str, payload: str) -> None:
    """
    Helper for publishing from registry
    """
    global ctx
    if ctx.mqtt_client is None:
        logger.error("[MQTT] Client not initialized")
        return

    if not ctx.mqtt_client.is_connected():
        logger.warning("[MQTT] Client not connected, dropping message to %s", topic)
        return
    try:
        ctx.mqtt_client.publish(topic, payload)
    except Exception as e:
        logger.error("[MQTT] Failed to publish to %s: %s", topic, e)


# ---------------------------------------------------------------------
# Yandex types handlers
# ---------------------------------------------------------------------


def _to_bool(raw_state: Any) -> bool:
    """
    Conversion to bool according to Yandex on_off rules
    """
    if isinstance(raw_state, bool):
        return raw_state
    elif isinstance(raw_state, (int, float)):
        return raw_state != 0
    elif isinstance(raw_state, str):
        raw_state = raw_state.strip().lower()
        if raw_state in {"1", "true", "on"}:
            return True
        elif raw_state in {"0", "false", "off"}:
            return False
        elif raw_state.isdigit():
            return int(raw_state) != 0
        else:
            logger.debug("[to_bool] Unknown string %s → False", raw_state)
            return False
    else:
        logger.debug("[to_bool] Unknown value type %s → False", type(raw_state))
        return False


def _to_float(raw: Any) -> float:
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.debug("[to_float] Cannot convert %s to float → 0.0", raw)
        return 0.0


def _on_off(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, "devices.capabilities.on_off", instance, _to_bool(value)
    )


def _float_prop(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, "devices.properties.float", instance, _to_float(value)
    )


def _not_implemented(cap_type: str) -> Callable[..., None]:
    def _stub(*_a, **_kw) -> None:
        raise NotImplementedError(f"Handler for '{cap_type}' is not implemented yet")

    return _stub


# Yandex types handler table for select send logic from MQTT to Yandex
_HANDLERS: Dict[str, Callable[[str, Optional[str], Any], None]] = {
    # Capabilities
    "devices.capabilities.on_off": _on_off,
    "devices.capabilities.color_setting": _not_implemented("cap.color_setting"),
    "devices.capabilities.video_stream": _not_implemented("cap.video_stream"),
    "devices.capabilities.mode": _not_implemented("cap.mode"),
    "devices.capabilities.range": _not_implemented("cap.range"),
    "devices.capabilities.toggle": _not_implemented("cap.toggle"),
    # Properties
    "devices.properties.float": _float_prop,
    "devices.properties.event": _not_implemented("prop.event"),
}


def _build_state_block(
    block_type: str,
    instance: Optional[str],
    value: Any,
    *,
    is_property: bool,
) -> dict:
    """
    Builds either a capability **or** a property state block depending
    on *is_property* flag
    """
    key = "properties" if is_property else "capabilities"
    state_key = {
        "type": block_type,
        "state": {"instance": instance, "value": value},
    }
    return {key: [state_key]}


def send_state_to_server(
    device_id: str,
    block_type: str,
    instance: Optional[str],
    value: Any,
) -> None:
    """
    Pushes **any** single capability / property
    state update to the cloud proxy via Socket.IO
    """
    is_prop = block_type.startswith("devices.properties")

    # normalise known value types
    if block_type.endswith("on_off"):
        value = _to_bool(value)
    elif block_type.endswith("float"):
        value = _to_float(value)

    payload = {
        "ts": int(time.time()),
        "payload": {
            # user_id will be added by the proxy
            "devices": [
                {
                    "id": device_id,
                    "status": "online",
                    **_build_state_block(
                        block_type, instance, value, is_property=is_prop
                    ),
                }
            ]
        },
    }
    try:
        _emit_async("device_state", payload)
    except Exception:
        logging.exception("Error when processing MQTT message")


def send_to_yandex_state(
    device_id: str, cap_type: str, instance: Optional[str], value: Any
) -> None:
    """
    Unified sender to Yandex used by registry
    """
    handler = _HANDLERS.get(cap_type)
    if handler is None:
        logger.error(
            "[YANDEX] Unknown capability/property '%s'. Supported: %s",
            cap_type,
            ", ".join(_HANDLERS.keys()),
        )
        return

    try:
        handler(device_id, instance, value)
    except NotImplementedError as err:
        # Explicit and loud: capability exists in table but lacks implementation
        logger.error("[YANDEX] %s", err)
    except Exception as exc:
        logger.exception("[YANDEX] Handler error for '%s': %s", cap_type, exc)


# ---------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------


def mqtt_on_connect(client, userdata, flags, rc):
    if rc != 0:
        logger.error(f"[MQTT] Connection failed with code: {rc}")
        return

    # Check if registry is ready
    if ctx.registry is None or not hasattr(ctx.registry, "topic2info"):
        logger.error("[MQTT] Registry not ready, no topics to subscribe")
        return

    # subscribe to every topic from registry
    for t in ctx.registry.topic2info.keys():
        client.subscribe(t, qos=0)
        logger.info(f"[MQTT] Subscribed to {t}")


def mqtt_on_disconnect(client, userdata, rc):
    logger.warning("[MQTT] Disconnected with code %s", rc)


def mqtt_on_message(client, userdata, message):
    if ctx.registry is None:
        logger.debug("[MQTT] Registry not available, ignoring message")
        return

    topic_str = message.topic
    try:
        payload_str = message.payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        logger.warning("[MQTT] Cannot decode payload in topic '%s'", message.topic)
        logger.debug("[MQTT] Raw bytes: %s", message.payload)
        return

    logger.debug(f"[MQTT] Incoming from topic '{topic_str}':")
    logger.debug(f"       - Size   : '{len(message.payload)}'")
    logger.debug(f"       - Message: '{payload_str}'")

    ctx.registry.forward_mqtt_to_yandex(topic_str, payload_str)


def generate_client_id(prefix: str = "wb-alice-client") -> str:
    """
    Generate unique MQTT client ID with random suffix
    """
    import random
    import string

    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return f"{prefix}-{suffix}"


ctx.mqtt_client = mqtt_client.Client(client_id=generate_client_id())
ctx.mqtt_client.on_connect = mqtt_on_connect
ctx.mqtt_client.on_disconnect = mqtt_on_disconnect
ctx.mqtt_client.on_message = mqtt_on_message


async def read_mqtt_state(
    topic: str, mqtt_host="localhost", timeout=1
) -> Optional[bool]:
    """
    Reads the value of a topic (0/1, "false"/"true", etc.) and returns a Python bool
    Uses subscribe.simple(...) from paho.mqtt, which BLOCKS for the duration of reading
    """
    logger.debug("[read_mqtt_state] trying to read topic: %s", topic)

    try:
        msg = await read_topic_once(topic, timeout=timeout)
        if msg is None:
            logger.debug("[REG] No retained payload in '%s'", topic)
            return None
    except Exception as e:
        logger.warning("[REG] Failed to read topic '%s': %s", topic, e)
        return None

    payload_str = msg.payload.decode().strip().lower()

    # Interpret different payload variants
    if payload_str in {"1", "true", "on"}:
        return True
    elif payload_str in {"0", "false", "off"}:
        return False
    else:
        logger.warning(
            "[WARN] Unexpected payload in topic '%s': %s", topic, payload_str
        )
        return False


def write_mqtt_state(mqtt_client: mqtt_client.Client, topic: str, is_on: bool) -> None:
    """
    Publishes "1" (True) or "0" (False) to the given topic
    """
    payload_str = "1" if is_on else "0"
    full_topic = f"{topic}/on"
    mqtt_client.publish(full_topic, payload_str)
    logger.debug("[MQTT] Published '%s' to '%s'", payload_str, full_topic)


# ---------------------------------------------------------------------
# SocketIO callbacks
# ---------------------------------------------------------------------


async def connect():
    global ctx
    logger.info("[SUCCESS] Connected to Socket.IO server!")
    await ctx.sio.emit(
        "message", {"controller_sn": ctx.controller_sn, "status": "online"}
    )


async def disconnect():
    """
    Triggered when SocketIO connection with server is lost

    NOTE: argument "reason" implemented in version 5.12, but not accessible
    in current client 5.0.3 (Released in Dec 14,2020)
    """
    logger.warning("[DISCONNECT] Lost connection")


async def response(data):
    logger.info(f"[INCOME] Server response: {data}")


async def error(data):
    logger.info(f"[SOCKETIO] Server error: {data}")


async def connect_error(data: dict[str, Any]) -> None:
    """
    Called when initial connection to server fails
    """
    logger.warning("[SOCKET.IO] Connection refused by server: %s", data)


async def any_unprocessed_event(event, sid, data):
    """
    Fallback handler for Socket.IO events that don't have specific handlers
    """
    logger.info(f"[Socket.IO/ANY] Not handled event {event}")


async def on_alice_devices_list(data: dict[str, Any]) -> dict[str, Any]:
    """
    Handles a device discovery request from the server
    Returns a list of devices defined in the controller config
    """
    logger.debug("[SOCKET.IO] Received 'alice_devices_list' event:")
    logger.debug(json.dumps(data, ensure_ascii=False, indent=2))
    req_id: str = data.get("request_id")

    if ctx.registry is None:
        logger.error("[SOCKET.IO] Registry not available for device list")
        return {"request_id": req_id, "payload": {"devices": []}}

    devices_list: list[dict[str, Any]] = ctx.registry.build_yandex_devices_list()
    if not devices_list:
        logger.warning("[SOCKET.IO] No devices found in configuration")

    devices_response: dict[str, Any] = {
        "request_id": req_id,
        "payload": {
            # "user_id" will be added on the server proxy side
            "devices": devices_list,
        },
    }

    logger.info("[SOCKET.IO] Sending device list response to Yandex:")
    logger.info(json.dumps(devices_response, ensure_ascii=False, indent=2))
    return devices_response


async def on_alice_devices_query(data):
    """
    Handles a Yandex request to retrieve the current state of devices.
    """
    logger.info("[SOCKET.IO] alice_devices_query event:")
    logger.info(json.dumps(data, ensure_ascii=False, indent=2))

    request_id = data.get("request_id", "unknown")
    devices_response = []

    for dev in data.get("devices", []):
        device_id = dev.get("id")
        logger.info(f"[SOCKET.IO] Try get data for device: '{device_id}'")
        devices_response.append(await ctx.registry.get_device_current_state(device_id))

    query_response = {
        "request_id": request_id,
        "payload": {
            "devices": devices_response,
        },
    }

    logger.info("[SOCKET.IO] answer devices query to Yandex:")
    logger.info(json.dumps(query_response, ensure_ascii=False, indent=2))
    return query_response


def handle_single_device_action(device: dict[str, Any]) -> dict[str, Any]:
    """
    Processes all capabilities for a single device and returns the result block,
    formatted according to Yandex Smart Home action response spec.
    """
    device_id: str = device.get("id", "")
    if not device_id:
        logger.warning("[SOCKET.IO] Device block missing 'id': %s", device)
        return {}

    cap_results: list[dict[str, Any]] = []
    for cap in device.get("capabilities", []):
        cap_type: str = cap.get("type")
        instance: str = cap.get("state", {}).get("instance")
        value: Any = cap.get("state", {}).get("value")

        try:
            ctx.registry.forward_yandex_to_mqtt(device_id, cap_type, instance, value)
            logger.info(
                "[SOCKET.IO] Action applied to %s: %s = %s", device_id, instance, value
            )
            status = "DONE"
        except Exception as e:
            logger.exception(
                "[SOCKET.IO] Failed to apply action for device '%s'", device_id
            )
            status = "ERROR"

        cap_results.append(
            {
                "type": cap_type,
                "state": {
                    "instance": instance,
                    "action_result": {"status": status},
                },
            }
        )

    return {
        "id": device_id,
        "capabilities": cap_results,
    }


async def on_alice_devices_action(data: dict[str, Any]) -> dict[str, Any]:
    """
    Handles a device action request from Yandex (e.g., turn on/off)
    Applies the command to the device and returns the result
    """
    logger.debug("[SOCKET.IO] Received 'alice_devices_action' event")

    logger.debug("[SOCKET.IO] Full payload:")
    logger.debug(json.dumps(data, ensure_ascii=False, indent=2))

    request_id: str = data.get("request_id", "unknown")
    devices_in: list[dict[str, Any]] = data.get("payload", {}).get("devices", [])
    devices_info: list[dict[str, Any]] = []

    for device in devices_in:
        result = handle_single_device_action(device)
        if result:
            devices_info.append(result)

    action_response: dict[str, Any] = {
        "request_id": request_id,
        "payload": {"devices": devices_info},
    }

    logger.debug("[SOCKET.IO] Sending action response:")
    logger.debug(json.dumps(action_response, ensure_ascii=False, indent=2))
    return action_response


def bind_socketio_handlers(sock: socketio.AsyncClient):
    """
    Bind event handlers to the SocketIO client

    Unlike decorators, we use .on() method for better safety - this approach
    helps control all names and objects at any time and in any context
    """
    sock.on("connect", connect)
    sock.on("disconnect", disconnect)
    sock.on("response", response)
    sock.on("error", error)
    sock.on("connect_error", connect_error)
    sock.on("alice_devices_list", on_alice_devices_list)
    sock.on("alice_devices_query", on_alice_devices_query)
    sock.on("alice_devices_action", on_alice_devices_action)
    sock.on("*", any_unprocessed_event)  # Handle any unprocessed events


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------


def get_controller_sn():
    """
    Get controller ID from the configuration file
    """
    try:
        with open(SHORT_SN_PATH, "r") as file:
            controller_sn = file.read().strip()
            logger.info(f"Read controller ID: {controller_sn}")
            return controller_sn
    except FileNotFoundError:
        logger.error(f"Controller ID file not found! Check the path: {SHORT_SN_PATH}")
        return None
    except Exception as e:
        logger.error(f"Reading controller ID exception: {e}")
        return None


def read_config():
    """
    Read configuration from file which is generated by WEBUI
    """
    try:
        if not os.path.exists(CONFIG_PATH):
            logger.error(f"Configuration file not found at {CONFIG_PATH}")
            return None

        with open(CONFIG_PATH, "r") as file:
            config = json.load(file)
            return config
    except json.JSONDecodeError:
        logger.error("Parsing configuration file: Invalid JSON format")
        return None
    except Exception as e:
        logger.error(f"Reading configuration exception: {e}")
        return None


async def connect_controller(sock: socketio.AsyncClient):
    global ctx

    config = read_config()
    if not config:
        logger.error("Cannot proceed without configuration")
        return

    if not config.get("is_registered", False):
        logger.error(
            "Controller is not registered. Please register the controller first"
        )
        return

    ctx.controller_sn = get_controller_sn()
    if not ctx.controller_sn:
        logger.error("Cannot proceed without controller ID")
        return

    # ARCHITECTURE NOTE: We always connect to localhost:8042 where Nginx proxy runs.
    # Nginx forwards requests to the actual server specified in 'server_address'.
    # This allows for:
    # - SSL termination at Nginx level
    # - Certificate-based authentication
    # See configure-nginx-proxy.sh for Nginx configuration details.
    LOCAL_PROXY_URL = "http://localhost:8042"
    server_address = config.get("server_address")  # Used by Nginx proxy
    if not server_address:
        logger.error("'server_address' not specified in configuration")
        return
    logger.info(f"Target SocketIO server: {server_address}")
    logger.info(f"Connecting via Nginx proxy: {LOCAL_PROXY_URL}")

    try:
        # Connect to local Nginx proxy which forwards to actual server
        await sock.connect(
            LOCAL_PROXY_URL,
            socketio_path="/socket.io",
            # controller_sn is passed via SSL certificate when Nginx proxies
        )
        logger.info("Socket.IO connected successfully via proxy")

    except socketio.exceptions.ConnectionError as e:
        logger.error(f"Socket.IO connection error: {e}")
        # Unable to connect
        # - The controller might have been unregistered
        # - Or Server may have error or offline
        # ACTION - do reconnection
    except Exception as e:
        logger.exception(f"Unexpected exception during connection: {e}")
        # ACTION - do reconnection


def _log_and_stop(sig: signal.Signals) -> None:
    """
    Generic signal handler:
    1) logs which signal was received;
    2) sets the global ctx.stop_event so the main loop can exit.
    Idempotent: repeated signals after the first one do nothing.
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    logger.warning("[SIGNAL] %s received at %s – shutting down…", sig.name, ts)

    # ctx.stop_event is created in main() before signal handlers are registered,
    # but we keep the guard just in case.
    if ctx.stop_event is not None and not ctx.stop_event.is_set():
        ctx.stop_event.set()


async def main() -> None:
    global ctx
    ctx.main_loop = asyncio.get_running_loop()

    ctx.stop_event = asyncio.Event()  # keeps the loop alive until a signal arrives
    ctx.main_loop.add_signal_handler(signal.SIGINT, _log_and_stop, signal.SIGINT)
    ctx.main_loop.add_signal_handler(signal.SIGTERM, _log_and_stop, signal.SIGTERM)

    try:
        ctx.registry = DeviceRegistry(
            "/etc/wb-alice-devices.conf",
            send_to_yandex=send_to_yandex_state,
            publish_to_mqtt=publish_to_mqtt,
        )
        logger.info(f"[REG] Registry created with {len(ctx.registry.devices)} devices")
    except Exception as e:
        logger.error(f"[REG] Failed to create registry: {e}")
        logger.info("[REG] Continuing without device configuration")
        ctx.registry = None

    # Connect to local MQTT broker (assuming Wiren Board default: localhost:1883)
    try:
        ctx.mqtt_client.connect("localhost", 1883, 60)
        ctx.mqtt_client.loop_start()
        logger.info("Connected to local MQTT broker")
    except Exception as e:
        logger.error(f"MQTT connect failed: {e}")
        return

    ctx.sio = socketio.AsyncClient(
        logger=True,
        engineio_logger=True,
        reconnection=True,  # auto-reconnect ON
        reconnection_attempts=0,  # 0 = infinite retries
        reconnection_delay=2,  # first delay 2 s
        reconnection_delay_max=30,  # cap at 30 s
        randomization_factor=0.5,  # jitter
    )

    # Explicitly set the loop to avoid "attached to a different loop" errors
    ctx.sio._loop = ctx.main_loop
    bind_socketio_handlers(ctx.sio)

    logger.info("Connecting Socket.IO client...")
    await connect_controller(ctx.sio)
    sio_task = asyncio.create_task(ctx.sio.wait())

    # Wait for shutdown signal
    await ctx.stop_event.wait()
    logger.info("Shutdown signal received")

    logger.info("Stopping Socket.IO client ...")
    if ctx.sio.connected:
        await ctx.sio.disconnect()
    if not sio_task.done():
        sio_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sio_task

    # Cancel any remaining asyncio tasks
    pending = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    logger.info("[EXIT] Cancelling %d pending tasks…", len(pending))
    for task in pending:
        task.cancel()

    # Gather only if something is pending
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info("[EXIT] %d tasks cancelled", len(pending))

    logger.info("Stopping MQTT client")
    ctx.mqtt_client.loop_stop()
    ctx.mqtt_client.disconnect()
    logger.info("MQTT disconnected")

    logger.info("Shutdown complete")


if __name__ == "__main__":
    logger.info("Starting wb-alice-client...")

    try:
        asyncio.run(main(), debug=True)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C)")
    except SystemExit as e:
        logger.warning("System exit with code %s", e.code)
    except Exception as e:
        logger.exception("Unhandled exception: %s", e)
    finally:
        logger.info("wb-alice-client stopped")
