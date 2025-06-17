#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Wiren Board Alice Integration Client
This script provides integration between Wiren Board controllers
and "Yandex smart home" platform with Alice

Usage:
    python3 wb-alice-client.py
"""
import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from typing import Any, Callable, Optional, Tuple

import paho.mqtt.client as mqtt
import paho.mqtt.subscribe as subscribe
import socketio
from wb_common.mqtt_client import MQTTClient

from mqtt_topic import MQTTTopic

logging.basicConfig(level=logging.DEBUG, force=True)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)

logger.info("socketio module path: %s", socketio.__file__)
from importlib.metadata import PackageNotFoundError, version

try:
    import logging

    logger = logging.getLogger(__name__)
    logger.info("python-socketio version: %s", version("python-socketio"))
except PackageNotFoundError:
    logger.warning("python-socketio is not installed.")

# Configuration file paths
SHORT_SN_PATH = "/var/lib/wirenboard/short_sn.conf"
CONFIG_PATH = "/etc/wb-alice-client.conf"

MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
"""
Global asyncio event loop, used to safely schedule coroutines from non-async
threads (e.g. MQTT callbacks) via `asyncio.run_coroutine_threadsafe()`
"""

# Событие, которое «будит» основной цикл и инициирует остановку
stop_event: Optional[asyncio.Event] = None

sio = socketio.AsyncClient(
    logger=True,
    engineio_logger=True,
    reconnection=True,  # auto-reconnect ON
    reconnection_attempts=0,  # 0 = infinite retries
    reconnection_delay=2,  # first delay 2 s
    reconnection_delay_max=30,  # cap at 30 s
    randomization_factor=0.5,  # jitter
)
for name in ("engineio", "socketio"):
    logging.getLogger(name).setLevel(logging.DEBUG)

controller_sn: Optional[str] = None


def _emit_async(event: str, data: dict) -> None:
    """
    Safely schedules a Socket.IO event to be emitted
    from any thread (async or not).
    """
    logger.info("[SOCKET.IO] Connected status: %s", sio.connected)

    if not sio.connected:
        logger.warning("[SOCKET.IO] Not connected, skipping emit '%s'", event)
        logger.warning("            Payload: %s", json.dumps(data))
        return

    logger.info("[SOCKET.IO] Attempting to emit '%s' with payload: %s", event, data)

    try:
        # We're in an asyncio thread – safe to call create_task directly
        asyncio.get_running_loop()
        asyncio.create_task(sio.emit(event, data))
        logger.debug("[SOCKET.IO] Scheduled emit '%s' via asyncio task", event)

    except RuntimeError:
        # No running loop in current thread – fallback to MAIN_LOOP
        logger.debug("[SOCKET.IO] No running loop in current thread – using MAIN_LOOP")

        if MAIN_LOOP is None:
            logger.warning(
                "[SOCKET.IO] MAIN_LOOP not available – dropping event '%s'", event
            )
            return

        if MAIN_LOOP.is_running():
            fut = asyncio.run_coroutine_threadsafe(sio.emit(event, data), MAIN_LOOP)

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
                "[SOCKET.IO] MAIN_LOOP is not running – cannot emit '%s'", event
            )


async def read_topic_once(
    topic: str, *, host: str = "localhost", retain: bool = True, timeout: float = 2.0
):
    """
    Читает одно retained-сообщение из MQTT в отдельном потоке.
    Возвращает paho.mqtt.client.MQTTMessage либо None при тайм-ауте.
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
        logger.info("Current topic '%s' state: '%s'" % (topic, res))
        return res
    except asyncio.TimeoutError:
        logger.warning("[read_topic_once] timeout waiting '%s'", topic)
        return None


