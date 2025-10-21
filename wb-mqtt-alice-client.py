#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Wiren Board Alice Integration Client
This script provides integration between Wiren Board controllers
and "Yandex smart home" platform with Alice

Usage:
    python3 wb-mqtt-alice-client.py
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
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt_client
import socketio

from constants import CONFIG_PATH, DEVICE_PATH, SHORT_SN_PATH
from device_registry import DeviceRegistry
from wb_alice_device_state_sender import AliceDeviceStateSender
from yandex_handlers import send_to_yandex_state, set_emit_callback

# Configuration constants
MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 60
LOCAL_PROXY_URL = "http://localhost:8042"
SOCKETIO_PATH = "/socket.io"

# Timeouts
READ_TOPIC_TIMEOUT = 1.0
RECONNECT_DELAY_INITIAL = 2
RECONNECT_DELAY_MAX = 30

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)

logger.debug("socketio module path: %r", socketio.__file__)
from importlib.metadata import PackageNotFoundError, version

try:
    logger.debug("python-socketio version: %r", version("python-socketio"))
except PackageNotFoundError:
    logger.warning("python-socketio is not installed.")


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
        self.time_rate_sender: Optional[AliceDeviceStateSender] = None
        self.client_pkg_ver: Optional[str] = None


ctx = AppContext()


def _emit_async(event: str, data: Dict[str, Any]) -> None:
    """
    Safely schedules a Socket.IO event to be emitted
    from any thread (async or not).
    """
    if not ctx.sio:
        logger.warning("Socket.IO client not initialized, skipping emit %r", event)
        logger.debug("            Payload: %r", json.dumps(data))
        return None

    if not ctx.sio.connected or getattr(ctx.sio.eio, "state", None) != "connected":
        logger.warning(
            "Socket.IO not ready (state: %s), skipping emit %r",
            getattr(ctx.sio.eio, "state", "unknown"),
            event,
        )
        logger.debug("            Payload: %r", json.dumps(data))
        return None

    logger.debug("Connected status: %r", ctx.sio.connected)

    if not hasattr(ctx.sio, "namespaces") or "/" not in ctx.sio.namespaces:
        logger.warning("Namespace not ready, skipping emit %r", event)
        return None

    # BUG: Additional check for version SocketIO 5.0.3 (may delete when upgrade)
    if hasattr(ctx.sio.eio, "write_loop_task") and ctx.sio.eio.write_loop_task is None:
        logger.warning(
            "Write loop task is None, connection unstable - skipping emit %r",
            event,
        )
        return None

    logger.debug("Attempting to emit %r with payload: %r", event, data)

    try:
        # We're in an asyncio thread - safe to call create_task directly
        asyncio.get_running_loop()
        asyncio.create_task(ctx.sio.emit(event, data))
        logger.debug("Scheduled emit %r via asyncio task", event)

    except RuntimeError:
        # No running loop in current thread - fallback to ctx.main_loop
        logger.debug("No running loop in current thread - using ctx.main_loop")

        if ctx.main_loop is None:
            logger.warning("ctx.main_loop not available - dropping event %r", event)
            return None

        if ctx.main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(ctx.sio.emit(event, data), ctx.main_loop)

            # Log if the future raises an exception
            def log_emit_exception(f: asyncio.Future):
                exc = f.exception()
                if exc:
                    logger.error("Emit %r failed: %r", event, exc, exc_info=True)

            fut.add_done_callback(log_emit_exception)
        else:
            logger.error("ctx.main_loop is not running - cannot emit %r", event)


async def publish_to_mqtt(topic: str, payload: str) -> None:
    """
    Helper for publishing from registry
    """
    if ctx.mqtt_client is None:
        logger.error("MQTT Client not initialized")
        return None

    if not ctx.mqtt_client.is_connected():
        logger.warning("MQTT Client not connected, dropping message to %r", topic)
        return None
    try:
        await asyncio.wait_for(
            asyncio.to_thread(ctx.mqtt_client.publish, topic, payload),
            timeout=2.0,
        )
        logger.debug("Published %r → %r", payload, topic)
    except asyncio.TimeoutError:
        logger.error("MQTT publish timeout for topic %r", topic)
    except Exception as e:
        logger.error("MQTT Failed to publish to %r: %r", topic, e)


# ---------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------


def mqtt_on_connect(client: mqtt_client.Client, userdata: Any, flags: Dict[str, Any], rc: int) -> None:
    if rc != 0:
        logger.error("MQTT Connection failed with code: %r", rc)
        return None

    # Check if registry is ready
    if ctx.registry is None or not hasattr(ctx.registry, "topic2info"):
        logger.error("MQTT Registry not ready, no topics to subscribe")
        return None

    # subscribe to every topic from registry
    for t in ctx.registry.topic2info.keys():
        client.subscribe(t, qos=0)
        logger.debug("MQTT Subscribed to %r", t)


