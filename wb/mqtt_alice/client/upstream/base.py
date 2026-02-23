# upstream/base.py

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Any, Dict, Optional
from .types import UpstreamState

# Type aliases for handlers

# Handles "discovery" requests (get list of devices)
DiscoveryHandler = Callable[[dict], Awaitable[dict]]

# Handles "action" requests (turn on light)
CommandHandler = Callable[[dict], Awaitable[dict]]

# Handles "query" requests (get temperature)
QueryHandler = Callable[[dict], Awaitable[dict]]

# Handles state transitions (READY <-> UNAVAILABLE)
StateChangeHandler = Callable[[UpstreamState], Awaitable[None]]


class BaseUpstreamAdapter(ABC):
    """
    Abstract interface for Yandex Smart Home communication
    Only knows callbacks - not know about router, store or registry
    """

    def __init__(self, config: dict):
        self.config = config
        self._state: UpstreamState = UpstreamState.INITIALIZING

        # Injected handlers
        self._discovery_handler: Optional[DiscoveryHandler] = None
        self._command_handler: Optional[CommandHandler] = None
        self._query_handler: Optional[QueryHandler] = None
        self._state_handler: Optional[StateChangeHandler] = None

    # --- Lifecycle Methods ---

    @abstractmethod
    async def start(self) -> None:
        """
        Start transport mechanism
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop transport and release resources
        """
        pass

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        """
        Block until the adapter reaches READY state
        """
        start_time = asyncio.get_running_loop().time()

        while self._state != UpstreamState.READY:
            if asyncio.get_running_loop().time() - start_time > timeout:
                return False
            if self._state == UpstreamState.STOPPED:
                return False
            await asyncio.sleep(0.1)

        return True

    # --- Setup Methods ---
    def register_discovery_handler(self, handler: DiscoveryHandler) -> None:
        """
        Register callback for incoming 'discovery' requests (device list)
        """
        self._discovery_handler = handler

    def register_command_handler(self, handler: CommandHandler) -> None:
        """
        Register callback for incoming 'action' requests
        """
        self._command_handler = handler

    def register_query_handler(self, handler: QueryHandler) -> None:
        """
        Register callback for incoming 'query' requests
        """
        self._query_handler = handler

    def register_state_handler(self, handler: StateChangeHandler) -> None:
        """
        Register callback for state changes (e.g., READY -> UNAVAILABLE)
        Core uses this to pause/resume downstream logic
        """
        self._state_handler = handler

    # --- Outgoing (Core -> Yandex) ---

    @abstractmethod
    async def send_notification(self, notification_data: dict) -> None:
        """
        Send state update to Yandex
        """
        pass

    # --- Internal Helpers (For subclasses) ---

    async def _set_state(self, new_state: UpstreamState) -> None:
        """
        Update internal state and notify listener if changed
        """
        if self._state != new_state:
            old_state = self._state
            self._state = new_state

            logging.info(
                f"Upstream state changed: {old_state.name} -> {new_state.name}"
            )

            if self._state_handler:
                # Fire and forget (or await if critical)
                try:
                    await self._state_handler(new_state)
                except Exception as e:
                    logging.error(f"Error in upstream state handler: {e}")

    async def _handle_incoming_discovery(self, payload: dict) -> dict:
        """
        Subclasses safely call this when they receive a Discovery request
        """
        if not self._discovery_handler:
            logging.error("Received Discovery but no handler registered!")
            return {"error": "Internal Error: No handler registered"}
        return await self._discovery_handler(payload)

    async def _handle_incoming_command(self, payload: dict) -> dict:
        """
        Subclasses safely call this when they receive an Action request
        """
        if not self._command_handler:
            logging.error("Received Command but no handler registered!")
            return {"error": "Internal Error: No handler registered"}
        return await self._command_handler(payload)

    async def _handle_incoming_query(self, payload: dict) -> dict:
        """
        Subclasses safely call this when they receive a Query request
        """
        if not self._query_handler:
            logging.error("Received Query but no handler registered!")
            return {"error": "Internal Error: No handler registered"}
        return await self._query_handler(payload)
