#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


# TODO: PointSpec need split into separate file or change codec in downstream
#       becoase PointSpec used in two place like contract - in downstream and dispatcher
@dataclass(frozen=True)
class PointSpec:
    """
    Canonical description of a point inside a device

    This is built from config.devices[device_id].capabilities/properties

    Fields:
      - device_id: device identifier from config
      - point: canonical point reference (human-editable), e.g.
          "capabilities:devices.capabilities.on_off:on"
          "properties:devices.properties.float:temperature"
      - y_type: full Yandex type string, e.g. "devices.capabilities.on_off"
      - instance: instance string, e.g. "on" or "temperature"
      - parameters: original "parameters" object from config

    Example (output):
        PointSpec(
            device_id="dev1",
            point="capabilities:devices.capabilities.on_off:on",
            y_type="devices.capabilities.on_off",
            instance="on",
            parameters={"instance":"on"}
        )
    """

    device_id: str
    point: str
    y_type: str
    instance: str
    parameters: Dict[str, Any]
    is_single_topic: bool = False
