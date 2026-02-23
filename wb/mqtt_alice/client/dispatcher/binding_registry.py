#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .device_registry import DeviceRegistry
from .models import Binding, InboundRoute, OutboundTarget

DEFAULT_LEGACY_DOWNSTREAM_NAME = "mqtt_wb_conv"

def _iter_address_value_map(address_value: Any) -> List[Tuple[str, str]]:
    """
    Normalize binding.address_value into list of (suffix, address).

    Input:
      - string: "topic/x"
      - dict: {"h":"topic/h","s":"topic/s"}

    Output:
      list of (suffix, address)
        - for string: [("", "topic/x")]
        - for dict: [("h","topic/h"), ("s","topic/s")]

    Examples:
        _iter_address_value_map("t") -> [("", "t")]
        _iter_address_value_map({"h":"a","s":"b"}) -> [("h","a"),("s","b")]
    """
    if isinstance(address_value, str):
        return [("", address_value)]
    if isinstance(address_value, dict):
        out: List[Tuple[str, str]] = []
        for k, v in address_value.items():
            if not isinstance(v, str):
                raise ValueError(f"Composite address.value must be dict[str,str], got key={k!r} value={v!r}")
            out.append((str(k), v))
        return out
    raise ValueError(f"Unsupported address.value type: {type(address_value).__name__}")


def _make_point_ref_for_legacy(kind: str, y_type: str, instance: str) -> str:
    """
    Build point_ref for legacy in exactly same format as DeviceRegistry.

    This is used only for legacy fallback when point_ref is not explicitly provided
    in config["bindings"] and we need to derive it from capability/property blocks.

    Format:
      capabilities:<y_type>:<instance>
      properties:<y_type>:<instance>
    """
    group = "capabilities" if kind == "cap" else "properties"
    return f"{group}:{y_type}:{instance}"