class DeviceRegistry:
    """Parses WB config and routes MQTT ↔ Yandex"""

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
        """Reads /etc/wb-alice-devices.conf and fills:
        • self.devices        – полный json-блок устройства
        • self.topic2info     – full_topic → (device_id, 'capabilities' / 'properties', index)
        • self.cap_index      – 'capabilities' / 'properties'(device_id, type, instance) → full_topic
        """

        logger.info(f"[REG] Try read config file '{path}'")
        with open(path, "r") as f:
            config_data = json.load(f)
            logger.info(
                f"[REG] Readed data from config file '{json.dumps(config_data, indent=2, ensure_ascii=False)}'"
            )

        self.rooms = config_data.get("rooms", {})
        devices_config = config_data.get("devices", {})
        for device_id, device_data in devices_config.items():
            self.devices[device_id] = device_data

            for i, cap in enumerate(device_data.get("capabilities", [])):
                mqtt_topic = MQTTTopic(cap["mqtt"])  # convert once
                full = mqtt_topic.full  # always full form
                self.topic2info[full] = (device_id, "capabilities", i)
                inst = cap.get("instance")

                # TODO: Add all correct instance types for each capabilities
                #       https://yandex.ru/dev/dialogs/smart-home/doc/en/concepts/capability-types
                if cap["type"].endswith("on_off") and not inst:
                    # Для on/off по спецификации Яндекса instance == "on"
                    self.cap_index[(device_id, cap["type"], "on")] = full
                else:
                    self.cap_index[(device_id, cap["type"], inst)] = full

            for i, prop in enumerate(device_data.get("properties", [])):
                mqtt_topic = MQTTTopic(prop["mqtt"])
                full = mqtt_topic.full
                self.topic2info[full] = (device_id, "properties", i)
                self.cap_index[(device_id, prop["type"], prop.get("instance"))] = full

        logger.info(
            f"[REG] Devices loaded: {len(self.devices)}, "
            f"mqtt topics: {len(self.topic2info)}"
        )

    def build_yandex_devices_list(self) -> list[dict]:
        """
        Формирует массив `devices` в формате, который ожидает
        Яндекс-Диалоги в ответе /user/devices ( discovery ).
        """
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
                # добавим parameters, если знаем instance
                instance = prop.get("instance")
                if instance or prop["type"].endswith("float"):
                    prop_obj["parameters"] = {
                        "instance": instance or "temperature",
                        "unit": "unit.temperature.celsius",
                    }
                props.append(prop_obj)
            if props:
                device["properties"] = props

            devices_out.append(device)

        return devices_out

    # ---------- MQTT → Yandex ----------
    def handle_mqtt(self, topic: str, raw: str) -> None:
        if topic not in self.topic2info:
            return

        device_id, section, idx = self.topic2info[topic]
        blk = self.devices[device_id][section][idx]

        cap_type = blk["type"]
        instance = blk.get("instance")

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

    # ---------- Yandex → MQTT ----------
    def handle_action(
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
        # Check what user write topic with or without '\on'
        cmd_topic = base if base.endswith("/on") else f"{base}/on"

        if cap_type.endswith("on_off"):
            payload = "1" if value else "0"
        else:
            payload = str(value)

        self._publish_to_mqtt(cmd_topic, payload)
        logger.info(f"[REG] Published '{payload}' → {cmd_topic}")

    async def _read_capability_state(self, device_id: str, cap: dict) -> Optional[dict]:
        key = (device_id, cap["type"], cap.get("instance"))
        topic = self.cap_index.get(key)
        if not topic:
            logger.debug(f"[REG] No MQTT topic found for capability: {key}")
            return None
        try:
            value = await read_mqtt_state(topic, mqtt_host="localhost")

            # Normalize boolean values for on_off capabilities
            if cap["type"].endswith("on_off"):
                value = bool(value)

            return {
                "type": cap["type"],
                "state": {
                    "instance": cap.get("instance", "on"),
                    "value": value,
                },
            }
        except Exception as e:
            logger.debug(f"[REG] Failed to read capability topic '{topic}': {e}")
            return None

    async def _read_property_state(self, device_id: str, prop: dict) -> Optional[dict]:
        key = (device_id, prop["type"], prop.get("instance"))
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
                "type": prop["type"],
                "state": {
                    "instance": prop.get("instance", "temperature"),
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

        # TODO: тут очень важно проверить есть ли вообще такой топик
        #       так же важно понимать retained ли топик или нет.
        #       очень желательно чтобы все топики были retain

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

        # ► Если ничего не прочитали – ошибка
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

        # ► Если хотя бы что-то есть – возвращаем
        device_output = {"id": device_id}
        if capabilities_output:
            device_output["capabilities"] = capabilities_output
        if properties_output:
            device_output["properties"] = properties_output

        return device_output


# Helper for publishing from registry
def publish_to_mqtt(topic: str, payload: str) -> None:
    mqtt_client.publish(topic, payload)


def to_bool(raw_state: Any) -> bool:
    """Conversion to bool according to Yandex on_off rules"""
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


def to_float(raw: Any) -> float:
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.debug("[to_float] Cannot convert %s to float → 0.0", raw)
        return 0.0


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
        value = to_bool(value)
    elif block_type.endswith("float"):
        value = to_float(value)

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
    if cap_type == "devices.capabilities.on_off":
        send_state_to_server(
            device_id,
            "devices.capabilities.on_off",
            "on",
            value,
        )
    elif cap_type == "devices.properties.float":
        send_state_to_server(
            device_id,
            "devices.properties.float",
            "temperature",
            value,
        )
    else:
        logger.info(f"[YANDEX] TODO sender for {cap_type} (instance={instance})")


def on_connect(client, userdata, flags, rc):
    logger.info(f"[MQTT] Connected with code: {rc}")
    # subscribe to every topic from registry
    for t in registry.topic2info.keys():
        client.subscribe(t, qos=0)
        logger.info(f"[MQTT] Subscribed to {t}")


def on_message(client, userdata, message):
    topic_str = message.topic
    payload_str = message.payload.decode().strip()
    logger.info(f"[MQTT] Incoming from topic '{topic_str}':")
    logger.info(f"       - Size   : '{len(message.payload)}'")
    logger.info(f"       - Message: '{payload_str}'")
    # delegate parsing to registry
    registry.handle_mqtt(topic_str, payload_str)


mqtt_client = MQTTClient(
    client_id_prefix="my_simple_app",
    broker_url="tcp://localhost:1883",
    is_threaded=True,
)
mqtt_client.on_message = on_message
mqtt_client.on_connect = on_connect


# Instantiate registry (after mqtt_client is defined)
# ------------------------------------------------------------------

registry = DeviceRegistry(
    "/etc/wb-alice-devices.conf",
    send_to_yandex=send_to_yandex_state,
    publish_to_mqtt=publish_to_mqtt,
)


def get_controller_sn():
    """Get controller ID from the configuration file"""
    try:
        with open(SHORT_SN_PATH, "r") as file:
            controller_sn = file.read().strip()
            logger.info(f"[INFO] Read controller ID: {controller_sn}")
            return controller_sn
    except FileNotFoundError:
        logger.info(
            f"[ERR] Controller ID file not found! Check the path: {SHORT_SN_PATH}"
        )
        return None
    except Exception as e:
        logger.info(f"[ERR] Reading controller ID exception: {e}")
        return None


def read_config():
    """Read configuration file"""
    try:
        if not os.path.exists(CONFIG_PATH):
            logger.info(f"[ERR] Configuration file not found at {CONFIG_PATH}")
            return None

        with open(CONFIG_PATH, "r") as file:
            config = json.load(file)
            return config
    except json.JSONDecodeError:
        logger.info("[ERR] Parsing configuration file: Invalid JSON format")
        return None
    except Exception as e:
        logger.info(f"[ERR] Reading configuration exception: {e}")
        return None


async def read_mqtt_state(
    topic: str, mqtt_host="localhost", timeout=1
) -> Optional[bool]:
    """
    Reads the value of a topic (0/1, "false"/"true", etc.) and returns a Python bool
    Uses subscribe.simple(...) from paho.mqtt, which BLOCKS for the duration of reading
    """
    logger.info("[read_mqtt_state] trying to read topic: %s", topic)

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
        logger.info("[WARN] Unexpected payload in topic '%s': %s", topic, payload_str)
        return False


def write_mqtt_state(mqtt_client: mqtt.Client, topic: str, is_on: bool) -> None:
    """
    Publishes "1" (True) or "0" (False) to the given topic
    """
    payload_str = "1" if is_on else "0"
    full_topic = f"{topic}/on"
    mqtt_client.publish(full_topic, payload_str)
    logger.debug("[MQTT] Published '%s' to '%s'", payload_str, full_topic)


@sio.event
async def connect():
    logger.info("[SUCCESS] Connected to Socket.IO server!")
    await sio.emit("message", {"controller_sn": controller_sn, "status": "online"})


# NOTE: argument "reason" not accesable in current client 5.0.3
#       implemented on version 5.12
@sio.event
async def disconnect():
    logger.info("[DISCONNECT] This client disconnected from server")


@sio.event
async def response(data):
    logger.info(f"[INCOME] Server response: {data}")


@sio.event
async def error(data):
    logger.info(f"[ERR] Server error: {data}")
    logger.info("[ERR] Terminating connection due to server error")
    await sio.disconnect()


@sio.event
async def connect_error(data: dict[str, Any]) -> None:
    """
    Called when initial connection to server fails
    """
    logger.warning("[SOCKET.IO] Connection refused by server: %s", data)


@sio.on("*")
async def any_event(event, sid, data):
    logger.info(f"[Socket.IO/ANY] Not handled event {event}")


@sio.on("alice_devices_list")
async def on_alice_devices_list(data: dict[str, Any]) -> dict[str, Any]:
    """
    Handles a device discovery request from the server
    Returns a list of devices defined in the controller config
    """
    logger.debug("[SOCKET.IO] Received 'alice_devices_list' event:")
    logger.debug(json.dumps(data, ensure_ascii=False, indent=2))

    req_id: str = data.get("request_id")
    devices_list: list[dict[str, Any]] = registry.build_yandex_devices_list()
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


@sio.on("alice_devices_query")
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
        devices_response.append(await registry.get_device_current_state(device_id))

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
            registry.handle_action(device_id, cap_type, instance, value)
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


@sio.on("alice_devices_action")
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


async def connect_controller():
    global controller_sn

    config = read_config()
    if not config:
        logger.info("[ERR] Cannot proceed without configuration")
        return

    # TODO(vg): On this moment this parameter hardcoded - must set after
    #           register controller on web server automatically
    if not config.get("is_registered", False):
        logger.info(
            "[ERR] Controller is not registered. Please register the controller first."
        )
        return

    controller_sn = get_controller_sn()
    if not controller_sn:
        logger.info("[ERR] Cannot proceed without controller ID")
        return

    server_address = config.get("server_address")
    if not server_address:
        logger.info("[ERR] 'server_address' not specified in configuration")
        return

    # Connect to local MQTT broker (assuming Wiren Board default: localhost:1883)
    local_mqtt_client = mqtt.Client("wb-alice-client")
    try:
        local_mqtt_client.connect("localhost", 1883, 60)
        local_mqtt_client.loop_start()
        logger.info(
            # f"[INFO] Connected to local MQTT broker, using topic '{mqtt_topics['light_corridor'].full}'"
            "[INFO] Connected to local MQTT broker"
        )
    except Exception as e:
        logger.info(f"[ERR] MQTT connect failed: {e}")
        # Можно прервать работу или продолжить без MQTT
        # Жёстко прерываем при отсутствии брокера
        return

    server_url = f"https://{server_address}"
    logger.info(f"[INFO] Connecting to Socket.IO server: {server_url}")

    try:
        # Connect to server and keep connection active
        # Pass controller_sn via custom header
        await sio.connect(
            server_url,
            socketio_path="/socket.io",
            headers={"X-Controller-SN": controller_sn},
        )
        logger.info("[CONNECT_CONTROLLER] Socket.IO connected successfully")

    except socketio.exceptions.ConnectionError as e:
        logger.error(f"[ERR] Socket.IO connection error: {e}")
        # Unable to connect
        # - The controller might have been unregistered
        # - Or Server may have error or offline
        # ACTION - do reconnection
    except Exception as e:
        logger.exception(f"[ERR] Unexpected exception during connection: {e}")
        # ACTION - do reconnection


def _log_and_stop(sig: signal.Signals) -> None:
    """
    Generic signal handler:
    1) logs which signal was received;
    2) sets the global stop_event so the main loop can exit.
    Idempotent: repeated signals after the first one do nothing.
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    logger.warning("[SIGNAL] %s received at %s – shutting down…", sig.name, ts)

    # stop_event is created in main() before signal handlers are registered,
    # but we keep the guard just in case.
    if stop_event is not None and not stop_event.is_set():
        stop_event.set()


async def main() -> None:
    global stop_event
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

    stop_event = asyncio.Event()  # keeps the loop alive until a signal arrives
    MAIN_LOOP.add_signal_handler(signal.SIGINT, _log_and_stop, signal.SIGINT)
    MAIN_LOOP.add_signal_handler(signal.SIGTERM, _log_and_stop, signal.SIGTERM)

    logger.info("[MAIN] Start background services ...")

    logger.info("[MAIN] Starting MQTT client...")
    mqtt_client.start()

    logger.info("[MAIN] Connecting Socket.IO client...")
    await connect_controller()
    sio_task = asyncio.create_task(sio.wait())

    # Wait for shutdown signal
    await stop_event.wait()
    logger.info("[MAIN] Shutdown signal received")

    logger.info("[MAIN] Stopping Socket.IO client ...")
    if sio.connected:
        await sio.disconnect()
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

    logger.info("[MAIN] Stopping MQTT client")
    mqtt_client.stop()  # WB func - do inside loop_stop + disconnect
    logger.info("[MAIN] MQTT disconnected")

    logger.info("[MAIN] Shutdown complete")


if __name__ == "__main__":
    logger.info("[MAIN] Starting wb-alice-client...")

    try:
        asyncio.run(main(), debug=True)
    except KeyboardInterrupt:
        logger.warning("[MAIN] Interrupted by user (Ctrl+C)")
    except SystemExit as e:
        logger.warning("[MAIN] System exit with code %s", e.code)
    except Exception as e:
        logger.exception("[MAIN] Unhandled exception: %s", e)
    finally:
        logger.info("[MAIN] wb-alice-client stopped.")