def mqtt_on_disconnect(client: mqtt_client.Client, userdata: Any, rc: int) -> None:
    logger.warning("MQTT Disconnected with code %r", rc)


def mqtt_on_message(client: mqtt_client.Client, userdata: Any, message: mqtt_client.MQTTMessage) -> None:
    if ctx.registry is None:
        logger.debug("MQTT Registry not available, ignoring message")
        return None

    if message.retain:
        logger.debug("MQTT Ignoring retained message from %r", message.topic)
        # This fix needed for not get new messages in first second after connect
        return None

    topic_str = message.topic
    try:
        payload_str = message.payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        logger.warning("MQTT Cannot decode payload in topic %r", message.topic)
        logger.debug("MQTT Raw bytes: %r", message.payload)
        return None

    logger.debug("MQTT Incoming from topic %r:", topic_str)
    logger.debug("       - Size   : %r", len(message.payload))
    logger.debug("       - Message: %r", payload_str)

    # add message to wb_alice_device_state_sender
    asyncio.run_coroutine_threadsafe(ctx.time_rate_sender.add_message(topic_str, payload_str), ctx.main_loop)


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
    await ctx.sio.emit("message", {"controller_sn": ctx.controller_sn, "status": "online"})


async def disconnect() -> None:
    """
    Triggered when SocketIO connection with server is lost

    NOTE: argument "reason" implemented in version 5.12, but not accessible
    in current client 5.0.3 (Released in Dec 14,2020)
    """
    logger.warning("SocketIO connection lost")


async def response(data: Any) -> None:
    logger.debug("SocketIO server response: %r", data)


async def error(data: Any) -> None:
    logger.debug("SocketIO server error: %r", data)


async def connect_error(data: Dict[str, Any]) -> None:
    """
    Called when initial connection to server fails
    """
    logger.warning("Connection refused by server: %r", data)


async def any_unprocessed_event(event: str, sid: str, data: Any) -> None:
    """
    Fallback handler for Socket.IO events that don't have specific handlers
    """
    logger.debug("SocketIO not handled event %r", event)


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
    logger.debug("alice_devices_query event:")
    logger.debug(json.dumps(data, ensure_ascii=False, indent=2))

    request_id = data.get("request_id", "unknown")
    devices_response: List[Dict[str, Any]] = []

    for dev in data.get("devices", []):
        device_id = dev.get("id")
        logger.debug("Try get data for device: %r", device_id)
        devices_response.append(await ctx.registry.get_device_current_state(device_id))

    query_response = {
        "request_id": request_id,
        "payload": {
            "devices": devices_response,
        },
    }

    logger.debug("answer devices query to Yandex:")
    logger.debug(json.dumps(query_response, ensure_ascii=False, indent=2))
    return query_response


async def handle_single_device_action(device: Dict[str, Any]) -> Dict[str, Any]:
    """
    Processes all capabilities for a single device and returns the result block,
    formatted according to Yandex Smart Home action response spec.
    """
    device_id: str = device.get("id", "")
    if not device_id:
        logger.warning("Device block missing 'id': %r", device)
        return {}

    cap_results: List[Dict[str, Any]] = []
    for cap in device.get("capabilities", []):
        cap_type: str = cap.get("type")
        instance: str = cap.get("state", {}).get("instance")
        value: Any = cap.get("state", {}).get("value")

        try:
            await ctx.registry.forward_yandex_to_mqtt(device_id, cap_type, instance, value)
            logger.debug("Action applied to %r: %r = %r", device_id, instance, value)
            status = "DONE"
        except Exception as e:
            logger.exception("Failed to apply action for device %r", device_id)
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
        result = await handle_single_device_action(device)
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
            logger.debug("Read controller ID: %r", controller_sn)
            return controller_sn
    except FileNotFoundError:
        logger.error("Controller ID file not found! Check the path: %r", SHORT_SN_PATH)
        return None
    except Exception as e:
        logger.error("Reading controller ID exception: %r", e)
        return None

def get_client_pkg_ver() -> str:
    """
    Get wb-mqtt-alice package version from Debian system
    
    Returns:
        - Package version string (e.g. '0.5.2~exp~PR+34~3~g1b68346')
        - 'unknown' if unable to determine
    """
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "wb-mqtt-alice"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        version = result.stdout.strip()
        if version:
            logger.debug("wb-mqtt-alice package version: %r", version)
            return version
        logger.warning("dpkg-query returned empty version")
        return "unknown"
    
    except subprocess.CalledProcessError as e:
        logger.warning("Package not found (returncode: %d)", e.returncode)
        return "unknown"
    except subprocess.TimeoutExpired:
        logger.warning("Timeout while querying package version")
        return "unknown"
    except Exception as e:
        logger.warning("Failed to get package version: %r", e)
        return "unknown"


