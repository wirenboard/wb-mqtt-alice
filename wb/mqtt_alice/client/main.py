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
import http.client
import json
import logging
import random
import signal
import socket
import string
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, Optional, Tuple

import engineio
import paho.mqtt.client as mqtt_client
import socketio
from pydantic import ValidationError
from wb.mqtt_alice.common.constants import (
    CLIENT_CONFIG_PATH,
    DEVICE_PATH,
    SERVER_CONFIG_PATH,
    SHORT_SN_PATH,
)
from wb.mqtt_alice.common.models import ClientConfig, Config

from .device_registry import DeviceRegistry
from .sio_alice_handlers import SioAliceHandlers
from .sio_connection_manager import SioConnectionManager
from .wb_alice_device_state_sender import AliceDeviceStateSender
from .yandex_handlers import convert_to_yandex_block, set_emit_callback

# Configuration constants
MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 60
LOCAL_PROXY_HOST = "localhost"
LOCAL_PROXY_PORT = 8042
LOCAL_PROXY_URL = f"http://{LOCAL_PROXY_HOST}:{LOCAL_PROXY_PORT}"
SOCKETIO_PATH = "/socket.io"

# Timeouts
READ_TOPIC_TIMEOUT = 1.0
RECONNECT_DELAY_INITIAL = 2
RECONNECT_DELAY_MAX = 60

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)

try:
    # Client and server MUST have same Socket.IO versions = 5.0.3
    logger.debug("Socket.IO module path: %s", socketio.__file__)
    logger.debug("python-socketio version: %r", version("python-socketio"))
except PackageNotFoundError:
    logger.warning("python-socketio is not installed.")

try:
    # Client and server MUST have same Engine.IO versions = 4.0.0
    logger.debug("Engine.IO module path: %s", engineio.__file__)
    logger.debug("python-engineio version: %r", version("python-engineio"))
except PackageNotFoundError:
    logger.warning("python-engineio is not installed.")


class AppContext:
    def __init__(self):
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None
        """
        Global asyncio event loop, used to safely schedule
        coroutines from non-async threads (e.g. MQTT callbacks)
        via `asyncio.run_coroutine_threadsafe()`
          - stop_event - Event that signals the main loop to wake up and initiate shutdown
          - sio_manager - SocketIO connection manager for handling real-time communication
        """

        self.graceful_shutdown_requested: bool = False
        """
        Flag indicating shutdown (Ctrl+C, SIGTERM) vs reconnectable disconnect
        """

        self.stop_event: Optional[asyncio.Event] = None
        self.mqtt_connected_event: Optional[asyncio.Event] = None
        self.mqtt_auth_failed_event: Optional[asyncio.Event] = None
        self.mqtt_failed_event: Optional[asyncio.Event] = None
        self.mqtt_outage_logged: bool = False
        self.sio_manager: Optional[SioConnectionManager] = None
        self.sio_handlers: Optional[SioAliceHandlers] = None
        self.registry: Optional[DeviceRegistry] = None
        self.mqtt_client: Optional[mqtt_client.Client] = None
        self.controller_sn: Optional[str] = None
        self.time_rate_sender: Optional[AliceDeviceStateSender] = None
        self.client_pkg_ver: Optional[str] = None


ctx = AppContext()


def _log_emit_exception(event: str, future: asyncio.Future) -> None:
    """Log exception from emit future if any occurred"""
    exc = future.exception()
    if exc:
        logger.error("Emit %r failed: %r", event, exc, exc_info=True)


