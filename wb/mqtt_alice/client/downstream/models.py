#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class RawDownstreamMessage:
    """
    Raw message produced by a downstream adapter

    This object is the adapter output BEFORE any codec conversion

    NOTE:
      Router does NOT assume anything about payload format
      It only uses (downstream, address) to find binding and then asks codec
      how to interpret payload for a given PointSpec

    Fields:
      - downstream_name: downstream id (e.g. "mqtt_wb_conv")
      - address: transport-level address (e.g. MQTT topic)
      - payload: raw payload (bytes/str/number depending on adapter)
      - meta: optional metadata (e.g. qos/retain for MQTT)
    """

    downstream_name: str
    address: str
    payload: Any
    meta: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class DownstreamWrite:
    """
    Request to write raw payload to a downstream address

    This is router output BEFORE codec conversion if router already prepared payload,
    OR after codec conversion if codec returns bytes and router passes them here
    """

    downstream_name: str
    address: str
    payload: Any
    meta: Optional[Mapping[str, Any]] = None
