#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Socket.IO Connection Manager for Wiren Board Alice Integration

Manages Socket.IO client lifecycle: creation, connection, monitoring, and shutdown
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

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
        
        # - - - State tracking - - -
        # ts after sock.connect()
        self.connected_at: Optional[float] = None
        # SocketIO async client instance for handling real-time communication
        self._sio: Optional[socketio.AsyncClient] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        
        logger.debug("SocketIOConnectionManager initialized for %r", server_url)
    
    def create_client(self) -> socketio.AsyncClient:
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

        logger.debug("Created Socket.IO client with reconnection=%r", self._reconnection)
        return client
    
    async def connect(self, client: socketio.AsyncClient) -> bool:
        """
        Establish Socket.IO connection
        
        Args:
            client: socketio.AsyncClient instance to connect
            
        Returns:
            True if connection successful, False otherwise
        """
        self._event_loop = asyncio.get_running_loop()
        self._sio = client

        # Explicitly set the loop to avoid "attached to a different loop" errors
        self._sio._loop = self._event_loop
        
        logger.info("Connecting Socket.IO client to %r...", self.server_url)
        
        try:
            # Connect to local Nginx proxy which forwards to actual server
            # "controller_sn" is passed via SSL certificate when Nginx proxies
            await asyncio.wait_for(
                self._sio.connect(
                    self.server_url,
                    socketio_path=self.socketio_path,
                    headers={"WB-Client-Pkg-Ver": self.client_pkg_ver},
                ),
                timeout=10.0,
            )
            
            # Record timestamp when connection established
            self.connected_at = time.monotonic()
            logger.info("Socket.IO connected successfully")
            
            # Start monitoring task
            self._monitor_task = asyncio.create_task(self._monitor_connection())
            logger.debug("Started connection monitor task")
            
            return True
            
        except socketio.exceptions.ConnectionError as e:
            logger.exception("Socket.IO Connection Failed: %r", str(e))
            return False
        except asyncio.TimeoutError:
            logger.exception("Socket.IO Connection Timeout (>10s)")
            return False
        except Exception as e:
            logger.exception(
                "Unexpected exception during connection: %r: %r",
                type(e).__name__,
                str(e)
            )
            return False

    async def _monitor_connection(self) -> None:
        """
        Monitor Socket.IO connection lifetime
        
        This coroutine waits for the connection to be permanently lost
        (i.e., when the built-in reconnection mechanism gives up)
        """
        if self._sio is None:
            logger.error("_monitor_connection() called but _sio is None")
            return
        
        try:
            await self._sio.wait()
        except Exception as e:
            logger.exception("Socket.IO wait() failed with exception: %r", e)
        
        logger.error(
            "Socket.IO client permanently disconnected from server AND "
            "internal SocketIO mechanism does not try to reconnect anymore"
        )
        
        # FIXME: maybe stop event here
        #        on_disconnect() callback may not call - but this place may better
        # if not ctx.stop_event.is_set():
        #     logger.error("SocketIO permanently disconnected, stopping client")
        #     ctx.stop_event.set()

    def is_connected(self) -> bool:
        """
        Check if Socket.IO connection is active and ready
        
        Returns:
            True if connected and ready, False otherwise
        """
        if self._sio is None:
            return False
        
        if not self._sio.connected:
            return False
        
        eio_state = getattr(self._sio.eio, "state", None)
        if eio_state != "connected":
            return False
        
        # Check namespace ready - mean server validate connection successfully
        ns_list = getattr(self._sio, "namespaces", {})
        if "/" not in ns_list:
            logger.warning("Namespace '/' not ready ")
            return False

        # BUG: Additional check for version SocketIO 5.0.3
        #      (may delete this check when upgrade version)
        if hasattr(self._sio.eio, "write_loop_task") and self._sio.eio.write_loop_task is None:
            logger.warning("Write loop task is None, connection may be unstable")
            return False

        return True

    def get_connection_info(self) -> Dict[str, Any]:
        """
        Get current connection state information
        
        Returns:
            Dict with connection details for debugging/monitoring
        """
        if self._sio is None:
            return {"status": "not_initialized"}
        
        return {
            "status": "connected" if self.is_connected() else "disconnected",
            "connected": self._sio.connected,
            "eio_state": getattr(self._sio.eio, "state", "unknown"),
            "namespaces": list(getattr(self._sio, "namespaces", {}).keys()),
            "uptime_seconds": (
                time.monotonic() - self.connected_at
                if self.connected_at
                else None
            ),
            "sid": getattr(self._sio, "sid", None),
        }

    async def disconnect(self) -> None:
        """
        Gracefully disconnect from Socket.IO server
        
        Sends offline status notification and closes connection
        """
        if self._sio is None or not self._sio.connected:
            logger.debug("Already disconnected or not initialized")
            return

        try:
            logger.info("Notifying server about offline status...")
            # This is informative message, not have spesial beckend processing
            await asyncio.wait_for(
                self._sio.emit(
                    "message",
                    {"controller_sn": self.controller_sn, "status": "offline"}
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
        try:
            logger.info("Disconnecting from Socket.IO server...")
            await asyncio.wait_for(self._sio.disconnect(), timeout=5.0)
            logger.info("Socket.IO disconnected successfully")
        except asyncio.TimeoutError:
            logger.warning("Timeout while disconnecting from Socket.IO server")
        except Exception as e:
            logger.warning("Error during Socket.IO disconnect: %r", e)
        
        # Cancel monitor task
        if self._monitor_task and not self._monitor_task.done():
            logger.debug("Cancelling Socket.IO monitor task...")
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
    
    @property
    def client(self) -> Optional[socketio.AsyncClient]:
        """
        Get underlying Socket.IO client
        
        Returns:
            socketio.AsyncClient instance or None
        """
        return self._sio
    
    @property
    def monitor_task(self) -> Optional[asyncio.Task]:
        """
        Get connection monitor task
        
        Returns:
            Monitor task or None
        """
        return self._monitor_task
