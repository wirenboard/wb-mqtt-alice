#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import Any, Mapping, Optional

from ..base import DownstreamCodec
from ...models import PointSpec

class MqttWbConvCodec(DownstreamCodec):
    """
    Codec for MQTT Wiren Board convention.

    Minimal supported conversions:
      - devices.capabilities.on_off  <-> bool
      - devices.properties.float     <-> float

    This can be expanded later (buttons/events, HSV, enums, etc).
    """

    @property
    def downstream_name(self) -> str:
        return "mqtt_wb_conv"

    def decode(
        self,
        spec: PointSpec,
        value_path: str,
        raw: Any,
    ) -> Any:
        if value_path != "value":
            # Minimal codec: only scalar "value" supported now.
            return None

        if isinstance(raw, (bytes, bytearray)):
            s = raw.decode("utf-8", errors="replace").strip()
        else:
            s = str(raw).strip()

        if spec.y_type == "devices.capabilities.on_off":
            sl = s.lower()
            if sl in ("1", "true", "on", "yes"):
                return True
            if sl in ("0", "false", "off", "no"):
                return False
            # Unknown -> return as-is (router may decide)
            return s

        if spec.y_type == "devices.properties.float":
            try:
                return float(s)
            except ValueError:
                return None

        return s

    def encode(
        self,
        spec: PointSpec,
        value_path: str,
        canonical: Any,
    ) -> Any:
        if value_path != "value":
            return None

        if spec.y_type == "devices.capabilities.on_off":
            if isinstance(canonical, bool):
                return b"1" if canonical else b"0"
            s = str(canonical).strip().lower()
            if s in ("1", "true", "on", "yes"):
                return b"1"
            if s in ("0", "false", "off", "no"):
                return b"0"
            return str(canonical).encode("utf-8")

        if spec.y_type == "devices.properties.float":
            try:
                return str(float(canonical)).encode("utf-8")
            except Exception:
                return None

        return str(canonical).encode("utf-8")
