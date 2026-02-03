#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from pathlib import Path

import pytest

from wb_alice_config.config_loader import load_config
from wb_alice_config.device_registry import DeviceRegistry
from wb_alice_config.binding_registry import BindingRegistry


def test_load_config_from_file_and_build_registries():
    """
    Integration-style test:
      - load JSON from tests/configs/config_example.json
      - build DeviceRegistry + BindingRegistry
      - verify both new-style and legacy-style bindings exist
    """
    cfg_path = Path(__file__).parent / "configs" / "config_example.json"
    cfg = load_config(str(cfg_path)).raw

    devreg = DeviceRegistry.from_config(cfg)
    bindreg = BindingRegistry.from_config(cfg, devreg)

    # Points exist
    assert devreg.get_point("dev1", "capabilities:devices.capabilities.on_off:on") is not None
    assert devreg.get_point("dev1", "properties:devices.properties.float:temperature") is not None

    # New style binding from cfg["bindings"]
    r_new = bindreg.inbound_lookup("mqtt_wb_conv", "wb/dev1/temperature")
    assert r_new is not None
    assert r_new.device_id == "dev1"
    assert r_new.point == "properties:devices.properties.float:temperature"
    assert r_new.value_path == "value"

    # Legacy fallback binding from capability["mqtt"] (defaults downstream="mqtt_wb_conv")
    r_legacy = bindreg.inbound_lookup("mqtt_wb_conv", "wb/dev1/relay")
    assert r_legacy is not None
    assert r_legacy.device_id == "dev1"
    assert r_legacy.point == "capabilities:devices.capabilities.on_off:on"
    assert r_legacy.value_path == "value"

    # Combined bindings list: 1 new + 1 legacy
    assert len(bindreg.bindings) == 2


def test_new_binding_unknown_point_raises():
    """
    A new-style binding must reference an existing PointSpec
    """
    cfg = {
        "devices": {
            "dev1": {
                "name": "Demo",
                "type": "devices.types.sensor",
                "capabilities": [],
                "properties": [],
            }
        },
        "bindings": [
            {
                "downstream": "mqtt_wb_conv",
                "device": "dev1",
                "point": "properties:devices.properties.float:temperature",  # not declared in devices
                "address": {"value": "wb/dev1/temperature"},
            }
        ],
    }

    devreg = DeviceRegistry.from_config(cfg)
    with pytest.raises(ValueError, match="unknown point"):
        BindingRegistry.from_config(cfg, devreg)


def test_composite_address_value_type_validation():
    """
    address.value may be:
      - str
      - dict[str,str]
    Any non-string values inside dict must raise
    """
    cfg = {
        "devices": {
            "dev1": {
                "name": "Demo",
                "type": "devices.types.sensor",
                "capabilities": [],
                "properties": [
                    {
                        "type": "devices.properties.float",
                        "parameters": {"instance": "temperature"},
                    }
                ],
            }
        },
        "bindings": [
            {
                "downstream": "mqtt_wb_conv",
                "device": "dev1",
                "point": "properties:devices.properties.float:temperature",
                "address": {"value": {"a": "ok", "b": 123}},  # invalid: 123 is not str
            }
        ],
    }

    devreg = DeviceRegistry.from_config(cfg)
    with pytest.raises(ValueError, match="Composite address.value must be dict\\[str,str\\]"):
        BindingRegistry.from_config(cfg, devreg)
