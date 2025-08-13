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
import random
import signal
import string
import time
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt_client
import socketio

from device_registry import DeviceRegistry
from yandex_handlers import send_to_yandex_state, set_emit_callback

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
CONFIG_PATH = "/etc/wb-mqtt-alice-client.conf"


class AppContext:
    def __init__(self):
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None
        """
        Global asyncio event loop, used to safely schedule coroutines from non-async
        threads (e.g. MQTT callbacks) via `asyncio.run_coroutine_threadsafe()`
          - stop_event - Event that signals the main loop to wake up and initiate shutdown
          - sio - SocketIO async client instance for handling real-time communication

        """
        self.stop_event: Optional[asyncio.Event] = None
        self.sio: Optional[socketio.AsyncClient] = None
        self.registry: Optional[DeviceRegistry] = None
        self.mqtt_client: Optional[mqtt_client.Client] = None
        self.controller_sn: Optional[str] = None


ctx = AppContext()


def _emit_async(event: str, data: Dict[str, Any]) -> None:
    """
    Safely schedules a Socket.IO event to be emitted
    from any thread (async or not).
    """
    if not ctx.sio or not ctx.sio.connected:
        logger.warning("Not connected, skipping emit '%s'", event)
        logger.debug("            Payload: %s", json.dumps(data))
        return
    logger.debug("Connected status: %s", ctx.sio.connected)

    if not hasattr(ctx.sio, "namespaces") or "/" not in ctx.sio.namespaces:
        logger.warning("Namespace not ready, skipping emit '%s'", event)
        return

    # BUG: Additional check for version SocketIO 5.0.3 (may delete when upgrade)
    if hasattr(ctx.sio.eio, "write_loop_task") and ctx.sio.eio.write_loop_task is None:
        logger.warning(
            "Write loop task is None, connection unstable - skipping emit '%s'",
            event,
        )
        return

    logger.debug("Attempting to emit '%s' with payload: %s", event, data)

    try:
        # We're in an asyncio thread – safe to call create_task directly
        asyncio.get_running_loop()
        asyncio.create_task(ctx.sio.emit(event, data))
        logger.debug("Scheduled emit '%s' via asyncio task", event)

    except RuntimeError:
        # No running loop in current thread – fallback to ctx.main_loop
        logger.debug("No running loop in current thread – using ctx.main_loop")

        if ctx.main_loop is None:
            logger.warning("ctx.main_loop not available – dropping event '%s'", event)
            return

        if ctx.main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(
                ctx.sio.emit(event, data), ctx.main_loop
            )

            # Log if the future raises an exception
            def log_emit_exception(f: asyncio.Future):
                exc = f.exception()
                if exc:
                    logger.error("Emit '%s' failed: %s", event, exc, exc_info=True)

            fut.add_done_callback(log_emit_exception)
        else:
            logger.error("ctx.main_loop is not running – cannot emit '%s'", event)


def publish_to_mqtt(topic: str, payload: str) -> None:
    """
    Helper for publishing from registry
    """
    if ctx.mqtt_client is None:
        logger.error("MQTT Client not initialized")
        return

    if not ctx.mqtt_client.is_connected():
        logger.warning("MQTT Client not connected, dropping message to %s", topic)
        return
    try:
        ctx.mqtt_client.publish(topic, payload)
    except Exception as e:
        logger.error("MQTT Failed to publish to %s: %s", topic, e)


# ---------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------


def mqtt_on_connect(
    client: mqtt_client.Client, userdata: Any, flags: Dict[str, Any], rc: int
) -> None:
    if rc != 0:
        logger.error(f"MQTT Connection failed with code: {rc}")
        return

    # Check if registry is ready
    if ctx.registry is None or not hasattr(ctx.registry, "topic2info"):
        logger.error("MQTT Registry not ready, no topics to subscribe")
        return

    # subscribe to every topic from registry
    for t in ctx.registry.topic2info.keys():
        client.subscribe(t, qos=0)
        logger.info(f"MQTT Subscribed to {t}")


def mqtt_on_disconnect(client: mqtt_client.Client, userdata: Any, rc: int) -> None:
    logger.warning("MQTT Disconnected with code %s", rc)