def read_config() -> Optional[Dict[str, Any]]:
    """
    Read configuration from file which is generated by WEBUI
    """
    try:
        if not os.path.exists(CONFIG_PATH):
            logger.error("Configuration file not found at %r", CONFIG_PATH)
            return None

        with open(CONFIG_PATH, "r") as file:
            config = json.load(file)
            return config
    except json.JSONDecodeError:
        logger.error("Parsing configuration file: Invalid JSON format")
        return None
    except Exception as e:
        logger.error("Reading configuration exception: %r", e)
        return None


async def connect_controller(sock: socketio.AsyncClient, config: Dict[str, Any]) -> bool:
    """
    Connect to SocketIO server using provided configuration
    """
    ctx.controller_sn = get_controller_sn()
    if not ctx.controller_sn:
        logger.error("Cannot proceed without controller ID")
        return False

    # ARCHITECTURE NOTE: We always connect to localhost:8042 where Nginx proxy runs.
    # Nginx forwards requests to the actual server specified in 'server_address'.
    # This allows for:
    # - SSL termination at Nginx level
    # - Certificate-based authentication
    # See configure-nginx-proxy.sh for Nginx configuration details.
    server_address = config.get("server_address")  # Used by Nginx proxy
    if not server_address:
        logger.error("'server_address' not specified in configuration")
        return False
    logger.info("Target SocketIO server: %r", server_address)
    logger.info("Client version: %r", ctx.client_pkg_ver)
    logger.info("Connecting via Nginx proxy: %r", LOCAL_PROXY_URL)

    logger.info("Waiting for nginx proxy to be ready...")
    if not await wait_for_nginx_ready():
        logger.error("Nginx proxy not ready - exiting")
        return False

    try:
        # Connect to local Nginx proxy which forwards to actual server
        # "controller_sn" is passed via SSL certificate when Nginx proxies
        await asyncio.wait_for(
            sock.connect(
                LOCAL_PROXY_URL,
                socketio_path=SOCKETIO_PATH,
                headers={
                    "WB-Client-Pkg-Ver": ctx.client_pkg_ver
                }
            ),
            timeout=10.0)
        logger.info("Socket.IO connected successfully via proxy")
        return True

    except (socketio.exceptions.ConnectionError, asyncio.TimeoutError) as e:
        logger.error("Initial Socket.IO connection failed: %r", e)
        return False
    except Exception as e:
        logger.exception("Unexpected exception during connection: %r", e)
        return False


