#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Socket.IO Event Handlers for Yandex Alice Integration

Contains all Socket.IO event handlers for processing Alice smart home requests
and connection lifecycle events.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import socketio

logger = logging.getLogger(__name__)


class SocketIOHandlers:
    """
    Centralized Socket.IO event handlers for Alice integration.
    
    This class encapsulates all Socket.IO event handling logic:
    - Connection lifecycle (connect, disconnect, errors)
    - Alice device discovery, queries, and actions
    - Generic server responses
    
    Separating handlers from the main client improves:
    - Testability (can mock dependencies)
    - Maintainability (all handlers in one place)
    - Readability (clear responsibility separation)
    """
    
    def __init__(
        self,
        *,
        registry: Any,  # DeviceRegistry
        controller_sn: str,
        sio_client: Optional[Any] = None,  # socketio.AsyncClient
        on_permanent_disconnect: Optional[callable] = None,
        connection_manager: Optional[Any] = None, # SocketIOConnectionManager
    ):
        """
        Initialize handlers with required dependencies
        
        Args:
            registry: DeviceRegistry instance for device management
            controller_sn: Controller serial number for identification
            sio_client: Socket.IO client instance (for emitting messages)
            on_permanent_disconnect: Callback to trigger on unrecoverable errors
            connection_manager: Manager for triggering custom reconnection
        """
        self.registry = registry
        self.controller_sn = controller_sn
        self._sio_client = sio_client
        self._on_permanent_disconnect = on_permanent_disconnect
        self._connection_manager = connection_manager

    @property
    def sio(self):
        """Get Socket.IO client - from direct reference or connection manager"""
        if self._sio_client is not None:
            return self._sio_client
        if self._connection_manager is not None:
            return self._connection_manager.client
        raise RuntimeError("No Socket.IO client available")

    # -------------------------------------------------------------------------
    # Connection Lifecycle Handlers
    # -------------------------------------------------------------------------
    
    async def on_connect(self) -> None:
        """
        Handler: Successful connection to Socket.IO server
        
        Called when the Socket.IO connection is established and namespace is ready
        """
        logger.info("Success connected to Socket.IO server! namespace='/' ready")
        
        # Log session identifiers for debugging
        # NOTE: ctx.sio.sid is the Socket.IO session id for the default namespace "/"
        sio_sid = getattr(self.sio, "sid", None)
        logger.info("Socket.IO session id (sid): %r", sio_sid)

        # Optional (debug): engine.io session id, may help in low-level troubleshooting
        eio_sid = getattr(getattr(self.sio, "eio", None), "sid", None)
        logger.debug("Engine.IO session id (eio.sid): %r", eio_sid)
        
        # Notify server that controller is online
        # NOTE: this is not processed on server side and needed only for debug
        try:
            await self.sio.emit(
                "message",
                {"controller_sn": self.controller_sn, "status": "online"}
            )
        except Exception as e:
            logger.warning("Failed to send 'online' status after connect: %r", e)

    async def on_disconnect(self) -> None:
        """
        Handler: Connection to Socket.IO server lost
        
        NOTE: Argument 'reason' is not available in Socket.IO version 5.0.3
        (Released in Dec 14,2020). It was added in version 5.12+

        This handler is called when:
        - Connection is lost during operation
        - Server explicitly disconnects the client
        """
        logger.warning("Socket.IO connection lost")

        # NOTE: Socket.IO will attempt automatic reconnection
        #       If reconnection fails permanently, monitor_task via sio.wait()
        #       will detect it and trigger custom reconnection logic


    async def on_connect_error(self, data: Any) -> None:
        """
        Handler: Connection error from server
          - Initial connection to server failed
          - Or active connection was refused by server side by disconnect(sid)
        
        Args:
            data: Error message from server (type varies by Socket.IO version)
                NOTE: In actual Socket.IO versions - argument is "Dict[str, Any]",
                but in old versions what we use - this is "str" -> now set "Any" type
        """
        logger.error("Yandex Alice integration connection refused by server")
        # This typically indicates:
        # - Controller not registered on server
        # - Authentication failure
        # - Server rejecting connection
        reason_raw = str(data).strip()
        logger.error("Message from server: %r", reason_raw)

        # Record disconnect event for monitoring purposes
        if self._connection_manager:
            if hasattr(self._connection_manager, '_record_disconnect'):
                self._connection_manager._record_disconnect(f"connect_error: {reason_raw}")

        # NOTE: Built-in Socket.IO reconnect NOT will handle this automatically
        #       It reconnection fails permanently, monitor_task via sio.wait()
        #       will detect it and trigger custom reconnection logic

    async def on_response(self, data: Any) -> None:
        """
        Handler: Generic server response.
        
        Args:
            data: Response data from server
        """
        logger.debug("Socket.IO server response: %r", data)
    
    async def on_error(self, data: Any) -> None:
        """
        Handler: Server error message.
        
        Args:
            data: Error data from server
        """
        logger.debug("Socket.IO server error: %r", data)

    async def on_any_unprocessed(self, event: str, sid: str, data: Any) -> None:
        """
        Handler: Catch-all for unprocessed Socket.IO events
        
        Args:
            event: Event name
            sid: Session ID
            data: Event data
        """
        logger.debug("Socket.IO unhandled event %r", event)
    
    # -------------------------------------------------------------------------
    # Alice Smart Home Handlers
    # -------------------------------------------------------------------------

    async def on_alice_devices_list(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handler: Device discovery request from Yandex server
        
        Returns a list of all devices configured in the controller
        Called when user links their smart home account or requests device refresh
        
        Args:
            data: Request with request_id
            
        Returns:
            Response dict with devices list formatted per Yandex Smart Home spec
        """
        logger.debug("Received 'alice_devices_list' event:")
        logger.debug(json.dumps(data, ensure_ascii=False, indent=2))
        req_id: str = data.get("request_id")

        if self.registry is None:
            logger.error("Registry not available for device list")
            return {"request_id": req_id, "payload": {"devices": []}}

        devices_list: List[Dict[str, Any]] = self.registry.build_yandex_devices_list()
        if not devices_list:
            logger.warning("No devices found in configuration")

        devices_list_response: Dict[str, Any] = {
            "request_id": req_id,
            "payload": {
                # "user_id" will be added on the server proxy side
                "devices": devices_list,
            },
        }
        
        logger.info("Sending device list response to Yandex:")
        logger.info(json.dumps(devices_list_response, ensure_ascii=False, indent=2))
        return devices_list_response

    async def on_alice_devices_query(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handler: Device current state query request from Yandex
        
        Returns current state of requested devices
        Called when user asks Alice about device status or when Alice app refreshes
        
        Args:
            data: Request with device IDs to query
            
        Returns:
            Response dict with device states
        """
        logger.debug("alice_devices_query event:")
        logger.debug(json.dumps(data, ensure_ascii=False, indent=2))
        
        request_id = data.get("request_id", "unknown")
        devices_response: List[Dict[str, Any]] = []

        for dev in data.get("devices", []):
            device_id = dev.get("id")
            logger.debug("Try getting state for device: %r", device_id)
            devices_response.append(
                await self.registry.get_device_current_state(device_id)
            )
        
        query_response = {
            "request_id": request_id,
            "payload": {
                "devices": devices_response,
            },
        }

        logger.debug("Sending devices query response to Yandex:")
        logger.debug(json.dumps(query_response, ensure_ascii=False, indent=2))
        return query_response

    async def on_alice_devices_action(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handler: Device action command from Yandex (e.g., turn on/off)
        
        Applies requested actions to devices (e.g., turn on/off, set brightness)
        Called when user gives voice commands or uses Alice app controls
        
        Args:
            data: Request with devices and actions to perform
            
        Returns:
            Response dict with action results
        """
        logger.debug("Received 'alice_devices_action' event")
        logger.debug("Full payload:")
        logger.debug(json.dumps(data, ensure_ascii=False, indent=2))

        request_id: str = data.get("request_id", "unknown")
        devices_in: List[Dict[str, Any]] = data.get("payload", {}).get("devices", [])
        devices_info: List[Dict[str, Any]] = []

        for device in devices_in:
            result = await self._handle_single_device_action(device)
            if result:
                devices_info.append(result)
        
        action_response: Dict[str, Any] = {
            "request_id": request_id,
            "payload": {"devices": devices_info},
        }

        logger.debug("Sending action response:")
        logger.debug(json.dumps(action_response, ensure_ascii=False, indent=2))
        return action_response

    async def _handle_single_device_action(
        self, device: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Internal helper for on_alice_devices_action
        Process all capabilities for a single device

        Args:
            device: Device action data with capabilities
            
        Returns:
            Result block formatted per Yandex Smart Home spec
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
                await self.registry.forward_yandex_to_mqtt(
                    device_id, cap_type, instance, value
                )
                logger.debug(
                    "Action applied to %r: %r = %r", device_id, instance, value
                )
                status = "DONE"
            except Exception as e:
                logger.exception("Failed to apply action for device %r", device_id)
                status = "ERROR"
            
            cap_results.append({
                "type": cap_type,
                "state": {
                    "instance": instance,
                    "action_result": {"status": status},
                },
            })
        
        return {
            "id": device_id,
            "capabilities": cap_results,
        }

    # -------------------------------------------------------------------------
    # Handler Registration
    # -------------------------------------------------------------------------

    def bind_sio_handlers_to_client(self, sio_client: socketio.AsyncClient) -> None:
        """
        Bind all event handlers to a Socket.IO client


        Unlike decorators, we use .on() method for better safety - this approach
        helps control all names and objects at any time and in any context
        
        Args:
            sio_client: socketio.AsyncClient instance
        """
        self._sio_client = sio_client

        # Connection lifecycle
        sio_client.on("connect", self.on_connect)
        sio_client.on("disconnect", self.on_disconnect)
        sio_client.on("connect_error", self.on_connect_error)
        sio_client.on("response", self.on_response)
        sio_client.on("error", self.on_error)
        
        # Alice Smart Home events
        sio_client.on("alice_devices_list", self.on_alice_devices_list)
        sio_client.on("alice_devices_query", self.on_alice_devices_query)
        sio_client.on("alice_devices_action", self.on_alice_devices_action)
        
        # Catch-all for unprocessed events
        sio_client.on("*", self.on_any_unprocessed)
        
        logger.debug("All Socket.IO handlers bound to client")