def mqtt_on_message(
    client: mqtt_client.Client, userdata: Any, message: mqtt_client.MQTTMessage
) -> None:
    if ctx.registry is None:
        logger.debug("MQTT Registry not available, ignoring message")
        return

    topic_str = message.topic
    try:
        payload_str = message.payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        logger.warning("MQTT Cannot decode payload in topic '%s'", message.topic)
        logger.debug("MQTT Raw bytes: %s", message.payload)
        return

    logger.debug(f"MQTT Incoming from topic '{topic_str}':")
    logger.debug(f"       - Size   : '{len(message.payload)}'")
    logger.debug(f"       - Message: '{payload_str}'")

    ctx.registry.forward_mqtt_to_yandex(topic_str, payload_str)


def generate_client_id(prefix: str = "wb-alice-client") -> str:
    """
    Generate unique MQTT client ID with random suffix
    """
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return f"{prefix}-{suffix}"


def setup_mqtt_client() -> mqtt_client.Client:
    """
    Create and configure MQTT client with event handlers
    """
    client = mqtt_client.Client(client_id=generate_client_id())
    client.on_connect = mqtt_on_connect
    client.on_disconnect = mqtt_on_disconnect
    client.on_message = mqtt_on_message
    return client


# ---------------------------------------------------------------------
# SocketIO callbacks
# ---------------------------------------------------------------------


async def connect() -> None:
    logger.info("Success connected to Socket.IO server!")
    await ctx.sio.emit(
        "message", {"controller_sn": ctx.controller_sn, "status": "online"}
    )


async def disconnect() -> None:
    """
    Triggered when SocketIO connection with server is lost

    NOTE: argument "reason" implemented in version 5.12, but not accessible
    in current client 5.0.3 (Released in Dec 14,2020)
    """
    logger.warning("SocketIO connection lost")


async def response(data: Any) -> None:
    logger.info(f"SocketIO server response: {data}")


async def error(data: Any) -> None:
    logger.info(f"SocketIO server error: {data}")


async def connect_error(data: Dict[str, Any]) -> None:
    """
    Called when initial connection to server fails
    """
    logger.warning("Connection refused by server: %s", data)


async def any_unprocessed_event(event: str, sid: str, data: Any) -> None:
    """
    Fallback handler for Socket.IO events that don't have specific handlers
    """
    logger.info(f"SocketIO not handled event {event}")