def _emit_async(event: str, data: Dict[str, Any]) -> None:
    """
    Safely schedules a Socket.IO event to be emitted
    from any thread (async or not).
    """
    if not ctx.sio_manager or not ctx.sio_manager.client:
        logger.warning("Emit blocked: Socket.IO client not init (event = %r)", event)
        logger.debug("            Payload: %r", json.dumps(data))
        return None

    if not ctx.sio_manager.is_connected():
        conn_info = ctx.sio_manager.get_connection_info()
        logger.warning(
            "Emit blocked: Socket.IO not ready (info=%r, event=%r)",
            conn_info,
            event,
        )
        logger.debug("            Payload: %r", json.dumps(data))
        return None

    logger.debug("Attempting to emit %r with payload: %r", event, json.dumps(data))
    sio_client = ctx.sio_manager.client

    try:
        # We're in an asyncio thread - safe to call create_task directly
        asyncio.get_running_loop()
        asyncio.create_task(sio_client.emit(event, data))
        logger.debug("Scheduled emit %r via asyncio task", event)

    except RuntimeError:
        # No running loop in current thread - fallback to ctx.main_loop
        logger.debug("No running loop in current thread - using ctx.main_loop")

        if ctx.main_loop is None:
            logger.warning("ctx.main_loop not available - dropping event %r", event)
            return None

        if ctx.main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(sio_client.emit(event, data), ctx.main_loop)
            fut.add_done_callback(lambda f: _log_emit_exception(event, f))
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
    if rc in (4, 5):
        logger.error("MQTT authentication failed with code %r", rc)
        if ctx.main_loop and ctx.mqtt_auth_failed_event:
            ctx.main_loop.call_soon_threadsafe(ctx.mqtt_auth_failed_event.set)
        return None

    if rc in (1, 2):
        logger.error("MQTT connection failed with code %r", rc)
        if ctx.main_loop and ctx.mqtt_failed_event:
            ctx.main_loop.call_soon_threadsafe(ctx.mqtt_failed_event.set)
        return None

    if rc != 0:
        if not ctx.mqtt_outage_logged:
            logger.error("MQTT connection failed with code %r; retrying", rc)
            ctx.mqtt_outage_logged = True
        return None

    if ctx.mqtt_outage_logged:
        logger.info("MQTT connection restored")
        ctx.mqtt_outage_logged = False
    else:
        logger.info("MQTT connected")

    if ctx.main_loop and ctx.mqtt_connected_event:
        ctx.main_loop.call_soon_threadsafe(ctx.mqtt_connected_event.set)

    if ctx.sio_manager and ctx.sio_manager.is_connected() and ctx.sio_handlers:
        ctx.sio_handlers.subscribe_registry_topics()


def mqtt_on_disconnect(client: mqtt_client.Client, userdata: Any, rc: int) -> None:
    if rc != 0 and not ctx.mqtt_outage_logged:
        logger.warning("MQTT disconnected with code %r; retrying", rc)
        ctx.mqtt_outage_logged = True


