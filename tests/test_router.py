#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

"""
Router tests.

We check 2 directions:

1) Inbound (downstream -> router -> state store):
   - adapter provides RawDownstreamMessage
   - router finds inbound binding by (downstream,address)
   - router decodes payload using codec
   - router stores canonical value into StateStore

2) Outbound (router -> adapter):
   - router writes canonical value
   - router finds outbound binding by (device_id,point,value_path)
   - router encodes value using codec
   - router sends DownstreamWrite to adapter
"""

from dataclasses import dataclass
from typing import Any, Dict, List

from wb_alice_config.binding_registry import BindingRegistry
from wb_alice_config.device_registry import DeviceRegistry
from wb_alice_config.router import Router
from wb_alice_config.state_store import StateStore
from wb_alice_config.downstream.models import DownstreamWrite, RawDownstreamMessage


def _build_min_config() -> dict:
    """
    Minimal config with 2 points:
    - on_off capability uses legacy binding style (capability["mqtt"])
    - float temperature uses new binding style (cfg["bindings"])
    """
    return {
        "devices": {
            "dev1": {
                "name": "Demo",
                "type": "devices.types.sensor",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "mqtt": "wb/dev1/relay",
                        "parameters": {"instance": "on"},
                    }
                ],
                "properties": [
                    {
                        "type": "devices.properties.float",
                        "retrievable": True,
                        "reportable": True,
                        "parameters": {
                            "instance": "temperature",
                            "unit": "unit.temperature.celsius",
                        },
                    }
                ],
            }
        },
        "bindings": [
            {
                "downstream": "mqtt_wb",
                "device": "dev1",
                "point": "properties:devices.properties.float:temperature",
                "address": {"value": "wb/dev1/temperature"},
            }
        ],
    }


@dataclass
class DummyAdapter:
    """
    Minimal adapter stub:
    - write() collects requests so we can assert what router sent
    """

    writes: List[DownstreamWrite]

    def __init__(self) -> None:
        self.writes = []

    def start(self, handler) -> None:  # pragma: no cover
        self._handler = handler

    def stop(self) -> None:  # pragma: no cover
        return None

    def write(self, req: DownstreamWrite) -> None:
        self.writes.append(req)


class DummyCodec:
    """
    Minimal codec stub:
    - decodes on_off: b"1"/b"0" to bool
    - decodes float: b"23.5" to float
    - encodes bool to b"1"/b"0"
    - encodes float to ASCII bytes
    """

    def decode(
        self,
        *,
        point_y_type: str,
        value_path: str,
        payload: bytes,
        point_params: Dict[str, Any],
        binding_meta: Dict[str, Any] | None = None,
    ) -> Any:
        if point_y_type == "devices.capabilities.on_off":
            s = payload.decode("utf-8").strip().lower()
            return s in ("1", "true", "on", "yes")
        if point_y_type == "devices.properties.float":
            return float(payload.decode("utf-8").strip())
        raise AssertionError(f"Unexpected point_y_type={point_y_type}")

    def encode(
        self,
        *,
        point_y_type: str,
        value_path: str,
        value: Any,
        point_params: Dict[str, Any],
        binding_meta: Dict[str, Any] | None = None,
    ) -> bytes:
        if point_y_type == "devices.capabilities.on_off":
            return b"1" if bool(value) else b"0"
        if point_y_type == "devices.properties.float":
            return str(float(value)).encode("utf-8")
        raise AssertionError(f"Unexpected point_y_type={point_y_type}")


def test_router_inbound_decodes_and_stores_values():
    cfg = _build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)
    store = StateStore()

    router = Router(
        devreg=devreg,
        bindreg=bindreg,
        store=store,
        codecs={"mqtt_wb": DummyCodec()},
        adapters={"mqtt_wb": DummyAdapter()},
    )

    # Legacy binding: wb/dev1/relay -> on_off.on -> value
    router.on_downstream_message(
        RawDownstreamMessage(downstream="mqtt_wb", address="wb/dev1/relay", payload=b"1")
    )
    assert (
        store.get_value("dev1", "capabilities:devices.capabilities.on_off:on", "value")
        is True
    )

    # New binding: wb/dev1/temperature -> float.temperature -> value
    router.on_downstream_message(
        RawDownstreamMessage(downstream="mqtt_wb", address="wb/dev1/temperature", payload=b"18.25")
    )
    assert store.get_value("dev1", "properties:devices.properties.float:temperature", "value") == 18.25


def test_router_outbound_encodes_and_writes_to_adapter():
    cfg = _build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)
    store = StateStore()

    adapter = DummyAdapter()
    router = Router(
        devreg=devreg,
        bindreg=bindreg,
        store=store,
        codecs={"mqtt_wb": DummyCodec()},
        adapters={"mqtt_wb": adapter},
    )

    # Write bool (legacy binding uses capability["mqtt"])
    router.write_canonical("dev1", "capabilities:devices.capabilities.on_off:on", "value", False)

    # Write float (new binding uses cfg["bindings"])
    router.write_canonical("dev1", "properties:devices.properties.float:temperature", "value", 21.5)

    assert len(adapter.writes) == 2
    assert adapter.writes[0] == DownstreamWrite(downstream="mqtt_wb", address="wb/dev1/relay", payload=b"0")
    assert adapter.writes[1] == DownstreamWrite(
        downstream="mqtt_wb", address="wb/dev1/temperature", payload=b"21.5"
    )


def test_router_inbound_ignores_unknown_binding():
    cfg = _build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)
    store = StateStore()

    router = Router(
        devreg=devreg,
        bindreg=bindreg,
        store=store,
        codecs={"mqtt_wb": DummyCodec()},
        adapters={"mqtt_wb": DummyAdapter()},
    )

    router.on_downstream_message(
        RawDownstreamMessage(downstream="mqtt_wb", address="wb/dev1/unknown", payload=b"123")
    )

    # No state must be created for unknown routes
    assert store.get_value("dev1", "capabilities:devices.capabilities.on_off:on", "value") is None
    assert store.get_value("dev1", "properties:devices.properties.float:temperature", "value") is None
