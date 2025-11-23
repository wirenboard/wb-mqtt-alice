#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Socket.IO Event Handlers for Yandex Alice Integration

Contains Socket.IO event handlers for processing Alice smart home requests
and connection lifecycle events.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SioAliceHandlers:
    """
    Centralized Socket.IO event handlers for Alice integration

    This class encapsulates all Socket.IO event handling logic:
    - Connection lifecycle (connect, disconnect, errors)
    - Alice device discovery, queries, and actions
    """

    def __init__(
        self,
        *,
        registry: Any,  # DeviceRegistry
        controller_sn: str,
        mqtt_client: Optional[Any] = None,
        time_rate_sender: Optional[Any] = None,
    ):
        """
        Initialize handlers with required dependencies

        Args:
            registry: DeviceRegistry instance for device management
            controller_sn: Controller serial number for identification
            mqtt_client: Optional MQTT client instance for subscriptions
            time_rate_sender: Optional AliceDeviceStateSender instance
        """
        self.registry = registry
        self.controller_sn = controller_sn
        self._manager = None  # Will be set by register_with_manager()
        self._mqtt_client = mqtt_client
        self._time_rate_sender = time_rate_sender

    def _subscribe_registry_topics(self) -> None:
        """
        Subscribe MQTT client to all topics from registry
        Called only after full initialization (Socket.IO connected, etc.)

        NOTE: This need only for notify yandex API,
            but not needed for commands from Yandex
        """
        if self._mqtt_client is None:
            logger.error("Cannot subscribe: MQTT client is None")
            return

        # Check if registry is ready
        if self.registry is None or not hasattr(self.registry, "topic2info"):
            logger.error("Cannot subscribe: MQTT registry not ready")
            return

        for topic in self.registry.topic2info.keys():
            try:
                self._mqtt_client.subscribe(topic, qos=0)
                logger.debug("MQTT subscribed to %r", topic)
            except Exception as e:
                logger.warning("Failed to subscribe to %r: %r", topic, e)

    def _unsubscribe_registry_topics(self) -> None:
        """
        Unsubscribe MQTT client from all topics from registry

        Called when Socket.IO connection is lost or permanently refused,
        to avoid processing device updates while we cannot notify Yandex
        """
        if self._mqtt_client is None:
            logger.error("Cannot unsubscribe: MQTT client is None")
            return

        if self.registry is None or not hasattr(self.registry, "topic2info"):
            logger.error("Cannot unsubscribe: MQTT registry not ready")
            return

        for topic in self.registry.topic2info.keys():
            try:
                self._mqtt_client.unsubscribe(topic)
                logger.debug("MQTT unsubscribed from %r", topic)
            except Exception as e:
                logger.warning("Failed to unsubscribe from %r: %r", topic, e)

    # -------------------------------------------------------------------------
    # Connection Lifecycle Handlers
    # -------------------------------------------------------------------------

    async def on_connect(self) -> None:
        """
        Handler: Successful connection to Socket.IO server

        Called when the Socket.IO connection is established and namespace is ready
        """
        logger.debug("Business: Connected to Socket.IO server")

        # Start Alice state sender (if provided) and subscribe to MQTT topics
        if self._time_rate_sender is not None and not getattr(
            self._time_rate_sender, "running", False
        ):
            try:
                await self._time_rate_sender.start()
                logger.info("Alice state sender started after connect")
            except Exception as e:
                logger.exception(
                    "Failed to start Alice state sender after connect: %r", e
                )

        # Subscribe to all topics from registry (if MQTT client is available)
        self._subscribe_registry_topics()

    async def on_disconnect(self) -> None:
        """
        Handler: Connection to Socket.IO server lost

        NOTE: Argument 'reason' is not available in Socket.IO version 5.0.3
        (Released in Dec 14,2020). It was added in version 5.12+

        This handler is called when:
        - Connection is lost during operation
        - Server explicitly disconnects the client
        """
        logger.debug("Business: Socket.IO connection lost")

        # NOTE: Socket.IO will attempt automatic reconnection
        #       If reconnection fails permanently, monitor_task via sio.wait()
        #       will detect it and trigger custom reconnection logic

        # Stop Alice state sender to avoid sending updates while offline
        if self._time_rate_sender is not None and getattr(
            self._time_rate_sender, "running", False
        ):
            try:
                await self._time_rate_sender.stop()
                logger.info("Alice state sender stopped due to disconnect")
            except Exception as e:
                logger.exception(
                    "Failed to stop Alice state sender on disconnect: %r", e
                )
        self._unsubscribe_registry_topics()

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
        # - Controller not registerfed on server
        # - Authentication failure
        # - Server rejecting connection
        reason_raw = str(data).strip()
        logger.error("Message from server: %r", reason_raw)

        # Stop Alice state sender to avoid sending updates while offline
        if self._time_rate_sender is not None and getattr(
            self._time_rate_sender, "running", False
        ):
            try:
                await self._time_rate_sender.stop()
                logger.info("Alice state sender stopped due to disconnect")
            except Exception as e:
                logger.exception(
                    "Failed to stop Alice state sender on disconnect: %r", e
                )
        self._unsubscribe_registry_topics()

        # NOTE: Built-in Socket.IO reconnect will NOT try handle connect_error
        #       It reconnection fails permanently, monitor_task via sio.wait()
        #       will detect it and trigger custom reconnection logic

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

    # -------------------------------------------------------------------------
    # Handler Registration
    # -------------------------------------------------------------------------

    def register_with_manager(self, manager) -> None:
        """
        Register all event handlers with SioConnectionManager

        Unlike decorators, we use .on() method for better safety - this approach
        helps control all names and objects at any time and in any context

        Args:
            manager: SioConnectionManager instance
        """
        self._manager = manager

        # System events - registered through manager's .on() for wrapping
        manager.on("connect", self.on_connect)
        manager.on("disconnect", self.on_disconnect)
        manager.on("connect_error", self.on_connect_error)

        # Alice Smart Home events
        manager.on("alice_devices_list", self.on_alice_devices_list)
        manager.on("alice_devices_query", self.on_alice_devices_query)
        manager.on("alice_devices_action", self.on_alice_devices_action)

        logger.debug("All handlers registered with SioConnectionManager")
