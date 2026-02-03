#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

try:
    import paho.mqtt.client as paho_mqtt  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    paho_mqtt = None

from ..base import DownstreamAdapter, RawMessageHandler
from ..models import DownstreamWrite, RawDownstreamMessage


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MqttConnectionConfig:
    """
    MQTT connection settings for paho-mqtt client
    """

    host: str
    port: int = 1883
    client_id: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    keepalive: int = 60
    qos: int = 0
    retain: bool = False


class MqttWbConvAdapter(DownstreamAdapter):
    """
    Real MQTT adapter using paho-mqtt

    Notes:
      - In tests we inject a mocked paho client via `client=...`
      - In production we create the client automatically
    """

    def __init__(
        self,
        *,
        cfg: MqttConnectionConfig,
        subscriptions: Iterable[str],
        client: Optional[Any] = None,
    ) -> None:
        self._cfg = cfg
        self._subs = list(subscriptions or [])
        self._handler: Optional[RawMessageHandler] = None

        if client is None:
            if paho_mqtt is None:  # pragma: no cover
                raise RuntimeError("paho-mqtt is not installed. Install it with: pip install paho-mqtt")
            self._client = paho_mqtt.Client(client_id=cfg.client_id)
        else:
            self._client = client

        # Configure auth if provided
        if getattr(cfg, "username", None):
            self._client.username_pw_set(cfg.username, cfg.password)

        # Callbacks
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    @property
    def downstream_name(self) -> str:
        return "mqtt_wb_conv"

    def start(self, handler: RawMessageHandler) -> None:
        self._handler = handler
        logger.info(
            "Starting MQTT adapter: host=%s port=%s subs=%d",
            self._cfg.host,
            self._cfg.port,
            len(self._subs),
        )
        self._client.connect(self._cfg.host, self._cfg.port, keepalive=self._cfg.keepalive)

        # Subscribe immediately so that unit tests (mock client) can assert subscribe calls
        # without simulating a full broker connect cycle
        for topic in self._subs:
            self._client.subscribe(topic)

        # Start network loop in background thread
        self._client.loop_start()

    def stop(self) -> None:
        logger.info("Stopping MQTT adapter")
        try:
            self._client.loop_stop()
        finally:
            try:
                self._client.disconnect()
            except Exception:
                logger.exception("MQTT disconnect failed")

    def write(self, req: DownstreamWrite) -> None:
        if req.downstream_name != self.downstream_name:
            raise ValueError(f"Downstream mismatch: expected={self.downstream_name} got={req.downstream_name}")
        payload_bytes = req.payload
        if isinstance(payload_bytes, str):
            payload_bytes = payload_bytes.encode("utf-8")
        logger.debug("MQTT publish: topic=%s payload=%r", req.address, payload_bytes)
        self._client.publish(req.address, payload=payload_bytes, qos=self._cfg.qos, retain=self._cfg.retain)

    # ---- paho callbacks ----

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        logger.info("MQTT connected: rc=%s", rc)
        # We already subscribed in start(), but double-subscribe is usually safe
        for topic in self._subs:
            try:
                client.subscribe(topic)
            except Exception:
                logger.exception("MQTT subscribe failed: %s", topic)

    def _on_disconnect(self, client: Any, userdata: Any, rc: int) -> None:
        logger.warning("MQTT disconnected: rc=%s", rc)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        if not self._handler:
            return
        try:
            payload = msg.payload
        except Exception:
            payload = None
        raw = RawDownstreamMessage(
            downstream_name=self.downstream_name,
            address=str(getattr(msg, "topic", "")),
            payload=payload,
            meta={"qos": getattr(msg, "qos", None), "retain": getattr(msg, "retain", None)},
        )
        self._handler(raw)
