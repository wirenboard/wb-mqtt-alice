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
from typing import Any, Dict, Optional
import http.client
import socket

import paho.mqtt.client as mqtt_client

from constants import SERVER_CONFIG_PATH, DEVICE_PATH, SHORT_SN_PATH, CLIENT_CONFIG_PATH
from device_registry import DeviceRegistry
from wb_alice_device_state_sender import AliceDeviceStateSender
from yandex_handlers import send_to_yandex_state, set_emit_callback
from socketio_handlers import SocketIOHandlers
from socketio_manager import SocketIOConnectionManager

# Configuration constants
MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 60
LOCAL_PROXY_URL = "http://localhost:8042"
SOCKETIO_PATH = "/socket.io"

# Timeouts
READ_TOPIC_TIMEOUT = 1.0
RECONNECT_DELAY_INITIAL = 2
RECONNECT_DELAY_MAX = 60

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s", force=True)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)


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
        self.sio_manager: Optional[SocketIOConnectionManager] = None
        self.sio_handlers: Optional[SocketIOHandlers] = None
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
        logger.warning(
            "Emit blocked: Socket.IO client not init (event = %r)",
            event
        )
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

    logger.debug("Attempting to emit %r with payload: %r", event, data)
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
            fut = asyncio.run_coroutine_threadsafe(
                sio_client.emit(event, data), ctx.main_loop
            )
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


def subscribe_registry_topics() -> None:
    """
    Subscribe MQTT client to all topics from registry
    Called only after full initialization (Socket.IO connected, etc.)
    
    NOTE: This need only for notify yandex API,
          but not needed for commands from Yandex
    """

    if ctx.mqtt_client is None:
        logger.error("Cannot subscribe: MQTT client is None")
        return

    # Check if registry is ready
    if ctx.registry is None or not hasattr(ctx.registry, "topic2info"):
        logger.error("Cannot subscribe: MQTT Registry not ready")
        return None

    # subscribe to every topic from registry
    for t in ctx.registry.topic2info.keys():
        ctx.mqtt_client.subscribe(t, qos=0)
        logger.debug("MQTT Subscribed to %r", t)


def mqtt_on_connect(client: mqtt_client.Client, userdata: Any, flags: Dict[str, Any], rc: int) -> None:
    if rc != 0:
        logger.error("MQTT Connection failed with code: %r", rc)
        return None

    logger.info("MQTT connected - no subscriptions on this moment")


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
    if ctx.time_rate_sender:
        asyncio.run_coroutine_threadsafe(
            ctx.time_rate_sender.add_message(topic_str, payload_str),
            ctx.main_loop
        )
    else:
        logger.debug("time_rate_sender is not ready, drop message from %r", topic_str)


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


def read_config(filename: str) -> Optional[Dict[str, Any]]:
    """
    Read configuration from file which is generated by WEBUI
    """
    try:
        if not os.path.exists(filename):
            logger.error("Integration configuration file not found at %r", filename)
            return None

        with open(filename, "r") as file:
            config = json.load(file)
            return config
    except json.JSONDecodeError:
        logger.error("Parsing configuration file: Invalid JSON format")
        return None
    except Exception as e:
        logger.error("Reading configuration exception: %r", e)
        return None


