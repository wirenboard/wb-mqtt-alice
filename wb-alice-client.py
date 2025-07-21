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

sio: Optional[socketio.AsyncClient] = None


# TODO: implement certificates work (client + server)
# Best solution for owr old client (Release 5.0.3 - 2020, 14 Dec)
# https://github.com/miguelgrinberg/python-socketio/discussions/1040

# Maybe easely do via NGINX (owr nginx/1.18.0 - 2020-05-28)?
# https://mailman.nginx.org/pipermail/unit/2020-May/000201.html
# Начиная с 1.7.9 вместо обычного файла можно подставить строку вида
# engine:<имя-engine>:<id-ключа> — тогда все операции с приватным ключом будут выполнять­ся через OpenSSL-engine (PKCS#11, HSM, secure-element и т. д.)
# https://nginx.org/en/docs/http/ngx_http_proxy_module.html


# This watchdog monitors connection health and handles reconnection manually
# without SocketIO mechanism
# TODO: Need to investigate more to fix standard SocketIO reconnection
async def _watchdog_task(sock: socketio.AsyncClient) -> None:
    global _RECONNECTING, _BACKOFF, _LAST_RECONNECT_TIME
    reconnect_trace_task = None
    while True:
        await asyncio.sleep(WATCHDOG_PERIOD)

        # debug_socketio_state(sock, "WATCHDOG")
        debug_socketio_state_enhanced(sock, "WATCHDOG")
        if not sock.connected:
            log_reconnect_timing()

        # Проверка целостности
        integrity = check_engineio_integrity(sock)
        logger.info(f"[WATCHDOG] EngineIO integrity: {integrity}")

        if hasattr(sock, "_reconnect_task") and sock._reconnect_task:
            task = sock._reconnect_task

            # Если это новая задача (не отслеживаемая)
            if (
                reconnect_trace_task is None
                or reconnect_trace_task.done()
                or id(task) != getattr(reconnect_trace_task, "_tracked_task_id", None)
            ):

                logger.info(f"[WATCHDOG] New reconnect task detected: {id(task)}")
                log_reconnect_timing()

                # Запускаем трейсинг этой задачи
                reconnect_trace_task = asyncio.create_task(
                    trace_reconnect_execution(sock)
                )
                reconnect_trace_task._tracked_task_id = id(task)

        # Error indicators
        connected = sock.connected  # SocketIO status method
        # write_task = getattr(sock.eio, "_write_loop_task", True) # True == live
        write_task = getattr(sock.eio, "_write_loop_task", None)
        write_task_ok = (
            write_task is not None
            and hasattr(write_task, "done")  # проверяем что это Task
            and not write_task.done()  # и что он не завершен
        )

        # read_task  = getattr(sock.eio, "_read_loop_task",  True)
        read_task = getattr(sock.eio, "_read_loop_task", None)
        read_task_ok = (
            read_task is not None
            and hasattr(read_task, "done")
            and not read_task.done()
        )

        # last_ping == 0  →   считаем «ещё не измеряли»
        last_ping = getattr(sock.eio, "last_ping", 0)
        ping_stale = last_ping and (time.time() - last_ping > MAX_PING_SILENCE)
        transport_connected = True
        if hasattr(sock.eio, "transport") and sock.eio.transport:
            transport_connected = getattr(sock.eio.transport, "connected", True)

        need_reconnect = (
            not connected
            or not write_task_ok  # write-loop умер (баг 5.0.3)
            or not read_task_ok  # read-loop  "
            or ping_stale  # сервер реально молчит
            or not transport_connected  # новая проверка
        )
        if not need_reconnect:
            continue

        # --------- 2) защита от гонок --------------------------------------
        if _RECONNECTING:
            continue
        _RECONNECTING = True

        current_time = time.time()
        since_last_recon = current_time - _LAST_RECONNECT_TIME
        if since_last_recon < _MIN_RECONNECT_INTERVAL:
            logger.debug(
                "[WATCHDOG] Too soon since last reconnect (%.1fs ago), waiting...",
                since_last_recon,
            )
            continue

        _LAST_RECONNECT_TIME = current_time

        try:
            logger.warning(
                "[WATCHDOG] triggered: conn=%s write_ok=%s read_ok=%s ping_stale=%s transport=%s",
                connected,
                write_task_ok,
                read_task_ok,
                ping_stale,
                transport_connected,
            )
            log_reconnect_timing()

            # Отменяем сломанные задачи если есть
            if (
                hasattr(sock, "_reconnect_task")
                and sock._reconnect_task
                and not sock._reconnect_task.done()
            ):
                logger.info("[WATCHDOG] Cancelling broken native reconnect task")
                sock._reconnect_task.cancel()

                # ДОБАВИТЬ ЗДЕСЬ - после отмены задачи
                log_reconnect_timing()

            # закрываем только если ещё «подвисшее» connected==True
            if sock.connected:
                with contextlib.suppress(Exception):
                    await sock.disconnect()

            await asyncio.sleep(0.1)  # дать event-loop’у подчиститься
            logger.info("[WATCHDOG] About to attempt manual reconnect")
            log_reconnect_timing()

            await sock.connect(
                SERVER_URL,
                socketio_path="/socket.io",
                headers={"X-Controller-SN": controller_sn},
            )
            logger.info("[WATCHDOG] reconnect succeeded")
            log_reconnect_timing()
            _BACKOFF = 2

        except Exception as exc:
            logger.error(
                "[WATCHDOG] reconnect failed: %s (next try in %ds)",
                exc,
                _BACKOFF,
            )
            await asyncio.sleep(_BACKOFF)
            _BACKOFF = min(_BACKOFF * 2, 30)

        finally:
            _RECONNECTING = False