@dataclass
class BindingRegistry:
    """
    Holds combined bindings (new + legacy fallback) and both-direction indexes.

    - inbound index:
        (downstream_name, address) -> InboundRoute(device_id, point, value_path)

    - outbound index:
        (device_id, point, value_path) -> list[OutboundTarget(downstream_name, address)]

    Notes:
      outbound is a list to support composites (HSV) and potential multi-target fanout.

    Example usage:

      # Inbound lookup (adapter -> router):
      route = bindreg.inbound[("mqtt_wb_conv","wb/dev1/temperature")]
      # route.device_id == "dev1"
      # route.point == "prop.float.temperature"
      # route.value_path == "value"

      # Outbound lookup (router -> adapter write):
      targets = bindreg.outbound[("dev1","prop.float.temperature","value")]
      # targets == [OutboundTarget(downstream_name="mqtt_wb_conv", address="wb/dev1/temperature")]

    """

    bindings: List[Binding]
    inbound: Dict[Tuple[str, str], InboundRoute]
    outbound: Dict[Tuple[str, str, str], List[OutboundTarget]]

    @classmethod
    def from_config(cls, cfg: Dict[str, Any], devreg: DeviceRegistry) -> "BindingRegistry":
        """
        Build BindingRegistry using:
          Pass 1: cfg["bindings"] (new format)
          Pass 2: legacy fallback from cfg["devices"][...].capabilities/properties "mqtt" field

        Input: cfg dict (full config), and DeviceRegistry (must already exist).

        Output:
          BindingRegistry with:
            - bindings list
            - inbound index for reverse lookup by topic/address
            - outbound index for forward lookup by (device, point, value_path)

        Legacy rules (minimal):
          - downstream_name assumed "mqtt_wb_conv"
          - legacy mqtt binds to value_path="value"
          - legacy is used only if no new binding exists for that point

        Point reference format (human-editable, mirrors Yandex):
          - capabilities:<y_type>:<instance>
          - properties:<y_type>:<instance>
        """
        bindings: List[Binding] = []
        inbound: Dict[Tuple[str, str], InboundRoute] = {}
        outbound: Dict[Tuple[str, str, str], List[OutboundTarget]] = {}

        def add_route(downstream_name: str, device_id: str, point: str, suffix: str, addr: str) -> None:
            value_path = "value" if suffix == "" else f"value.{suffix}"

            # inbound (single route per (downstream_name,address) - last wins if duplicate)
            inbound[(downstream_name, addr)] = InboundRoute(
                downstream_name=downstream_name,
                address=addr,
                device_id=device_id,
                point=point,
                value_path=value_path,
            )

            # outbound (may be multiple targets)
            out_key = (device_id, point, value_path)
            outbound.setdefault(out_key, []).append(OutboundTarget(downstream_name=downstream_name, address=addr))

        # ---- Pass 1: new bindings[] ----
        for b in cfg.get("bindings", []) or []:
            downstream_name = str(b.get("downstream", "")).strip()
            device_id = str(b.get("device"))
            point = str(b.get("point"))
            address = b.get("address") or {}
            address_value = address.get("value")

            if address_value is None:
                raise ValueError(f"Binding {device_id}.{point} missing address.value")

            # Validate point exists in DeviceRegistry (point_ref must match DeviceRegistry format)
            if devreg.get_point(device_id, point) is None:
                raise ValueError(f"Binding references unknown point: {device_id}.{point}")

            binding = Binding(
                downstream_name=downstream_name,
                device_id=device_id,
                point=point,
                address_value=address_value,
            )
            bindings.append(binding)

            for suffix, addr in _iter_address_value_map(address_value):
                add_route(downstream_name, device_id, point, suffix, addr)

        new_bound = {(b.device_id, b.point) for b in bindings}

        # ---- Pass 2: legacy fallback ----
        devices = cfg.get("devices", {}) or {}
        for device_id, dev in devices.items():
            # Capabilities legacy
            for cap in dev.get("capabilities", []) or []:
                params = cap.get("parameters") or {}
                instance = str(params.get("instance", ""))
                y_type = str(cap.get("type"))
                point = _make_point_ref_for_legacy("cap", y_type, instance)

                if (device_id, point) in new_bound:
                    continue

                mqtt_topic = cap.get("mqtt")
                if isinstance(mqtt_topic, str) and mqtt_topic.strip():
                    topic = mqtt_topic.strip()
                    bindings.append(
                        Binding(
                            downstream_name=DEFAULT_LEGACY_DOWNSTREAM_NAME,
                            device_id=device_id,
                            point=point,
                            address_value=topic,
                        )
                    )
                    add_route("mqtt_wb_conv", device_id, point, "", topic)

            # Properties legacy (optional)
            for prop in dev.get("properties", []) or []:
                params = prop.get("parameters") or {}
                instance = str(params.get("instance", ""))
                y_type = str(prop.get("type"))
                point = _make_point_ref_for_legacy("prop", y_type, instance)

                if (device_id, point) in new_bound:
                    continue

                mqtt_topic = prop.get("mqtt")
                if isinstance(mqtt_topic, str) and mqtt_topic.strip():
                    topic = mqtt_topic.strip()
                    bindings.append(
                        Binding(
                            downstream_name=DEFAULT_LEGACY_DOWNSTREAM_NAME,
                            device_id=device_id,
                            point=point,
                            address_value=topic,
                        )
                    )
                    add_route("mqtt_wb_conv", device_id, point, "", topic)

        return cls(bindings=bindings, inbound=inbound, outbound=outbound)

    def inbound_lookup(self, downstream_name: str, address: str) -> Optional[InboundRoute]:
        """
        Find inbound route by (downstream_name, address).

        Example:
          route = bindreg.inbound_lookup("mqtt_wb_conv","wb/dev1/relay")
          if route:
              # route.device_id, route.point, route.value_path
        """
        return self.inbound.get((downstream_name, address))

    def outbound_lookup(self, device_id: str, point: str, value_path: str) -> List[OutboundTarget]:
        """
        Find outbound targets by (device_id, point, value_path).

        Always returns a list (empty list if not found).

        Examples:
          # scalar:
          bindreg.outbound_lookup("dev1","prop.float.temperature","value")
            -> [OutboundTarget("mqtt_wb_conv","wb/dev1/temperature")]

          # composite:
          bindreg.outbound_lookup("dev1","cap.color_setting.hsv","value.h")
            -> [OutboundTarget("mqtt_wb_conv","wb/dev1/hue")]
        """
        return list(self.outbound.get((device_id, point, value_path), []))