def _log_and_stop(sig: signal.Signals) -> None:
    """
    Generic signal handler:
    1) logs which signal was received;
    2) sets the global ctx.stop_event so the main loop can exit.
    Idempotent: repeated signals after the first one do nothing.
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    logger.warning("Signal %r received at %r - shutting down...", sig.name, ts)

    # ctx.stop_event is created in main() before signal handlers are registered,
    # but we keep the guard just in case.
    if ctx.stop_event is not None and not ctx.stop_event.is_set():
        ctx.stop_event.set()


def check_nginx_status() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "nginx"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip() == "active"

    except subprocess.CalledProcessError:
        # nginx is not active (returncode != 0)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Timeout while checking nginx status")
        return False
    except Exception:
        return False


async def wait_for_nginx_ready(timeout: int = 15) -> bool:
    start_time = time.time()

    while time.time() - start_time < timeout:
        if check_nginx_status():
            logger.debug("Nginx is active")
            return True
        await asyncio.sleep(0.5)

    return False


async def graceful_shutdown() -> None:
    """
    Perform graceful shutdown of Socket.IO client with proper server notification
    """
    logger.info("Starting graceful shutdown...")

    # Stop AliceDeviceEventRate
    if ctx.time_rate_sender.running:
        logger.info("Stopping AliceDeviceEventRate...")
        await ctx.time_rate_sender.stop()
        await asyncio.sleep(0.1)

    # Notify server about going offline
    # This is informative message, not have spesial beckend processing
    if ctx.sio and ctx.sio.connected:
        try:
            logger.info("Notifying server about offline status...")
            await asyncio.wait_for(
                ctx.sio.emit("message", {"controller_sn": ctx.controller_sn, "status": "offline"}),
                timeout=3.0,
            )
            # Give server time to process the offline message
            await asyncio.sleep(0.5)
        except asyncio.TimeoutError:
            logger.warning("Timeout while notifying server about offline status")
        except Exception as e:
            logger.warning("Failed to notify server about offline status: %r", e)

    logger.info("Properly disconnect from Socket.IO server ...")
    # When service stop by user and then fast up
    # We need stop correctly with wait disconnect from server, without it server not connect second controller twice
    if ctx.sio and ctx.sio.connected:
        try:
            logger.info("Disconnecting from Socket.IO server...")
            await asyncio.wait_for(ctx.sio.disconnect(), timeout=5.0)
            logger.info("Socket.IO disconnected successfully")
        except asyncio.TimeoutError:
            logger.warning("Timeout while disconnecting from Socket.IO server")
        except Exception as e:
            logger.warning("Error during Socket.IO disconnect: %r", e)

    # Cancel Socket.IO wait task if still running
    sio_task = getattr(ctx, "sio_task", None)
    if sio_task and not sio_task.done():
        logger.info("Cancelling Socket.IO wait task...")
        sio_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sio_task

    # Stop MQTT client
    if ctx.mqtt_client:
        logger.info("Stopping MQTT client...")
        try:
            ctx.mqtt_client.loop_stop()
            ctx.mqtt_client.disconnect()
        except Exception as e:
            logger.warning("Error during MQTT disconnect: %r", e)
    logger.info("MQTT disconnected")

    # Cancel any remaining asyncio tasks
    pending = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    if pending:
        logger.info("Cancelling %d remaining tasks...", len(pending))
        for task in pending:
            task.cancel()

        # Wait for tasks to finish cancellation
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Some tasks did not cancel within timeout")

    logger.info("Graceful shutdown completed")


async def main() -> int:
    ctx.main_loop = asyncio.get_running_loop()

    ctx.stop_event = asyncio.Event()  # keeps the loop alive until a signal arrives
    ctx.main_loop.add_signal_handler(signal.SIGINT, _log_and_stop, signal.SIGINT)
    ctx.main_loop.add_signal_handler(signal.SIGTERM, _log_and_stop, signal.SIGTERM)
    ctx.client_pkg_ver = get_client_pkg_ver()

    config = read_config()
    if not config:
        logger.error("Cannot proceed without configuration")
        return 0  # 0 mean - exit without service restart

    if not config.get("client_enabled", False):
        logger.info("Client is disabled in configuration. Waiting for shutdown signal...")
        logger.info("To enable Alice integration, set 'client_enabled': true in config")
        # Just wait for shutdown signal without doing anything
        await ctx.stop_event.wait()
        return 0

    try:
        ctx.registry = DeviceRegistry(
            cfg_path=DEVICE_PATH,
            send_to_yandex=send_to_yandex_state,
            publish_to_mqtt=publish_to_mqtt,
        )
        logger.debug("Registry created with %r devices", len(ctx.registry.devices))
    except Exception as e:
        logger.error("Failed to create registry: %r", e)
        logger.info("Continuing without device configuration")
        ctx.registry = None

    # init and start AliceDeviceStateSender
    ctx.time_rate_sender = AliceDeviceStateSender(device_registry=ctx.registry)
    asyncio.run_coroutine_threadsafe(ctx.time_rate_sender.start(), ctx.main_loop)

    # Connect to local MQTT broker (assuming Wiren Board default: localhost:1883)
    ctx.mqtt_client = setup_mqtt_client()
    try:
        ctx.mqtt_client.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
        ctx.mqtt_client.loop_start()
        logger.info(f"Connected to '{MQTT_HOST}' MQTT broker")
    except Exception as e:
        logger.error("MQTT connect failed: %r", e)
        return 0  # 0 mean - exit without service restart

    is_debug_log_enabled = logger.getEffectiveLevel() == logging.DEBUG
    ctx.sio = socketio.AsyncClient(
        logger=is_debug_log_enabled,
        engineio_logger=is_debug_log_enabled,
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
    if not await connect_controller(ctx.sio, config):
        logger.error("Failed to establish initial connection - exiting")
        return 1  # Need exit with error for restart

    # Store the sio_task reference for graceful shutdown
    ctx.sio_task = asyncio.create_task(ctx.sio.wait())

    # Wait for shutdown signal
    await ctx.stop_event.wait()
    logger.info("Shutdown signal received")

    await graceful_shutdown()
    logger.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    logger.info("Starting wb-alice-client...")

    try:
        exit_code = asyncio.run(main(), debug=True)
        if exit_code != 0:
            logger.warning(
                "Service 'main()' returned error code %d - exiting with error",
                exit_code,
            )
            sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C)")
    except SystemExit as e:
        # One place called when code explicitly uses "sys.exit(code)" anywhere
        exit_code = e.code if e.code is not None else 0
        if exit_code == 0:
            logger.info("Service exiting normally (code %d)", exit_code)
        else:
            logger.warning("Service exiting with error (code %d) - systemd will restart", exit_code)
        # Pass SystemExit as-it
        # This needed for service restarted if stop with error from this code
        raise
    except Exception as e:
        logger.exception("Unhandled exception: %r", e)
    finally:
        logger.info("wb-alice-client stopped")