def mqtt_on_message(client: mqtt_client.Client, userdata: Any, message: mqtt_client.MQTTMessage) -> None:
    if ctx.registry is None:
        logger.debug("MQTT Registry not available, ignoring message")
        return None

    if message.retain:
        logger.debug("MQTT Ignoring retained message from %r", message.topic)
        # This fix needed for not get new messages in first second after connect
        return None

    topic_str = message.topic
    if not ctx.time_rate_sender or not ctx.time_rate_sender.running:
        logger.debug(
            "time_rate_sender is not running, drop message from %r",
            topic_str,
        )
        return None

    try:
        payload_str = message.payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        logger.warning("MQTT Cannot decode payload in topic %r", message.topic)
        logger.debug("MQTT Raw bytes: %r", message.payload)
        return None

    logger.debug("MQTT Incoming from topic %r:", topic_str)
    logger.debug("       - Size   : %r", len(message.payload))
    logger.debug("       - Message: %r", payload_str)

    # Pass message to wb_alice_device_state_sender
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
    - Called during initial setup
    - Not during reconnection
    """
    client = mqtt_client.Client(client_id=generate_client_id())
    client.on_connect = mqtt_on_connect
    client.on_disconnect = mqtt_on_disconnect
    client.on_message = mqtt_on_message
    return client


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


def read_config(filename: str) -> Dict[str, Any]:
    """
    Read configuration from file which is generated by WEBUI
    """
    with open(filename, "r", encoding="utf-8") as file:
        config = json.load(file)

    if not isinstance(config, dict):
        raise ValueError(f"Configuration root in {filename!r} must be an object")
    return config


def test_nginx_http_response(
    host: str = LOCAL_PROXY_HOST, port: int = LOCAL_PROXY_PORT, timeout: int = 5
) -> Tuple[bool, Optional[int], float, str]:
    """
    Test connection to server via local nginx proxy before Socket.IO connect

    Return tuple:
      ok: bool  # True only if we got the expected 422 response
      http_status: Optional[int]
      elapsed_s: float  # Request duration in seconds
      msg: str
    """
    method = "POST"
    path = "/api/v1/controller/link"

    start_t = time.perf_counter()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

        # Send empty body to get 422 status code
        conn.request(method, path)
        response = conn.getresponse()

        status = response.status
        reason = response.reason

        raw_body = response.read()
        conn.close()
        elapsed_s = time.perf_counter() - start_t

        logger.debug(
            "HTTP probe response: %s %s -> %s %s (%.3fs), raw_body=%r",
            method,
            path,
            status,
            reason,
            elapsed_s,
            raw_body,
        )

        # We need get correct answer:
        # 422 - server work correctly, but see empty body
        if status == 422:
            return True, status, elapsed_s, f"Nginx: {status}"

        # Any other status means proxy answered, but unexpected state.
        return False, status, elapsed_s, f"Nginx unexpected status: {status}"

    except socket.timeout:
        elapsed_s = time.perf_counter() - start_t
        return False, None, elapsed_s, f"TIMEOUT (>{timeout}s) - proxy or DNS resolution issue"

    except ConnectionRefusedError:
        elapsed_s = time.perf_counter() - start_t
        return False, None, elapsed_s, "Connection refused"

    except Exception as e:
        elapsed_s = time.perf_counter() - start_t
        return False, None, elapsed_s, f"Error: {type(e).__name__}: {e}"


async def probe_nginx_until_stable(
    *,
    max_attempts: int = 5,
    acceptable_latency_s: float = 1.5,
    per_attempt_timeout_s: int = 10,
    sleep_between_attempts_s: float = 7,  # [Chip work 0.1s - TTL may be 30s]
) -> bool:
    """
    Probe local nginx multiple times before opening the long-lived Socket.IO
    connection

    Rationale:
    - After nginx reload or DNS TTL expiry, the very first upstream request
      can block 6–20s while DNS/TLS warms up
    - If we call sio.connect() in that window, it may hit its 10s timeout
      and we treat startup as failed
    - So we "pre-flight" nginx here: POST /api/v1/controller/link, expect fast
      HTTP 422 (< acceptable_latency_s). We retry a few times until it's fast

    Returns:
        True  -> nginx responded with expected 422 and acceptable latency
        False -> nginx is slow/broken even after retries (error is already logged here)
    """
    logger.debug("Testing HTTP via proxy, warming up DNS/TLS path...")

    last_status_code: Optional[int] = None
    last_elapsed_time: Optional[float] = None
    last_msg: Optional[str] = None

    for attempt in range(1, max_attempts + 1):
        http_ok, status_code, elapsed_time, http_msg = test_nginx_http_response(timeout=per_attempt_timeout_s)

        last_status_code = status_code
        last_elapsed_time = elapsed_time
        last_msg = http_msg

        if http_ok and elapsed_time <= acceptable_latency_s:
            # nginx is alive, correct upstream, and now it's "warm" (fast)
            return True

        await asyncio.sleep(sleep_between_attempts_s)

    logger.warning(
        "Nginx upstream probe failed (upstream not ready or 5xx) "
        "(last_latency=%.3fs, last_status=%r, last_info=%r)",
        last_elapsed_time,
        last_status_code,
        last_msg,
    )
    return False


async def connect_controller(server_address: str, reconnection_interval_min: int) -> bool:
    """
    Create and connect Socket.IO client to Alice integration server
    with using provided configuration

    Returns:
        True if connection successful, False otherwise
    """
    ctx.controller_sn = get_controller_sn()
    if not ctx.controller_sn:
        logger.error("Cannot proceed without controller serial number")
        return False
    logger.info("Controller SN: %r", ctx.controller_sn)

    # ARCHITECTURE NOTE: We always connect to localhost:8042 where Nginx proxy runs.
    # Nginx forwards requests to the actual server specified in 'server_address'.
    # This allows for:
    # - SSL termination at Nginx level
    # - Certificate-based authentication
    # See configure-nginx-proxy.sh for Nginx configuration details.
    logger.info("Target SocketIO server: %r", server_address)
    logger.info("Client version: %r", ctx.client_pkg_ver)
    logger.debug("Connecting via Nginx proxy: %r", LOCAL_PROXY_URL)

    # Create manager + handlers BEFORE nginx probe, for custom reconnect
    # logic may be started even if pre-flight checks fail
    is_debug_log_enabled = logger.getEffectiveLevel() == logging.DEBUG
    ctx.sio_manager = SioConnectionManager(
        server_url=LOCAL_PROXY_URL,
        socketio_path=SOCKETIO_PATH,
        controller_sn=ctx.controller_sn,
        client_pkg_ver=ctx.client_pkg_ver,
        reconnection=True,  # auto-reconnect ON
        reconnection_delay=RECONNECT_DELAY_INITIAL,  # first reconnect delay
        reconnection_delay_max=RECONNECT_DELAY_MAX,
        debug_logging=is_debug_log_enabled,
        custom_reconnect_enabled=True,
        custom_reconnect_interval=reconnection_interval_min * 60,
    )

    ctx.sio_handlers = SioAliceHandlers(
        registry=ctx.registry,
        controller_sn=ctx.controller_sn,
        mqtt_client=ctx.mqtt_client,
        time_rate_sender=ctx.time_rate_sender,
    )
    # Register handlers BEFORE connecting
    ctx.sio_handlers.register_with_manager(ctx.sio_manager)
    logger.debug("Socket.IO handlers registered")

    logger.debug("Waiting for nginx proxy to be ready...")
    # This only informative, soft check need for can start custom reconnect
    # in case when server totally offline
    if not await wait_for_nginx_ready(timeout=15):
        logger.error("Nginx is not ready after 15 seconds")
    await probe_nginx_until_stable()

    # Connect with infinite attempts (0 = infinite)
    # NOTE: Connect to local Nginx proxy which forwards to actual server
    #       "controller_sn" is passed via SSL certificate when Nginx proxies
    return await ctx.sio_manager.connect(connection_attempts=0)


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

    # NOTE: Shutdown sequence:
    #       - First stop time_rate_sender so we stop generating new outbound updates
    #       - Always shutdown Socket.IO connections last
    #         This need because time_rate_sender use mqtt and mqtt use need use Socket.IO connections

    # Stop AliceDeviceEventRate
    if ctx.time_rate_sender and ctx.time_rate_sender.running:
        logger.info("Stopping AliceDeviceEventRate...")
        await ctx.time_rate_sender.stop()
        await asyncio.sleep(0.1)

    # Disconnect Socket.IO
    if ctx.sio_manager:
        try:
            await asyncio.wait_for(ctx.sio_manager.disconnect_client(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Socket.IO disconnect timeout")

    # Stop MQTT client
    if ctx.mqtt_client:
        logger.info("Stopping MQTT client...")
        try:
            ctx.mqtt_client.disconnect()
            ctx.mqtt_client.loop_stop()
        except Exception as e:
            logger.warning("Error during MQTT disconnect: %r", e)
    logger.info("MQTT disconnected")

    logger.info("Graceful shutdown completed")


async def wait_for_mqtt() -> int:
    """
    Wait until the local MQTT broker accepts the connection or shutdown is requested.
    """
    while not ctx.stop_event.is_set():
        try:
            result = ctx.mqtt_client.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
            if result != mqtt_client.MQTT_ERR_SUCCESS:
                raise ConnectionError(f"MQTT connect returned code {result}")
            ctx.mqtt_client.loop_start()
            break
        except (ConnectionError, OSError) as e:
            if not ctx.mqtt_outage_logged:
                logger.error("MQTT broker is unavailable (%s); retrying", e)
                ctx.mqtt_outage_logged = True
            try:
                await asyncio.wait_for(ctx.stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                continue

    if ctx.stop_event.is_set():
        return 7

    connected_task = asyncio.create_task(ctx.mqtt_connected_event.wait())
    auth_failed_task = asyncio.create_task(ctx.mqtt_auth_failed_event.wait())
    mqtt_failed_task = asyncio.create_task(ctx.mqtt_failed_event.wait())
    stop_task = asyncio.create_task(ctx.stop_event.wait())
    done, pending = await asyncio.wait(
        {connected_task, auth_failed_task, mqtt_failed_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    if auth_failed_task in done:
        return 2
    if mqtt_failed_task in done:
        return 1
    if stop_task in done:
        return 7
    return 0


async def wait_for_controller(server_address: str, reconnection_interval_min: int) -> int:
    """
    Connect to Alice cloud while still reacting to MQTT auth failures and signals.
    """
    connection_task = asyncio.create_task(connect_controller(server_address, reconnection_interval_min))
    auth_failed_task = asyncio.create_task(ctx.mqtt_auth_failed_event.wait())
    mqtt_failed_task = asyncio.create_task(ctx.mqtt_failed_event.wait())
    stop_task = asyncio.create_task(ctx.stop_event.wait())
    done, pending = await asyncio.wait(
        {connection_task, auth_failed_task, mqtt_failed_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if connection_task in done:
        connected = await connection_task
        if not connected:
            logger.error("Socket.IO connection stopped unexpectedly")
            result = 1
        else:
            logger.info("Client initialization completed")
            done, pending = await asyncio.wait(
                {auth_failed_task, mqtt_failed_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if auth_failed_task in done:
                result = 2
            elif mqtt_failed_task in done:
                result = 1
            else:
                result = 7
    else:
        connection_task.cancel()
        await asyncio.gather(connection_task, return_exceptions=True)
        if auth_failed_task in done:
            result = 2
        elif mqtt_failed_task in done:
            result = 1
        else:
            result = 7

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return result


async def main() -> int:

    # Early register signal handlers for graceful shutdown
    ctx.stop_event = asyncio.Event()  # Keeps the loop alive until a signal arrives
    ctx.mqtt_connected_event = asyncio.Event()
    ctx.mqtt_auth_failed_event = asyncio.Event()
    ctx.mqtt_failed_event = asyncio.Event()
    ctx.mqtt_outage_logged = False
    ctx.main_loop = asyncio.get_running_loop()
    ctx.main_loop.add_signal_handler(signal.SIGINT, _log_and_stop, signal.SIGINT)
    ctx.main_loop.add_signal_handler(signal.SIGTERM, _log_and_stop, signal.SIGTERM)

    try:
        server_cfg = read_config(SERVER_CONFIG_PATH)
        server_address = server_cfg.get("server_address")
        if not isinstance(server_address, str) or not server_address:
            raise ValueError(f"'server_address' is not specified in {SERVER_CONFIG_PATH!r}")

        client_cfg = ClientConfig(**read_config(CLIENT_CONFIG_PATH))
        log_level_name = client_cfg.log_level.upper()
        if log_level_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Unsupported log_level {client_cfg.log_level!r}")
        if client_cfg.reconnection_interval_min <= 0:
            raise ValueError("reconnection_interval_min must be positive")
        Config(**read_config(DEVICE_PATH))
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as e:
        logger.error("Invalid configuration: %s", e)
        return 6

    # Apply log level from client config
    logging.getLogger().setLevel(log_level_name)

    if not client_cfg.client_enabled:
        logger.info("Alice integration is DISABLED in configuration")
        logger.info("To enable integration, set 'client_enabled': true in file %r", CLIENT_CONFIG_PATH)
        return 7
    logger.info("Alice integration is enabled - starting client...")

    # NOTE: Initialize core components order is critical:
    #       1. Create registry - maps devices to MQTT topics
    #          Any actions from Yandex need registry ready map "device to MQTT topic"
    #       2. Next connect to local MQTT brocker
    #          We verify broker is alive and we can publish commands when gen Yandex action
    #          DO NOT subscribe yet on this moment
    #       3. Init Socket.IO connections
    #          When fully ready reciave commands from yandex
    #          Do it now, becoase mqtt and time_rate_sender need use already upped connection
    #       4. time_rate_sender + MQTT subscriptions
    #          Thid start only after Socket.IO connection is ready and we are
    #          can send notifications from us to Yandex server
    ctx.client_pkg_ver = get_client_pkg_ver()
    try:
        ctx.registry = DeviceRegistry(
            cfg_path=DEVICE_PATH,
            send_to_yandex=convert_to_yandex_block,
            publish_to_mqtt=publish_to_mqtt,
        )
        logger.debug("Registry created with %r devices", len(ctx.registry.devices))
    except Exception as e:
        logger.error("Invalid device configuration: %s", e)
        return 6

    # Connect to local MQTT broker (assuming Wiren Board default: localhost:1883)
    ctx.mqtt_client = setup_mqtt_client()
    try:
        result = await wait_for_mqtt()
        if result:
            return result

        # Set emit callback for yandex_handlers module
        set_emit_callback(_emit_async)

        ctx.time_rate_sender = AliceDeviceStateSender(device_registry=ctx.registry)
        logger.info("Connecting Socket.IO client...")

        return await wait_for_controller(server_address, client_cfg.reconnection_interval_min)
    finally:
        await graceful_shutdown()


def run() -> int:
    """
    Run the service and translate unexpected failures to EXIT_FAILURE.
    """
    logger.info("Starting wb-alice-client...")

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C)")
        return 7
    except Exception as e:
        logger.exception("Unhandled exception: %r", e)
        return 1


if __name__ == "__main__":
    sys.exit(run())
