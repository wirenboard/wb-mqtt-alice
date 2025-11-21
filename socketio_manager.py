#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Socket.IO Connection Manager for Wiren Board Alice Integration

Manages Socket.IO client lifecycle: creation, connection, monitoring, and shutdown
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional
import contextlib

import socketio

logger = logging.getLogger(__name__)

logger.debug("socketio module path: %r", socketio.__file__)
from importlib.metadata import PackageNotFoundError, version

try:
    logger.debug("python-socketio version: %r", version("python-socketio"))
except PackageNotFoundError:
    logger.warning("python-socketio is not installed.")


class SocketIOConnectionManager:
    """
    Manages Socket.IO connection lifecycle

    Responsibilities:
    - Create and configure Socket.IO client
    - Establish connection to server
    - Monitor connection status
    - Track connection metrics (uptime, etc.)
    - Graceful shutdown

    This class isolates Socket.IO connection logic from application logic,
    making it easier to add custom reconnection strategies later
    """

    # System events that manager wraps for monitoring/metrics
    SYSTEM_EVENTS = {"connect", "disconnect", "connect_error"}

    def __init__(
        self,
        *,
        server_url: str,
        socketio_path: str = "/socket.io",
        controller_sn: str,
        client_pkg_ver: str,
        reconnection: bool = True,
        reconnection_delay: int = 2,
        reconnection_delay_max: int = 60,
        debug_logging: bool = False,
        custom_reconnect_enabled: bool = True,
        custom_reconnect_interval: int = 1200,  # 20 minutes
    ):
        """
        Initialize Socket.IO Connection Manager

        Args:
            server_url: URL to connect (e.g., "http://localhost:8042")
            socketio_path: Socket.IO endpoint path
            controller_sn: Controller serial number for identification
            client_pkg_ver: Client package version for headers
            reconnection: Enable automatic reconnection
            reconnection_delay: Initial reconnection delay (seconds)
            reconnection_delay_max: Maximum reconnection delay (seconds)
            debug_logging: Enable Socket.IO debug logging

            custom_reconnect_enabled: Enable custom 20-min reconnection logic
            custom_reconnect_interval: Interval between custom reconnect attempts (seconds)
        """
        self.server_url = server_url
        self.socketio_path = socketio_path
        self.controller_sn = controller_sn
        self.client_pkg_ver = client_pkg_ver

        # - - - Connection configuration - - -
        self._reconnection = reconnection
        self._reconnection_delay = reconnection_delay
        self._reconnection_delay_max = reconnection_delay_max
        self._debug_logging = debug_logging

        self._custom_reconnect_enabled = custom_reconnect_enabled
        self._custom_reconnect_interval = custom_reconnect_interval

        # - - - User handlers registry - - -
        self._user_handlers: Dict[str, Callable] = {}
        """User-registered event handlers (business logic)"""

        # - - - State tracking - - -
        # ts after sock.connect()
        self.connected_at: Optional[float] = None
        # SocketIO async client instance for handling real-time communication
        self._sio_client: Optional[socketio.AsyncClient] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        self._reconnection_task: Optional[asyncio.Task] = None
        self._is_reconnecting: bool = False
        self._should_stop: bool = False

        self.disconnect_history: List[Dict[str, Any]] = []
        logger.debug("SocketIOConnectionManager initialized for %r", server_url)

    # =========================================================================
    # Public API: Event Handler Registration
    # =========================================================================

    def on(self, event: str, handler: Callable) -> None:
        """
        Register event handler (business logic)

        For system events (connect, disconnect, connect_error), manager wraps
        the handler to perform infrastructure tasks first

        Example:
            manager.on('connect', async_connect_handler)
            manager.on('alice_devices_list', async_alice_handler)
        """
        if event in self.SYSTEM_EVENTS:
            # System event - store for calling after manager's work
            self._user_handlers[event] = handler
            logger.debug(
                "Registered user handler for system event %r (will be wrapped)", event
            )
        else:
            # Business event
            # - if not exist, register later directly with Socket.IO
            # - if client already exists, bind immediately
            if event not in self._user_handlers or isinstance(
                self._user_handlers[event], Callable
            ):
                self._user_handlers[event] = []
            self._user_handlers[event].append(handler)
            logger.debug(
                "Registered business handler for event %r (will be bound on client creation)",
                event,
            )

            if self._sio_client is not None:
                self._sio_client.on(event, handler)
                logger.debug("Registered business handler %r on existing client", event)

    # =========================================================================
    # System Event Wrappers (Infrastructure Layer)
    # =========================================================================

    async def _on_connect_wrapper(self) -> None:
        """
        System wrapper for 'connect' event.

        Manager responsibilities:
        1. Track connection timestamp
        2. Log connection details
        3. Call user handler (if registered)
        """
        # Manager's infrastructure work
        self.connected_at = time.monotonic()

        # Log session identifiers for debugging
        # NOTE: ctx.sio.sid is the Socket.IO session id for the default namespace "/"
        sio_sid = getattr(self._sio_client, "sid", None)
        logger.info(
            "Manager: Success connected to Socket.IO server, namespace='/' ready (sid=%r)",
            sio_sid,
        )

        # Optional (debug): engine.io session id, may help in low-level troubleshooting
        eio_sid = getattr(getattr(self._sio_client, "eio", None), "sid", None)
        logger.debug("Engine.IO session id (eio.sid): %r", eio_sid)

        # Notify server that controller is online
        # NOTE: this is not processed on server side and needed only for debug
        try:
            await self._sio_client.emit(
                "message", {"controller_sn": self.controller_sn, "status": "online"}
            )
        except Exception as e:
            logger.warning("Failed to send 'online' status after connect: %r", e)

        # Call user's business logic handler
        if "connect" in self._user_handlers:
            try:
                await self._user_handlers["connect"]()
            except Exception as e:
                logger.exception("Error in user connect handler: %r", e)

    async def _on_disconnect_wrapper(self) -> None:
        """
        System wrapper for 'disconnect' event.

        Manager responsibilities:
        1. Record disconnect event
        2. Log disconnect
        3. Call user handler (if registered)
        """
        # Manager's infrastructure work
        self._record_disconnect("disconnect_event")
        logger.warning("Manager: Socket.IO disconnected")

        # Call user's business logic handler
        if "disconnect" in self._user_handlers:
            try:
                await self._user_handlers["disconnect"]()
            except Exception as e:
                logger.exception("Error in user disconnect handler: %r", e)

    async def _on_connect_error_wrapper(self, data: Any) -> None:
        """
        System wrapper for 'connect_error' event.

        Manager responsibilities:
        1. Record error details
        2. Log error
        3. Call user handler (if registered)

        Note: Built-in Socket.IO reconnect will handle retries automatically.
        If all attempts fail, monitor_task will trigger custom reconnection.
        """
        reason_raw = str(data).strip()
        self._record_disconnect(f"connect_error: {reason_raw}")
        logger.error("Manager: Socket.IO connect_error - %r", reason_raw)

        # Call user's business logic handler
        if "connect_error" in self._user_handlers:
            try:
                await self._user_handlers["connect_error"](data)
            except Exception as e:
                logger.exception("Error in user connect_error handler: %r", e)

    def _create_client(self) -> socketio.AsyncClient:
        """
        Create and configure Socket.IO client instance

        Returns:
            Configured socketio.AsyncClient instance
        """
        client = socketio.AsyncClient(
            logger=self._debug_logging,
            engineio_logger=self._debug_logging,
            reconnection=self._reconnection,  # auto-reconnect
            reconnection_attempts=0,  # 0 = infinite retries
            reconnection_delay=self._reconnection_delay,  # first recconect delay
            reconnection_delay_max=self._reconnection_delay_max,
            randomization_factor=0.5,  # jitter
        )

        logger.debug("Re-registering handlers to new client")
        # System event handlers are registered here as wrappers.
        # User handlers will be called from within these wrappers.
        client.on("connect", self._on_connect_wrapper)
        client.on("disconnect", self._on_disconnect_wrapper)
        client.on("connect_error", self._on_connect_error_wrapper)

        # Later business callbacks register
        for event, handlers in self._user_handlers.items():
            if event not in self.SYSTEM_EVENTS:
                if isinstance(handlers, list):
                    for h in handlers:
                        client.on(event, h)
                else:
                    client.on(event, handlers)

        logger.debug("Created new Socket.IO client")
        return client

    async def _close_client(self) -> None:
        """
        Safely close current Socket.IO client
        """
        client_tmp = self._sio_client
        # Drop reference first to avoid re-use while closing
        self._sio_client = None

        if client_tmp is None:
            return

        # Close websocket connection
        if getattr(client_tmp, "connected", False):
            try:
                logger.info("Disconnecting from Socket.IO server...")
                await asyncio.wait_for(client_tmp.disconnect(), timeout=5.0)
                logger.info("Socket.IO disconnected successfully")
            except asyncio.TimeoutError:
                logger.warning("Timeout while disconnecting from Socket.IO server")
            except Exception as e:
                logger.warning("Error during Socket.IO disconnect: %r", e)


    async def connect(self) -> bool:
        """
        Establish Socket.IO connection
        Creates client internally and connects to server

        Returns:
            True if connection successful, False otherwise
        """
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("connect() called outside async context")
            return False

        # If client is already exists - kill it
        if self._sio_client is not None:
            with contextlib.suppress(Exception):
                await self._close_client()
        self._sio_client = self._create_client()
        logger.debug("Created new Socket.IO client instance")

        logger.info("Connecting Socket.IO client to %r...", self.server_url)

        try:
            # Connect to local Nginx proxy which forwards to actual server
            # "controller_sn" is passed via SSL certificate when Nginx proxies
            await asyncio.wait_for(
                self._sio_client.connect(
                    self.server_url,
                    socketio_path=self.socketio_path,
                    transports=["websocket"],  # Need sticky WebSocket sessions
                    headers={"WB-Client-Pkg-Ver": self.client_pkg_ver},
                ),
                timeout=10.0,
            )
        except socketio.exceptions.ConnectionError as e:
            logger.warning("Socket.IO Connection Failed: %r", str(e))
            with contextlib.suppress(Exception):
                await self._close_client()
            return False
        except asyncio.TimeoutError:
            logger.warning("Socket.IO Connection Timeout (>10s)")
            with contextlib.suppress(Exception):
                await self._close_client()
            return False
        except Exception as e:
            logger.exception(
                "Unexpected exception during connection: %r: %r",
                type(e).__name__,
                str(e),
            )
            with contextlib.suppress(Exception):
                await self._close_client()
            return False

        # Wait for namespace to be ready (important for socketio 5.0.3)
        # FIXME(vg): In this place need add wait for namespace to be actually
        #            connected.
        # On this moment simply sleep, this not critical error, but show
        # in log msg - ...
        await asyncio.sleep(2)
        namespace = "/"
        # Check if client still connected
        if not self._sio_client.connected:
            logger.warning("Client disconnected while waiting for namespace")
            return False
        # Check if namespace registered
        if (
            hasattr(self._sio_client, "namespaces")
            and namespace not in self._sio_client.namespaces
        ):
            logger.error("Default namespace failed to connect")
            return False

        # Record timestamp when connection established
        self.connected_at = time.monotonic()
        logger.info("Socket.IO connected successfully")

        # Start tasks
        try:
            self._monitor_task = asyncio.create_task(
                self._monitor_connection(), name="socketio-monitor"
            )
            logger.debug("Started connection monitor task")
        except Exception:
            logger.exception("Failed to start monitoring tasks")

        return True

    def _record_disconnect(self, reason: str) -> None:
        """Record disconnect event in history"""
        event = {
            "timestamp": time.time(),
            "reason": reason,
            "uptime_seconds": (
                time.monotonic() - self.connected_at if self.connected_at else None
            ),
        }
        self.disconnect_history.append(event)

        # Keep only last 50 events to avoid memory bloat
        if len(self.disconnect_history) > 50:
            self.disconnect_history.pop(0)

        logger.debug("Recorded disconnect: %r", event)

    async def _trigger_custom_reconnection(self, reason: str) -> None:
        """Trigger custom reconnection logic"""
        if not self._custom_reconnect_enabled:
            return

        if self._is_reconnecting:
            logger.debug("Reconnection already in progress, skipping trigger")
            return

        if self._reconnection_task and not self._reconnection_task.done():
            logger.debug("Reconnection task already running, skipping trigger")
            return

        self._is_reconnecting = True

        # Cancel existing reconnection task if any
        if self._reconnection_task and not self._reconnection_task.done():
            self._reconnection_task.cancel()
            try:
                await self._reconnection_task
            except asyncio.CancelledError:
                pass

        # Start new reconnection loop
        self._reconnection_task = asyncio.create_task(
            self._custom_reconnection_loop(reason)
        )
        logger.info("Started custom reconnection task (reason: %r)", reason)

    async def start_custom_reconnect(
        self, reason: str = "manual_initial_failure"
    ) -> None:
        """
        Public wrapper for starting custom reconnection from application code
        """
        await self._trigger_custom_reconnection(reason)

    async def _monitor_connection(self) -> None:
        """
        Monitor Socket.IO connection lifetime

        This coroutine waits for the connection to be permanently lost
        (i.e., when the built-in reconnection mechanism gives up)
        """
        if self._sio_client is None:
            logger.error("_monitor_connection() called but _sio_client is None")
            return

        try:
            await self._sio_client.wait()
        except Exception as e:
            logger.exception("Socket.IO wait() failed with exception: %r", e)

        logger.error(
            "Socket.IO client permanently disconnected from server AND "
            "internal SocketIO mechanism does not try to reconnect anymore"
        )

        # Record disconnect and trigger custom reconnection
        self._record_disconnect("permanent_disconnect")
        await self._trigger_custom_reconnection("permanent_disconnect")

        # FIXME: maybe stop event here
        #        on_disconnect() callback may not call - but this place may better
        # if not ctx.stop_event.is_set():
        #     logger.error("SocketIO permanently disconnected, stopping client")
        #     ctx.stop_event.set()

    async def _custom_reconnection_loop(self, initial_reason: str) -> None:
        """
        Custom reconnection loop with fixed 20-minute intervals

        Args:
            initial_reason: Reason for the initial disconnect
        """
        attempt = 0

        logger.warning(
            "Entered custom reconnection mode (reason: %r). "
            "Will attempt reconnection every %d seconds",
            initial_reason,
            self._custom_reconnect_interval,
        )

        while not self._should_stop:
            attempt += 1

            # First attempt is immediate, subsequent attempts wait interval
            # (default 20 minutes)
            if attempt > 1:
                logger.info(
                    "Waiting %d seconds before reconnection attempt #%d...",
                    self._custom_reconnect_interval,
                    attempt,
                )
                try:
                    await asyncio.sleep(self._custom_reconnect_interval)
                except asyncio.CancelledError:
                    logger.debug("Reconnection wait cancelled")
                    break

            if self._should_stop:
                break

            logger.info("Attempting reconnection #%d...", attempt)

            try:
                # Stop tasks before reconnecting
                if self._monitor_task and not self._monitor_task.done():
                    self._monitor_task.cancel()
                    try:
                        await self._monitor_task
                    except asyncio.CancelledError:
                        pass
                
                # Close client AFTER stop monitor task, for prevent reconect
                await self._close_client(notify_offline=False)

                # Try to reconnect with new client
                self._sio_client = None  # Force creation of new client
                logger.debug("Recreating Socket.IO client for reconnection")
                success = await self.connect()

                if success:
                    logger.info(
                        "Custom reconnection successful after %d attempts", attempt
                    )
                    self._is_reconnecting = False
                    return  # Exit loop on success
                else:
                    logger.warning("Reconnection attempt #%d failed", attempt)

            except Exception as e:
                logger.exception(
                    "Error during reconnection attempt #%d: %r", attempt, e
                )
        logger.error("Custom reconnection loop exited")
        self._is_reconnecting = False

    def is_connected(self) -> bool:
        """
        Check if Socket.IO connection is active and ready

        Returns:
            True if connected and ready, False otherwise
        """
        if self._sio_client is None:
            return False

        if not self._sio_client.connected:
            return False

        eio_state = getattr(self._sio_client.eio, "state", None)
        if eio_state != "connected":
            return False

        # Check namespace ready - mean server validate connection successfully
        ns_list = getattr(self._sio_client, "namespaces", {})
        if "/" not in ns_list:
            logger.warning("Namespace '/' not ready ")
            return False

        # BUG: Additional check for version SocketIO 5.0.3
        #      (may delete this check when upgrade version)
        if (
            hasattr(self._sio_client.eio, "write_loop_task")
            and self._sio_client.eio.write_loop_task is None
        ):
            logger.warning("Write loop task is None, connection may be unstable")
            return False

        return True

    def get_connection_info(self) -> Dict[str, Any]:
        """
        Get current connection state information

        Returns:
            Dict with connection details for debugging/monitoring
        """
        if self._sio_client is None:
            return {"status": "not_initialized"}

        return {
            "status": "connected" if self.is_connected() else "disconnected",
            "connected": self._sio_client.connected,
            "eio_state": getattr(self._sio_client.eio, "state", "unknown"),
            "namespaces": list(getattr(self._sio_client, "namespaces", {}).keys()),
            "uptime_seconds": (
                time.monotonic() - self.connected_at if self.connected_at else None
            ),
            "sid": getattr(self._sio_client, "sid", None),
        }

    async def disconnect(self) -> None:
        """
        Gracefully disconnect from Socket.IO server

        Sends offline status notification and closes connection
        """
        self._should_stop = True

        # Cancel reconnection task
        if self._reconnection_task and not self._reconnection_task.done():
            logger.debug("Cancelling reconnection task...")
            self._reconnection_task.cancel()
            try:
                await self._reconnection_task
            except asyncio.CancelledError:
                pass

        # Cancel monitor task
        if self._monitor_task and not self._monitor_task.done():
            logger.debug("Cancelling Socket.IO monitor task...")
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._sio_client is None or not self._sio_client.connected:
            logger.debug("Already disconnected or not initialized")
            return

        try:
            logger.info("Notifying server about offline status...")
            # This is informative message, not have spesial beckend processing
            await asyncio.wait_for(
                self._sio_client.emit(
                    "message",
                    {"controller_sn": self.controller_sn, "status": "offline"},
                ),
                timeout=3.0,
            )
            # Give server time to process the offline message
            await asyncio.sleep(0.5)
        except asyncio.TimeoutError:
            logger.warning("Timeout while notifying server about offline status")
        except Exception as e:
            logger.warning("Failed to notify server about offline status: %r", e)

        # When service stop by user and then fast up
        # We need stop correctly with wait disconnect from server,
        # without it server not connect second controller twice
        await self._close_client()

    def get_disconnect_summary(self) -> str:
        """Get formatted summary of recent disconnects"""
        if not self.disconnect_history:
            return "No disconnect events recorded"

        lines = ["Recent disconnect events:"]
        for i, event in enumerate(self.disconnect_history[-10:], 1):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["timestamp"]))
            uptime_str = (
                f"{event['uptime_seconds']:.1f}s"
                if event["uptime_seconds"] is not None
                else "N/A"
            )
            lines.append(f"  {i}. {ts} - {event['reason']} (uptime: {uptime_str})")

        return "\n".join(lines)

    @property
    def client(self) -> Optional[socketio.AsyncClient]:
        """
        Get underlying Socket.IO client

        Returns:
            socketio.AsyncClient instance or None
        """
        return self._sio_client

    @property
    def monitor_task(self) -> Optional[asyncio.Task]:
        """
        Get connection monitor task

        Returns:
            Monitor task or None
        """
        return self._monitor_task

    # =========================================================================
    # Convenience Methods for User Handlers
    # =========================================================================

    async def emit(self, event: str, data: Any = None, **kwargs) -> Any:
        """
        Emit Socket.IO event (convenience method for user handlers).

        Args:
            event: Event name
            data: Data to send
            **kwargs: Additional arguments passed to sio.emit()

        Returns:
            Result from sio.emit()

        Raises:
            RuntimeError: If Socket.IO client not connected
        """
        if self._sio_client is None or not self._sio_client.connected:
            raise RuntimeError(f"Cannot emit {event!r}: Socket.IO not connected")

        return await self._sio_client.emit(event, data, **kwargs)