def test_nginx_http_response(
    host: str = 'localhost',
    port: int = 8042,
    timeout: int = 5
) -> tuple[bool, Optional[int], float, str]:
    """
    Test connection to server via local nginx proxy before Socket.IO connect

    Return tuple:
      ok: bool  # True only if we got the expected 422 response
      http_status: Optional[int]
      elapsed_s: float  # Request duration in seconds
      msg: str
    """
    method = "POST"
    path = "/request-registration"
    
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
            method, path, status, reason, elapsed_s, raw_body
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
    sleep_between_attempts_s: float = 7, # [Chip work 0.1s - TTL may be 30s]
) -> bool:
    """
    Probe local nginx multiple times before opening the long-lived Socket.IO
    connection

    Rationale:
    - After nginx reload or DNS TTL expiry, the very first upstream request
      can block 6–20s while DNS/TLS warms up
    - If we call sio.connect() in that window, it may hit its 10s timeout
      and we treat startup as failed
    - So we "pre-flight" nginx here: POST /request-registration, expect fast
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
        http_ok, status_code, elapsed_time, http_msg = test_nginx_http_response(
            timeout=per_attempt_timeout_s
        )

        last_status_code = status_code
        last_elapsed_time = elapsed_time
        last_msg = http_msg

        if http_ok and elapsed_time <= acceptable_latency_s:
            # nginx is alive, correct upstream, and now it's "warm" (fast)
            return True

        await asyncio.sleep(sleep_between_attempts_s)

    logger.error(
        "Aborting init: Nginx probe FAILED before Socket.IO connect "
        "(last_latency=%.3fs, last_status=%r, last_info=%r)",
        last_elapsed_time,
        last_status_code,
        last_msg,
    )
    return False


async def connect_controller(config: Dict[str, Any]) -> bool:
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
    server_address = config.get("server_address")  # Used by Nginx proxy
    if not server_address:
        logger.error("'server_address' not specified in configuration")
        return False
    logger.info("Target SocketIO server: %r", server_address)
    logger.info("Client version: %r", ctx.client_pkg_ver)
    logger.debug("Connecting via Nginx proxy: %r", LOCAL_PROXY_URL)

    logger.debug("Waiting for nginx proxy to be ready...")
    if not await wait_for_nginx_ready(timeout=15):
        logger.error("Nginx is not ready after 15 seconds")
        return False

    if not await probe_nginx_until_stable():
        return False

    # Define callbacks for connection lifecycle
    def on_disconnect_detected():
        """
        Callback when Socket.IO disconnect is detected
        """
        logger.warning(
            "Socket.IO disconnected - stopping MQTT state updates"
        )
        
        if ctx.time_rate_sender and ctx.time_rate_sender.running:
            try:
                # Schedule stop in event loop
                if ctx.main_loop and ctx.main_loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        ctx.time_rate_sender.stop(), ctx.main_loop
                    )
                    logger.info("Stopped Alice state sender due to disconnect")
            except Exception as e:
                logger.exception("Failed to stop sender: %r", e)

    def on_reconnect_success():
        """Called after successful reconnection - restart MQTT processing"""
        logger.info(
            "Socket.IO reconnected - resuming MQTT state updates"
        )
        
        if ctx.time_rate_sender and not ctx.time_rate_sender.running:
            try:
                # Schedule start in event loop
                if ctx.main_loop and ctx.main_loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        ctx.time_rate_sender.start(), ctx.main_loop
                    )
                    subscribe_registry_topics()
                    logger.info("Restarted Alice state sender after reconnection")
            except Exception as e:
                logger.exception("Failed to restart sender: %r", e)

    is_debug_log_enabled = logger.getEffectiveLevel() == logging.DEBUG
    ctx.sio_manager = SocketIOConnectionManager(
        server_url=LOCAL_PROXY_URL,
        socketio_path=SOCKETIO_PATH,
        controller_sn=ctx.controller_sn,
        client_pkg_ver=ctx.client_pkg_ver,
        reconnection=True,  # auto-reconnect ON
        reconnection_delay=RECONNECT_DELAY_INITIAL,  # first recconect delay
        reconnection_delay_max=RECONNECT_DELAY_MAX,
        debug_logging=is_debug_log_enabled,
        custom_reconnect_enabled=True,
        custom_reconnect_interval=1200,  # 20 minutes
        on_disconnect_callback=on_disconnect_detected,
        on_reconnect_success_callback=on_reconnect_success,
    )

    ctx.sio_handlers = SocketIOHandlers(
        registry=ctx.registry,
        controller_sn=ctx.controller_sn,
    )
    # Register handlers BEFORE connecting
    ctx.sio_handlers.register_with_manager(ctx.sio_manager)
    logger.debug("Socket.IO handlers registered")

    return await ctx.sio_manager.connect()


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
    #         This need becoase time_rate_sender use mqtt and mqtt use need use Socket.IO connections

    # Stop AliceDeviceEventRate
    if ctx.time_rate_sender and ctx.time_rate_sender.running:
        logger.info("Stopping AliceDeviceEventRate...")
        await ctx.time_rate_sender.stop()
        await asyncio.sleep(0.1)

    # Disconnect Socket.IO
    if ctx.sio_manager:
        await ctx.sio_manager.disconnect()

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

    # Early register signal handlers for graceful shutdown
    ctx.stop_event = asyncio.Event() # Keeps the loop alive until a signal arrives
    ctx.main_loop = asyncio.get_running_loop()
    ctx.main_loop.add_signal_handler(signal.SIGINT, _log_and_stop, signal.SIGINT)
    ctx.main_loop.add_signal_handler(signal.SIGTERM, _log_and_stop, signal.SIGTERM)

    config = read_config(SERVER_CONFIG_PATH)
    if not config:
        logger.error("Cannot proceed without configuration")
        return 0  # 0 mean - exit without service restart

    if not config.get("client_enabled", False):
        logger.info("Alice integration is DISABLED in configuration")
        logger.info(
            "To enable integration, set 'client_enabled': true in file %r",
            SERVER_CONFIG_PATH
        )
        return 0  # 0 mean - exit without service restart
    logger.info("Alice integration is enabled - starting client...")

    # NOTE: Initialization sequence:
    #       - First create registry
    #         Any actions from Yandex need registry ready to map "device -> MQTT topic"
    #       - Next connect to local MQTT brocker
    #         We verify broker is alive and we can publish commands when gen Yandex action
    #         DO NOT subscribe yet on this moment
    #       - Init Socket.IO connections
    #         When fully ready reciave commands from yandex
    #         Do it now, becoase mqtt and time_rate_sender need use already upped connection

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
            send_to_yandex=send_to_yandex_state,
            publish_to_mqtt=publish_to_mqtt,
        )
        logger.debug("Registry created with %r devices", len(ctx.registry.devices))
    except Exception as e:
        logger.error("Failed to create registry: %r", e)
        logger.info("Continuing without device configuration")
        ctx.registry = None

    # Connect to local MQTT broker (assuming Wiren Board default: localhost:1883)
    ctx.mqtt_client = setup_mqtt_client()
    try:
        ctx.mqtt_client.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
        ctx.mqtt_client.loop_start()
        logger.info("Connected to %r MQTT broker", MQTT_HOST)
    except Exception as e:
        logger.error("MQTT connect failed: %r", e)
        return 0  # 0 mean - exit without service restart

    # Set emit callback for yandex_handlers module
    set_emit_callback(_emit_async)

    logger.info("Connecting Socket.IO client...")
    if not await connect_controller(config):
        logger.error("Failed to establish initial connection - exiting")
        return 1  # Need exit with error for restart

    # Init and start AliceDeviceStateSender only after connect SocketIO
    ctx.time_rate_sender = AliceDeviceStateSender(device_registry=ctx.registry)
    asyncio.run_coroutine_threadsafe(ctx.time_rate_sender.start(), ctx.main_loop)
    subscribe_registry_topics()
    logger.info("Client initialization complete, ready to handle requests")

    # Wait for shutdown signal
    await ctx.stop_event.wait()
    logger.info("Shutdown signal received")

    await graceful_shutdown()
    logger.info("Shutdown complete")
    return 0  # 0 mean - exit without service restart


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
