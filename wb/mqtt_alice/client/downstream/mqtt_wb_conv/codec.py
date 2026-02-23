#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import Any

from ..base import DownstreamCodec
from ...models import PointSpec
from .converters import (
    convert_to_bool,
    convert_to_float,
    convert_rgb_wb_to_int,
    convert_rgb_int_to_wb,
    convert_temp_percent_to_kelvin,
    convert_temp_kelvin_to_percent,
    convert_mqtt_event_value,
)

logger = logging.getLogger(__name__)

class MqttWbConvCodec(DownstreamCodec):
    """
    Codec for MQTT Wiren Board convention

    Handles conversions between WB payload formats and Yandex Smart Home primitives
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
            return None

        # Convert raw msg to correct string
        if isinstance(raw, (bytes, bytearray)):
            s = raw.decode("utf-8", errors="replace").strip()
        else:
            s = str(raw).strip()

        # Get data from PointSpec
        y_type = spec.y_type
        instance = spec.instance
        params = spec.parameters

        # Decode logic (MQTT -> Yandex)
        if y_type == "devices.capabilities.on_off":
            return convert_to_bool(s)
        
        elif y_type in ("devices.properties.float", "devices.capabilities.range"):
            return convert_to_float(s)

        elif y_type == "devices.capabilities.color_setting":
            if instance == "rgb":
                rgb_int = convert_rgb_wb_to_int(s)
                if rgb_int is None:
                    logger.warning("Failed to decode RGB value: %r", s)
                return rgb_int

            elif instance == "temperature_k":
                t_params = params.get("temperature_k", {})
                min_k = t_params.get("min", 2700)
                max_k = t_params.get("max", 6500)
                try:
                    return convert_temp_percent_to_kelvin(float(s), min_k, max_k)
                except ValueError:
                    return None
            else:
                return s

        elif y_type == "devices.properties.event":
            # For events we need additional params from specification
            event_val = params.get("value")
            is_single_topic = getattr(spec, "is_single_topic", False)
            return convert_mqtt_event_value(instance, event_val, s, is_single_topic)

        # Fallback for unknown types
        return s

    def encode(
        self,
        spec: PointSpec,
        value_path: str,
        canonical: Any,
    ) -> Any:
        if value_path != "value":
            return None

        # Get data from PointSpec
        y_type = spec.y_type
        instance = spec.instance
        params = spec.parameters
        
        # Encode logic (Yandex -> MQTT)
        if y_type == "devices.capabilities.on_off":
            val = convert_to_bool(canonical)
            return b"1" if val else b"0"

        elif y_type in ("devices.properties.float", "devices.capabilities.range"):
            try:
                return str(float(canonical)).encode("utf-8")
            except Exception:
                return None

        elif y_type == "devices.capabilities.color_setting":
            if instance == "rgb":
                try:
                    wb_rgb = convert_rgb_int_to_wb(int(canonical))
                    return wb_rgb.encode("utf-8")
                except (ValueError, TypeError):
                    logger.warning("Failed to encode RGB value: %r", canonical)
                    return None

            elif instance == "temperature_k":
                t_params = params.get("temperature_k", {})
                min_k = t_params.get("min", 2700)
                max_k = t_params.get("max", 6500)
                try:
                    pct = convert_temp_kelvin_to_percent(int(canonical), min_k, max_k)
                    return str(pct).encode("utf-8")
                except (ValueError, TypeError):
                    logger.warning("Failed to encode temp value: %r", canonical)
                    return None
            else:
                return str(canonical).encode("utf-8")

        # Fallback for unknown types
        return str(canonical).encode("utf-8")
