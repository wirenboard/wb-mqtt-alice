#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Union


# Address.value can be a scalar address (string) or a composite mapping (dict[str, str])
AddressValue = Union[str, Dict[str, str]]


@dataclass(frozen=True)
class Binding:
    """
    New (preferred) binding record

    Each binding connects a canonical point to downstream addresses

    Input source: config["bindings"] items

    Example (input JSON):
        {
          "downstream": "mqtt_wb_conv",
          "device": "dev1",
          "point": "prop.float.temperature",
          "address": { "value": "wb/dev1/temperature" }
        }

    Example (output object):
        Binding(
            downstream="mqtt_wb_conv",
            device_id="dev1",
            point="prop.float.temperature",
            address_value="wb/dev1/temperature"
        )
    """

    downstream_name: str
    device_id: str
    point: str
    address_value: AddressValue


@dataclass(frozen=True)
class InboundRoute:
    """
    Inbound route for reverse lookup by (downstream, address)

    This is used when a downstream adapter receives a raw message

    Example (when binding.address.value is a scalar):
        ("mqtt_wb_conv", "wb/dev1/temperature") ->
            InboundRoute(
                downstream="mqtt_wb_conv",
                address="wb/dev1/temperature",
                device_id="dev1",
                point="prop.float.temperature",
                value_path="value"
            )

    Example (when binding.address.value is composite, like HSV):
        ("mqtt_wb_conv", "wb/dev1/hue") ->
            InboundRoute(... value_path="value.h")
    """

    downstream_name: str
    address: str
    device_id: str
    point: str
    value_path: str  # "value" or "value.<subkey>"


@dataclass(frozen=True)
class OutboundTarget:
    """
    Outbound target for forward lookup by (device_id, point, value_path)

    This is used when the router wants to write canonical values to downstream

    Example:
        ("dev1", "prop.float.temperature", "value") -> [
            OutboundTarget(downstream="mqtt_wb_conv", address="wb/dev1/temperature")
        ]

    For composites:
        ("dev1", "cap.color_setting.hsv", "value.h") -> [
            OutboundTarget(downstream="mqtt_wb_conv", address="wb/dev1/hue")
        ]
    """

    downstream_name: str
    address: str