async def on_alice_devices_list(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handles a device discovery request from the server
    Returns a list of devices defined in the controller config
    """
    logger.debug("Received 'alice_devices_list' event:")
    logger.debug(json.dumps(data, ensure_ascii=False, indent=2))
    req_id: str = data.get("request_id")

    if ctx.registry is None:
        logger.error("Registry not available for device list")
        return {"request_id": req_id, "payload": {"devices": []}}

    devices_list: List[Dict[str, Any]] = ctx.registry.build_yandex_devices_list()
    if not devices_list:
        logger.warning("No devices found in configuration")

    devices_response: Dict[str, Any] = {
        "request_id": req_id,
        "payload": {
            # "user_id" will be added on the server proxy side
            "devices": devices_list,
        },
    }

    logger.info("Sending device list response to Yandex:")
    logger.info(json.dumps(devices_response, ensure_ascii=False, indent=2))
    return devices_response


async def on_alice_devices_query(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handles a Yandex request to retrieve the current state of devices.
    """
    logger.info("alice_devices_query event:")
    logger.info(json.dumps(data, ensure_ascii=False, indent=2))

    request_id = data.get("request_id", "unknown")
    devices_response: List[Dict[str, Any]] = []

    for dev in data.get("devices", []):
        device_id = dev.get("id")
        logger.info(f"Try get data for device: '{device_id}'")
        devices_response.append(await ctx.registry.get_device_current_state(device_id))

    query_response = {
        "request_id": request_id,
        "payload": {
            "devices": devices_response,
        },
    }

    logger.info("answer devices query to Yandex:")
    logger.info(json.dumps(query_response, ensure_ascii=False, indent=2))
    return query_response


def handle_single_device_action(device: Dict[str, Any]) -> Dict[str, Any]:
    """
    Processes all capabilities for a single device and returns the result block,
    formatted according to Yandex Smart Home action response spec.
    """
    device_id: str = device.get("id", "")
    if not device_id:
        logger.warning("Device block missing 'id': %s", device)
        return {}

    cap_results: List[Dict[str, Any]] = []
    for cap in device.get("capabilities", []):
        cap_type: str = cap.get("type")
        instance: str = cap.get("state", {}).get("instance")
        value: Any = cap.get("state", {}).get("value")

        try:
            ctx.registry.forward_yandex_to_mqtt(device_id, cap_type, instance, value)
            logger.info("Action applied to %s: %s = %s", device_id, instance, value)
            status = "DONE"
        except Exception as e:
            logger.exception("Failed to apply action for device '%s'", device_id)
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


async def on_alice_devices_action(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handles a device action request from Yandex (e.g., turn on/off)
    Applies the command to the device and returns the result
    """
    logger.debug("Received 'alice_devices_action' event")

    logger.debug("Full payload:")
    logger.debug(json.dumps(data, ensure_ascii=False, indent=2))

    request_id: str = data.get("request_id", "unknown")
    devices_in: List[Dict[str, Any]] = data.get("payload", {}).get("devices", [])
    devices_info: List[Dict[str, Any]] = []

    for device in devices_in:
        result = handle_single_device_action(device)
        if result:
            devices_info.append(result)

    action_response: Dict[str, Any] = {
        "request_id": request_id,
        "payload": {"devices": devices_info},
    }

    logger.debug("Sending action response:")
    logger.debug(json.dumps(action_response, ensure_ascii=False, indent=2))
    return action_response


def bind_socketio_handlers(sock: socketio.AsyncClient) -> None:
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


def get_controller_sn() -> Optional[str]:
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


def read_config() -> Optional[Dict[str, Any]]:
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


async def connect_controller(
    sock: socketio.AsyncClient, config: Dict[str, Any]
) -> None:
    """
    Connect to SocketIO server using provided configuration
    """
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
    logger.warning("Signal %s received at %s – shutting down…", sig.name, ts)

    # ctx.stop_event is created in main() before signal handlers are registered,
    # but we keep the guard just in case.
    if ctx.stop_event is not None and not ctx.stop_event.is_set():
        ctx.stop_event.set()


async def main() -> None:
    ctx.main_loop = asyncio.get_running_loop()

    ctx.stop_event = asyncio.Event()  # keeps the loop alive until a signal arrives
    ctx.main_loop.add_signal_handler(signal.SIGINT, _log_and_stop, signal.SIGINT)
    ctx.main_loop.add_signal_handler(signal.SIGTERM, _log_and_stop, signal.SIGTERM)

    config = read_config()
    if not config:
        logger.error("Cannot proceed without configuration")
        return

    if not config.get("client_enabled", False):
        logger.info(
            "Client is disabled in configuration. Waiting for shutdown signal..."
        )
        logger.info("To enable Alice integration, set 'client_enabled': true in config")
        # Just wait for shutdown signal without doing anything
        await ctx.stop_event.wait()
        return

    try:
        ctx.registry = DeviceRegistry(
            "/etc/wb-mqtt-alice-devices.conf",
            send_to_yandex=send_to_yandex_state,
            publish_to_mqtt=publish_to_mqtt,
        )
        logger.info(f"Registry created with {len(ctx.registry.devices)} devices")
    except Exception as e:
        logger.error(f"Failed to create registry: {e}")
        logger.info("Continuing without device configuration")
        ctx.registry = None

    # Connect to local MQTT broker (assuming Wiren Board default: localhost:1883)
    ctx.mqtt_client = setup_mqtt_client()
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
    set_emit_callback(_emit_async)

    # Explicitly set the loop to avoid "attached to a different loop" errors
    ctx.sio._loop = ctx.main_loop
    bind_socketio_handlers(ctx.sio)

    logger.info("Connecting Socket.IO client...")
    await connect_controller(ctx.sio, config)
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
    logger.info("Cancelling %d pending tasks…", len(pending))
    for task in pending:
        task.cancel()

    # Gather only if something is pending
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info("%d tasks cancelled", len(pending))

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
