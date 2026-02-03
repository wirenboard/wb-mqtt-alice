#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from .binding_registry import BindingRegistry
from .device_registry import DeviceRegistry
from .state_store import StateStore
from .downstream.base import DownstreamAdapter, DownstreamCodec
from .downstream.models import DownstreamWrite, RawDownstreamMessage

logger = logging.getLogger(__name__)


class Router:
    """
    Routes messages between downstream adapters and internal values states

    Responsibilities:
      1) Inbound (downstream -> canonical):
         - Receive RawDownstreamMessage from an adapter
         - Find binding via BindingRegistry.inbound_lookup(downstream_name, address)
         - Decode payload via the downstream codec using PointSpec + value_path
         - Store canonical value in StateStore

      2) Outbound (canonical -> downstream):
         - Accept a canonical value set for (device_id, point, value_path)
         - Find one or more outbound targets via BindingRegistry.outbound_lookup(...)
         - Encode canonical value into downstream payload via codec
         - Send to adapter via adapter.write(DownstreamWrite)

    Notes:
      - Router does NOT know MQTT/HTTP/etc conventions; codecs do
      - Router does NOT parse device config; DeviceRegistry does
      - Router does NOT own bindings; BindingRegistry does
    """

    devreg: DeviceRegistry
    bindreg: BindingRegistry
    store: StateStore
    codecs: Dict[str, DownstreamCodec]
    adapters: Dict[str, DownstreamAdapter]

    def on_downstream_message(self, msg: RawDownstreamMessage) -> None:
        route = self.bindreg.inbound_lookup(msg.downstream_name, msg.address)
        if route is None:
            logger.debug("No inbound route for %s/%s", msg.downstream_name, msg.address)
            return

        spec = self.devreg.get_point(route.device_id, route.point)
        if spec is None:
            logger.warning("Binding points to missing PointSpec: %s %s", route.device_id, route.point)
            return

        codec = self.codecs.get(msg.downstream_name)
        if codec is None:
            logger.warning("No codec registered for downstream=%r", msg.downstream_name)
            return

        canonical = codec.decode(spec, route.value_path, msg.payload)
        self.store.set_value(route.device_id, route.point, route.value_path, canonical)

    def set_canonical(self, device_id: str, point: str, value_path: str, canonical: Any) -> None:
        """Set canonical value and fan it out to downstream targets."""
        self.store.set_value(device_id, point, value_path, canonical)

        spec = self.devreg.get_point(device_id, point)
        if spec is None:
            logger.warning("Unknown PointSpec: %s %s", device_id, point)
            return

        targets = self.bindreg.outbound_lookup(device_id, point, value_path)
        for t in targets:
            codec = self.codecs.get(t.downstream)
            adapter = self.adapters.get(t.downstream)
            if codec is None:
                logger.warning("No codec for outbound downstream=%r", t.downstream)
                continue
            if adapter is None:
                logger.warning("No adapter for outbound downstream=%r", t.downstream)
                continue
            raw = codec.encode(spec, value_path, canonical)
            adapter.write(
                DownstreamWrite(
                    downstream_name=t.downstream,
                    address=t.address,
                    payload=raw,
                    meta=None,
                )
            )
