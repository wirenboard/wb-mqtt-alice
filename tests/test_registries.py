#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

# NOTE: build_min_config() tests cover pure in-memory config

from wb_alice_config.device_registry import DeviceRegistry
from wb_alice_config.binding_registry import BindingRegistry


def build_min_config() -> dict:
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
                "downstream": "mqtt_wb_conv",
                "device": "dev1",
                "point": "properties:devices.properties.float:temperature",
                "address": {"value": "wb/dev1/temperature"},
            }
        ],
    }


def test_device_registry_points():
    cfg = build_min_config()
    devreg = DeviceRegistry.from_config(cfg)

    cap = devreg.get_point("dev1", "capabilities:devices.capabilities.on_off:on")
    prop = devreg.get_point("dev1", "properties:devices.properties.float:temperature")

    assert cap is not None
    assert prop is not None

    # Sanity-check some key fields (helps catch point naming regressions)
    assert cap.y_type == "devices.capabilities.on_off"
    assert cap.instance == "on"

    assert prop.y_type == "devices.properties.float"
    assert prop.instance == "temperature"


def test_binding_registry_builds_new_and_legacy():
    cfg = build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)

    # --- New style binding (explicit cfg["bindings"]) ---
    route_t = bindreg.inbound_lookup("mqtt_wb_conv", "wb/dev1/temperature")
    assert route_t is not None
    assert route_t.device_id == "dev1"
    assert route_t.point == "properties:devices.properties.float:temperature"
    assert route_t.value_path == "value"

    # --- Legacy style binding (implicit via capability["mqtt"]) ---
    # Important: legacy is expected to default downstream="mqtt_wb_conv"
    route_r = bindreg.inbound_lookup("mqtt_wb_conv", "wb/dev1/relay")
    assert route_r is not None
    assert route_r.device_id == "dev1"
    assert route_r.point == "capabilities:devices.capabilities.on_off:on"
    assert route_r.value_path == "value"


def test_outbound_lookup_scalar():
    cfg = build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)

    # --- New style: outbound lookup for temperature ---
    targets_new = bindreg.outbound_lookup("dev1", "properties:devices.properties.float:temperature", "value")
    assert len(targets_new) == 1
    assert targets_new[0].downstream_name == "mqtt_wb_conv"
    assert targets_new[0].address == "wb/dev1/temperature"

    # --- Legacy style: outbound lookup for relay on_off ---
    # Must default to downstream="mqtt_wb_conv" even though config didn't state downstream explicitly.
    targets_legacy = bindreg.outbound_lookup("dev1", "capabilities:devices.capabilities.on_off:on", "value")
    assert len(targets_legacy) == 1
    assert targets_legacy[0].downstream_name == "mqtt_wb_conv"
    assert targets_legacy[0].address == "wb/dev1/relay"


def test_inbound_outbound_consistency():
    cfg = build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)

    # --- New style consistency: inbound -> outbound round-trip ---
    r_new = bindreg.inbound_lookup("mqtt_wb_conv", "wb/dev1/temperature")
    assert r_new is not None
    targets_new = bindreg.outbound_lookup(r_new.device_id, r_new.point, r_new.value_path)
    assert any(t.address == "wb/dev1/temperature" for t in targets_new)

    # --- Legacy style consistency: inbound -> outbound round-trip ---
    r_legacy = bindreg.inbound_lookup("mqtt_wb_conv", "wb/dev1/relay")
    assert r_legacy is not None
    targets_legacy = bindreg.outbound_lookup(r_legacy.device_id, r_legacy.point, r_legacy.value_path)
    assert any(t.downstream_name == "mqtt_wb_conv" and t.address == "wb/dev1/relay" for t in targets_legacy)

def test_registry_contains_both_binding_styles_in_bindings_list():
    cfg = build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)

    # One new binding + one legacy binding are expected in bindreg.bindings
    # (temperature from cfg["bindings"], on_off from capability["mqtt"])
    assert len(bindreg.bindings) == 2

    # New style record (explicit downstream in config)
    new_b = next(b for b in bindreg.bindings if b.point == "properties:devices.properties.float:temperature")
    assert new_b.downstream_name == "mqtt_wb_conv"
    assert new_b.address_value == "wb/dev1/temperature"

    # Legacy style record (implicit downstream="mqtt_wb_conv")
    legacy_b = next(b for b in bindreg.bindings if b.point == "capabilities:devices.capabilities.on_off:on")
    assert legacy_b.downstream_name == "mqtt_wb_conv"
    assert legacy_b.address_value == "wb/dev1/relay"


def test_missing_outbound_returns_empty_list_not_none():
    cfg = build_min_config()
    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)

    # Unknown triple should return empty list (API guarantee)
    assert bindreg.outbound_lookup("dev1", "properties:devices.properties.float:temperature", "value.h") == []
