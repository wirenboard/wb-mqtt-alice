#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .models import PointSpec


def _make_point_ref(group: str, y_type: str, instance: str) -> str:
    """
    Build human-editable point reference string that mirrors Yandex structure

    Format:
      - capabilities:<y_type>:<instance>
      - properties:<y_type>:<instance>

    Examples:
        _make_point_ref("capabilities", "devices.capabilities.on_off", "on")
          -> "capabilities:devices.capabilities.on_off:on"

        _make_point_ref("properties", "devices.properties.float", "temperature")
          -> "properties:devices.properties.float:temperature"
    """
    # Keep it simple for humans editing config: 3 segments separated by ":".
    return f"{group}:{y_type}:{instance}"


@dataclass
class DeviceRegistry:
    """
    Builds PointSpec objects from config.devices.

    This registry does NOT depend on any downstream protocol.

    Output index:
      points[(device_id, point)] -> PointSpec

    Example:
      cfg = {...}
      devreg = DeviceRegistry.from_config(cfg)
      devreg.get_point("dev1", "capabilities:devices.capabilities.on_off:on") -> PointSpec(...)

    """

    points: Dict[Tuple[str, str], PointSpec]

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "DeviceRegistry":
        """
        Parse config["devices"] and build canonical points.

        Input (fragment):
            {
              "devices": {
                "dev1": {
                  "capabilities": [{"type":"devices.capabilities.on_off","parameters":{"instance":"on"}, ...}],
                  "properties": [{"type":"devices.properties.float","parameters":{"instance":"temperature"}, ...}]
                }
              }
            }

        Output:
            DeviceRegistry with points:
              ("dev1","capabilities:devices.capabilities.on_off:on") -> PointSpec(...)
              ("dev1","properties:devices.properties.float:temperature") -> PointSpec(...)
        """
        points: Dict[Tuple[str, str], PointSpec] = {}

        devices = cfg.get("devices", {}) or {}
        for device_id, dev in devices.items():
            # Capabilities
            for cap in dev.get("capabilities", []) or []:
                y_type = str(cap.get("type"))
                params = cap.get("parameters") or {}
                instance = str(params.get("instance", ""))

                point = _make_point_ref("capabilities", y_type, instance)

                points[(device_id, point)] = PointSpec(
                    device_id=device_id,
                    point=point,
                    y_type=y_type,
                    instance=instance,
                    parameters=params,
                )

            # Properties
            for prop in dev.get("properties", []) or []:
                y_type = str(prop.get("type"))
                params = prop.get("parameters") or {}
                instance = str(params.get("instance", ""))

                point = _make_point_ref("properties", y_type, instance)

                points[(device_id, point)] = PointSpec(
                    device_id=device_id,
                    point=point,
                    y_type=y_type,
                    instance=instance,
                    parameters=params,
                )

        return cls(points=points)

    def get_point(self, device_id: str, point: str) -> Optional[PointSpec]:
        """Return PointSpec by (device_id, point) or None if not found."""
        return self.points.get((device_id, point))