for name in ("engineio", "socketio"):
    logging.getLogger(name).setLevel(logging.DEBUG)
# sio.eio.logger.setLevel(logging.DEBUG)
# sio.logger.setLevel(logging.DEBUG)

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

    if not hasattr(sio, "namespaces") or "/" not in sio.namespaces:
        logger.warning("[SOCKET.IO] Namespace not ready, skipping emit '%s'", event)
        return

    # Additional check for version SocketIO 5.0.3 (may delete when upgrade)
    if hasattr(sio.eio, "_write_loop_task") and sio.eio._write_loop_task is None:
        logger.warning(
            "[SOCKET.IO] Write loop task is None, connection unstable - skipping emit '%s'",
            event,
        )
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

        # Маппинг instance -> unit для properties
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
                # Добавляем parameters только если есть instance
                instance = prop.get("instance")
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

    def _debug_capability_lookup(self, device_id: str, key: tuple) -> None:
        """Отладочная функция для поиска capability в cap_index"""
        logger.info(f"[DEBUG] Looking for key: {key}")
        logger.info(f"[DEBUG] Available keys in cap_index for device {device_id}:")
        for k in self.cap_index.keys():
            if k[0] == device_id:  # показать только для текущего устройства
                logger.info(f"[DEBUG]   {k} -> {self.cap_index[k]}")

    async def _read_capability_state(self, device_id: str, cap: dict) -> Optional[dict]:
        cap_type = cap["type"]
        instance = cap.get("instance")
        key = (device_id, cap_type, instance)

        # Отладка (можно убрать после исправления)
        self._debug_capability_lookup(device_id, key)

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
        instance = prop.get("instance")
        key = (device_id, prop_type, instance)

        # Отладка (можно убрать после исправления)
        self._debug_capability_lookup(device_id, key)

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
            instance,
            value,
        )
    elif cap_type == "devices.properties.float":
        send_state_to_server(
            device_id,
            "devices.properties.float",
            instance,
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


# @sio.event
async def connect():
    logger.info("[SUCCESS] Connected to Socket.IO server!")

    log_reconnect_timing()
    debug_socketio_state_enhanced(sio, "CONNECT")
    await sio.emit("message", {"controller_sn": controller_sn, "status": "online"})


# NOTE: argument "reason" not accesable in current client 5.0.3 (2020, 14 Dec)
#       implemented on version 5.12
# @sio.event
async def disconnect(*args, **kwargs):
    """Срабатывает при любом разрыве соединения."""
    reason = args[0] if args else kwargs.get("reason")
    logger.warning(
        "[DISCONNECT] lost connection; reason=%r  args=%s  kwargs=%s",
        reason,
        args,
        kwargs,
    )
    log_reconnect_timing()

    # Маркер багa 5.0.3 — в логе engineio чуть выше будет TypeError.
    logger.warning(
        "[BUGCHECK] если выше было "
        "\"Unexpected error decoding packet: 'int' object is not subscriptable\" "
        "→ это баг старого клиента ≤5.0.3"
    )

    # Запустить трейсинг задачи реконнекта
    if hasattr(sio, "_reconnect_task") and sio._reconnect_task:
        logger.info("[DISCONNECT] Starting reconnect task monitoring...")
        asyncio.create_task(trace_reconnect_execution(sio))

    # # ---- одноразовый «ручной» цикл переподключения ----
    # async def _reconnect():
    #     delay = 2
    #     while not sio.connected and SERVER_URL:
    #         try:
    #             # вызов без параметра `wait`
    #             await sio.connect(
    #                 SERVER_URL,
    #                 socketio_path="/socket.io",
    #                 headers={"X-Controller-SN": controller_sn},
    #             )
    #             logger.info("[MANUAL] reconnect succeeded")
    #         except Exception as exc:
    #             logger.error(
    #                 "[MANUAL] reconnect failed: %s (next try in %ss)", exc, delay
    #             )
    #             await asyncio.sleep(delay)
    #             delay = min(delay * 2, 30)      # экспоненциально до 30 с

    # # запускаем фоновую задачу ОДИН раз
    # asyncio.create_task(_reconnect())


# Функция для мониторинга жизненного цикла reconnect задачи
def debug_reconnect_lifecycle(sio):
    """
    Отслеживает полный жизненный цикл задачи реконнекта
    """
    if not hasattr(sio, "_reconnect_task") or not sio._reconnect_task:
        return "No reconnect task"

    task = sio._reconnect_task
    result = {
        "task_id": id(task),
        "done": task.done(),
        "cancelled": task.cancelled() if hasattr(task, "cancelled") else False,
    }

    if task.done():
        try:
            task_result = task.result()
            result["result"] = str(task_result)
            result["success"] = True
        except Exception as e:
            result["exception"] = str(e)
            result["exception_type"] = type(e).__name__
            result["success"] = False

            # Анализ специфичных ошибок
            if "different loop" in str(e):
                result["bug_type"] = "ASYNCIO_LOOP_MISMATCH"
                result["analysis"] = "Task created in one loop, executed in another"
            elif "int" in str(e) and "subscriptable" in str(e):
                result["bug_type"] = "PACKET_DECODE_BUG"

    return result


# Функция для понимания где именно падает задача
async def trace_reconnect_execution(sio):
    """
    Пытается понять на каком этапе падает reconnect
    """
    if not hasattr(sio, "_reconnect_task") or not sio._reconnect_task:
        return

    task = sio._reconnect_task

    # Мониторим задачу каждые 100ms
    check_count = 0
    while not task.done() and check_count < 50:  # max 5 секунд
        await asyncio.sleep(0.1)
        check_count += 1

        # Логируем прогресс
        logger.debug(f"[RECONNECT_TRACE] Check #{check_count}: task still running")

    # Анализируем результат
    if task.done():
        lifecycle = debug_reconnect_lifecycle(sio)
        logger.info(f"[RECONNECT_TRACE] Task completed: {lifecycle}")

        if not lifecycle.get("success", False):
            logger.error(f"[RECONNECT_TRACE] Task failed at early stage:")
            logger.error(f"  - Never reached connection attempt")
            logger.error(f"  - Failed during: asyncio.sleep() or Event.wait()")
            logger.error(f"  - Bug type: {lifecycle.get('bug_type', 'unknown')}")


# @sio.event
async def response(data):
    logger.info(f"[INCOME] Server response: {data}")


# @sio.event
async def error(data):
    logger.info(f"[SOCKETIO] Server error: {data}")
    # logger.info("[SOCKETIO] Terminating connection due to server error")
    # await sio.disconnect()


# @sio.event
async def connect_error(data: dict[str, Any]) -> None:
    """
    Called when initial connection to server fails
    """
    logger.warning("[SOCKET.IO] Connection refused by server: %s", data)
    log_reconnect_timing()


# @sio.on("*")
async def any_event(event, sid, data):
    logger.info(f"[Socket.IO/ANY] Not handled event {event}")


# @sio.on("alice_devices_list")
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


# @sio.on("alice_devices_query")
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


# @sio.on("alice_devices_action")
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


async def connect_controller(sock: socketio.AsyncClient):
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

    global SERVER_URL
    # SERVER_URL = f"https://{server_address}"
    SERVER_URL = f"http://localhost:8042"
    logger.info(f"[INFO] Connecting to Socket.IO server: {SERVER_URL}")

    try:
        # Connect to server and keep connection active
        # Pass controller_sn via custom header
        # NOTE: argument for custom auth={'token': 'my-token'} not accesable
        # in current client 5.0.3, this function implemented on version 5.1.0
        await sock.connect(
            SERVER_URL,
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


def debug_socketio_state(sio, label="DEBUG"):
    """
    Проверяет состояние SocketIO клиента и задач реконнекта
    """
    logger.info(f"[{label}] SocketIO State Check:")
    logger.info(f"  - sio.connected: {sio.connected}")

    if hasattr(sio, "eio") and sio.eio:
        eio = sio.eio
        logger.info(f"  - eio.state: {getattr(eio, 'state', 'unknown')}")

        # Проверяем задачи read/write loop
        write_task = getattr(eio, "_write_loop_task", None)
        read_task = getattr(eio, "_read_loop_task", None)
        logger.info(
            f"  - write_loop_task: {write_task is not None} ({'alive' if write_task and not write_task.done() else 'dead/None'})"
        )
        logger.info(
            f"  - read_loop_task: {read_task is not None} ({'alive' if read_task and not read_task.done() else 'dead/None'})"
        )

        # Проверяем ping состояние
        last_ping = getattr(eio, "last_ping", 0)
        if last_ping:
            ping_age = time.time() - last_ping
            logger.info(f"  - last_ping: {ping_age:.1f}s ago")
        else:
            logger.info(f"  - last_ping: never")

    # Проверяем задачи реконнекта на уровне SocketIO
    if hasattr(sio, "_reconnect_task"):
        reconnect_task = sio._reconnect_task
        if reconnect_task:
            logger.info(
                f"  - socketio reconnect_task: {'alive' if not reconnect_task.done() else 'done/failed'}"
            )
            if reconnect_task.done():
                try:
                    result = reconnect_task.result()
                    logger.info(f"    └─ result: {result}")
                except Exception as e:
                    logger.info(f"    └─ exception: {e}")
        else:
            logger.info(f"  - socketio reconnect_task: None")

    # Проверяем задачи реконнекта на уровне EngineIO
    if hasattr(sio, "eio") and sio.eio and hasattr(sio.eio, "_reconnect_task"):
        eio_reconnect_task = sio.eio._reconnect_task
        if eio_reconnect_task:
            logger.info(
                f"  - engineio reconnect_task: {'alive' if not eio_reconnect_task.done() else 'done/failed'}"
            )
        else:
            logger.info(f"  - engineio reconnect_task: None")

    # Проверяем все активные asyncio задачи связанные с socketio
    current_tasks = [task for task in asyncio.all_tasks() if not task.done()]
    socketio_tasks = []
    for task in current_tasks:
        task_name = getattr(task, "get_name", lambda: "unnamed")()
        coro_name = (
            task.get_coro().__name__
            if hasattr(task.get_coro(), "__name__")
            else str(task.get_coro())
        )
        if (
            "socket" in coro_name.lower()
            or "engine" in coro_name.lower()
            or "connect" in coro_name.lower()
        ):
            socketio_tasks.append(f"{task_name}: {coro_name}")

    if socketio_tasks:
        logger.info(f"  - related async tasks: {len(socketio_tasks)}")
        for task_info in socketio_tasks:
            logger.info(f"    └─ {task_info}")
    else:
        logger.info(f"  - related async tasks: none found")


def debug_socketio_state_enhanced(sio, label="DEBUG"):
    """
    Расширенная диагностика состояния SocketIO с проверкой event loops
    """
    logger.info(f"[{label}] SocketIO Enhanced State Check:")
    logger.info(f"  - sio.connected: {sio.connected}")

    # Получаем текущий event loop
    try:
        current_loop = asyncio.get_running_loop()
        current_loop_id = id(current_loop)
        logger.info(f"  - current_loop_id: {current_loop_id}")
    except RuntimeError:
        current_loop = None
        current_loop_id = "no_loop"
        logger.info(f"  - current_loop_id: {current_loop_id}")

    if hasattr(sio, "eio") and sio.eio:
        eio = sio.eio
        logger.info(f"  - eio.state: {getattr(eio, 'state', 'unknown')}")

        # Проверяем внутренние атрибуты EngineIO
        write_task = getattr(eio, "write_loop_task", "missing")
        read_task = getattr(eio, "read_loop_task", "missing")
        reconnect_task = getattr(sio, "_reconnect_task", "missing")

        # Расширенная информация о задачах
        logger.info(f"  - _write_loop_task: {type(write_task).__name__}")
        if type(write_task) == str:
            logger.info(f"    └─ write_task: {write_task}")
        if hasattr(write_task, "get_loop") and write_task is not None:
            task_loop_id = id(write_task.get_loop())
            logger.info(f"    └─ task_loop_id: {task_loop_id}")
            logger.info(f"    └─ same_loop: {task_loop_id == current_loop_id}")

        logger.info(f"  - _read_loop_task: {type(read_task).__name__}")
        if hasattr(read_task, "get_loop") and read_task is not None:
            task_loop_id = id(read_task.get_loop())
            logger.info(f"    └─ task_loop_id: {task_loop_id}")
            logger.info(f"    └─ same_loop: {task_loop_id == current_loop_id}")

        logger.info(f"  - reconnect_task: {type(reconnect_task).__name__}")
        if type(reconnect_task) == str:
            logger.info(f"    └─ reconnect_task: {reconnect_task}")
        if hasattr(reconnect_task, "get_loop") and reconnect_task is not None:
            task_loop_id = id(reconnect_task.get_loop())
            logger.info(f"    └─ task_loop_id: {task_loop_id}")
            logger.info(f"    └─ same_loop: {task_loop_id == current_loop_id}")

        # Дополнительные атрибуты EngineIO для понимания state
        for attr in ["transport", "_connect_event", "_reconnect_task"]:
            if hasattr(eio, attr):
                val = getattr(eio, attr)
                logger.info(f"  - eio.{attr}: {type(val).__name__ if val else None}")

        # Проверяем transport и его состояние
        if hasattr(eio, "transport") and eio.transport:
            transport = eio.transport
            logger.info(
                f"  - transport.connected: {getattr(transport, 'connected', 'unknown')}"
            )
            logger.info(
                f"  - transport.state: {getattr(transport, 'state', 'unknown')}"
            )

    # Проверяем задачи реконнекта SocketIO
    reconnect_task = getattr(sio, "_reconnect_task", None)
    if reconnect_task:
        logger.info(f"  - socketio._reconnect_task: {type(reconnect_task).__name__}")
        logger.info(f"    └─ done: {reconnect_task.done()}")
        if reconnect_task.done():
            try:
                result = reconnect_task.result()
                logger.info(f"    └─ result: {result}")
            except Exception as e:
                logger.info(f"    └─ exception: {type(e).__name__}: {e}")
                # Дополнительная информация об asyncio ошибках
                if "different loop" in str(e):
                    logger.error(f"    └─ ASYNCIO LOOP MISMATCH DETECTED!")

        # Проверяем loop задачи реконнекта
        if hasattr(reconnect_task, "get_loop"):
            task_loop_id = id(reconnect_task.get_loop())
            logger.info(f"    └─ task_loop_id: {task_loop_id}")
            logger.info(f"    └─ same_loop: {task_loop_id == current_loop_id}")

    # Анализ всех активных Socket.IO/Engine.IO задач
    current_tasks = [task for task in asyncio.all_tasks() if not task.done()]
    socketio_tasks = []

    for task in current_tasks:
        coro = task.get_coro()
        coro_name = getattr(coro, "__name__", str(coro))
        coro_qualname = getattr(coro, "__qualname__", "unknown")

        # Ищем задачи связанные с socket.io/engine.io
        search_terms = [
            "socket",
            "engine",
            "read_loop",
            "write_loop",
            "_handle",
            "connect",
        ]
        if any(term in coro_name.lower() for term in search_terms):
            task_loop_id = id(task.get_loop())
            socketio_tasks.append(
                {
                    "name": coro_name,
                    "qualname": coro_qualname,
                    "loop_id": task_loop_id,
                    "same_loop": task_loop_id == current_loop_id,
                }
            )

    if socketio_tasks:
        logger.info(f"  - related async tasks: {len(socketio_tasks)}")
        for task_info in socketio_tasks:
            logger.info(
                f"    └─ {task_info['name']} (loop_match: {task_info['same_loop']})"
            )
    else:
        logger.info(f"  - related async tasks: none found")

    # Дополнительная диагностика MAIN_LOOP
    global MAIN_LOOP
    if MAIN_LOOP:
        main_loop_id = id(MAIN_LOOP)
        logger.info(f"  - MAIN_LOOP_id: {main_loop_id}")
        logger.info(f"  - using_main_loop: {main_loop_id == current_loop_id}")
        logger.info(f"  - main_loop_running: {MAIN_LOOP.is_running()}")


# Добавить проверку целостности атрибутов EngineIO
def check_engineio_integrity(sio):
    """Проверяет целостность внутренних структур EngineIO"""
    if not hasattr(sio, "eio") or not sio.eio:
        return "No EIO object"

    eio = sio.eio
    issues = []

    # Проверяем ключевые атрибуты
    critical_attrs = ["_write_loop_task", "_read_loop_task", "transport", "state"]
    for attr in critical_attrs:
        if not hasattr(eio, attr):
            issues.append(f"Missing {attr}")
        elif attr.endswith("_task"):
            task = getattr(eio, attr)
            if task is None:
                issues.append(f"{attr} is None")
            elif hasattr(task, "done") and task.done() and not task.cancelled():
                try:
                    task.result()
                except Exception as e:
                    issues.append(f"{attr} failed: {e}")

    # Проверяем состояние transport
    if hasattr(eio, "transport") and eio.transport:
        transport = eio.transport
        if not getattr(transport, "connected", True):
            issues.append("Transport not connected")

    return issues if issues else "OK"


# Дополнительная функция для понимания timing'а
def log_reconnect_timing():
    """
    Помогает понять timing проблемы с reconnect
    """
    logger.info(f"[TIMING] Current time: {time.time()}")
    logger.info(f"[TIMING] Current loop: {id(asyncio.get_running_loop())}")

    # Проверяем все pending задачи
    all_tasks = asyncio.all_tasks()
    pending_tasks = [t for t in all_tasks if not t.done()]

    reconnect_tasks = []
    for task in pending_tasks:
        coro_str = str(task.get_coro())
        if "reconnect" in coro_str.lower() or "handle" in coro_str.lower():
            reconnect_tasks.append(
                {"id": id(task), "coro": coro_str[:100], "loop_id": id(task.get_loop())}
            )

    if reconnect_tasks:
        logger.info(f"[TIMING] Found {len(reconnect_tasks)} potential reconnect tasks:")
        for rt in reconnect_tasks:
            logger.info(f"  - Task {rt['id']}: {rt['coro']}")
            logger.info(f"    Loop: {rt['loop_id']}")


WATCHDOG_PERIOD = 5  # сек. между проверками
MAX_PING_SILENCE = 30  # сек. молчания от сервера → считаем, что подвисло
_RECONNECTING = False  # защита от гонок
_BACKOFF = 2  # экспоненциальный back-off (2 → 4 → … 30)

# Reconnect debouncing
_LAST_RECONNECT_TIME = 0
_MIN_RECONNECT_INTERVAL = 5  # секунд между попытками


async def analyze_reconnect_timing_issues():
    """
    Запускать эту функцию когда хотите детально изучить timing проблемы
    """
    logger.info("=== STARTING TIMING ANALYSIS ===")

    for i in range(20):  # 20 проверок каждые 0.5 сек = 10 секунд
        logger.info(f"[TIMING_ANALYSIS] Check #{i+1}")
        log_reconnect_timing()

        # Дополнительно проверяем состояние reconnect задачи
        if hasattr(sio, "_reconnect_task") and sio._reconnect_task:
            task = sio._reconnect_task
            logger.info(f"  └─ Reconnect task {id(task)}: done={task.done()}")

            if task.done():
                lifecycle = debug_reconnect_lifecycle(sio)
                logger.info(f"  └─ Lifecycle: {lifecycle}")

        await asyncio.sleep(0.5)

    logger.info("=== TIMING ANALYSIS COMPLETE ===")


def bind_handlers(sock: socketio.AsyncClient):
    # NOTE: Unlike decorators we use .on for more safety - this method help
    #       control all names and objectes in any time and any context

    # @sio.on("*")
    # async def any_event(event, sid, data):
    # @sio.on("alice_devices_list")
    # async def on_alice_devices_list(data: dict[str, Any]) -> dict[str, Any]:
    # @sio.on("alice_devices_query")
    # async def on_alice_devices_query(data):
    # @sio.on("alice_devices_action")
    # async def on_alice_devices_action(data: dict[str, Any]) -> dict[str, Any]:

    sock.on("*", any_event)
    sock.on("alice_devices_list", on_alice_devices_list)
    sock.on("alice_devices_query", on_alice_devices_query)
    sock.on("alice_devices_action", on_alice_devices_action)

    # @sio.event
    # async def connect():
    # # NOTE: argument "reason" not accesable in current client 5.0.3
    # #       implemented on version 5.12
    # @sio.event
    # async def disconnect(*args, **kwargs):
    # @sio.event
    # async def response(data):
    # @sio.event
    # async def error(data):

    # @sio.event
    # async def connect_error(data: dict[str, Any]) -> None:

    sock.on("connect", connect)
    sock.on("disconnect", disconnect)
    sock.on("response", response)
    sock.on("error", error)
    sock.on("connect_error", connect_error)


async def main() -> None:
    global sio
    global stop_event
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

    logger.info("[MAIN] Starting with initial timing check:")
    log_reconnect_timing()

    stop_event = asyncio.Event()  # keeps the loop alive until a signal arrives
    MAIN_LOOP.add_signal_handler(signal.SIGINT, _log_and_stop, signal.SIGINT)
    MAIN_LOOP.add_signal_handler(signal.SIGTERM, _log_and_stop, signal.SIGTERM)

    logger.info("[MAIN] Start background services ...")

    logger.info("[MAIN] Starting MQTT client...")
    mqtt_client.start()

    sio = socketio.AsyncClient(
        logger=True,
        engineio_logger=True,
        reconnection=True,  # auto-reconnect ON
        reconnection_attempts=0,  # 0 = infinite retries
        reconnection_delay=2,  # first delay 2 s
        reconnection_delay_max=30,  # cap at 30 s
        randomization_factor=0.5,  # jitter
    )

    # sio = socketio.AsyncClient(
    #     logger=True,
    #     engineio_logger=True,
    #     reconnection=False, # TODO: search reason not working correctly
    # )

    # FIXME: Workaround for SocketIO 5.0.3 reconnection bug
    # Currently, when a "disconnect" event occurs initiated by unexpected server
    # shutdown via CTRL+C, but can also happen in other ways:
    #   - SocketIO fails with error:
    #     INFO:engineio.client:Unexpected error decoding packet: "'int' object is not subscriptable", aborting
    #     INFO:engineio.client:Exiting read loop task
    #     INFO:socketio.client:Connection failed, new attempt in 1.73 seconds
    #   - If set socketio.AsyncClient(reconnection=True)
    #     Created new task in "socketio._reconnect_task"
    #   - In reconnect task get error (socketio._reconnect_task.result())
    #     exception: RuntimeError: Task <Task pending name='Task-45' coro=<Event.wait() running at /usr/lib/python3.9/asyncio/locks.py:226> cb=[_release_waiter(<Future pendi...events.py:424>)() at /usr/lib/python3.9/asyncio/tasks.py:416] created at /usr/lib/python3.9/asyncio/tasks.py:462> got Future <Future pending> attached to a different loop
    #   - Result: Breaking reconnection - client doesn't get any reconnection attempts
    # GitHub has some tickets where maintainers are improving SocketIO code with
    #   more general type checks in methods.
    #   The maintainer wrote about this code in this post:
    #   https://github.com/miguelgrinberg/python-socketio/issues/417#issuecomment-608050583
    #

    # Явно пропишем loop который используется чтобы избежать ошибки "attached to a different loop" у частей системы
    sio._loop = MAIN_LOOP
    bind_handlers(sio)

    logger.info("[MAIN] Connecting Socket.IO client...")
    await connect_controller(sio)
    log_reconnect_timing()
    sio_task = asyncio.create_task(sio.wait())

    # ─── сторож за Transport'ом ───
    # asyncio.create_task(_watchdog_task(sio))
    # asyncio.create_task(analyze_reconnect_timing_issues())

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
