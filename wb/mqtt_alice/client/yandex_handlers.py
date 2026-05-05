#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Yandex Smart Home Handlers for Wiren Board Alice Integration
Handles type conversions and state management for Yandex Smart Home API
"""
import logging
import time
from typing import Any, Callable, Dict, Optional

from wb.mqtt_alice.common.constants import (
    CAP_COLOR_SETTING,
    CAP_MODE,
    CAP_ON_OFF,
    CAP_RANGE,
    CAP_TOGGLE,
    CAP_VIDEO_STREAM,
    PROP_EVENT,
    PROP_FLOAT,
)

from .converters import convert_rgb_wb_to_int, convert_to_bool, convert_to_float

logger = logging.getLogger(__name__)

# Global callback for emitting events (set by main module)
_emit_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None


def set_emit_callback(callback: Callable[[str, Dict[str, Any]], None]) -> None:
    """Set callback function for emitting events to SocketIO"""
    global _emit_callback
    _emit_callback = callback


def _on_off(device_id: str, instance: Optional[str], value: Any) -> Dict[str, Any]:
    return build_device_state_block(device_id, CAP_ON_OFF, instance, convert_to_bool(value))


def _float_prop(device_id: str, instance: Optional[str], value: Any) -> Dict[str, Any]:
    return build_device_state_block(device_id, PROP_FLOAT, instance, convert_to_float(value))


def _range_cap(device_id: str, instance: Optional[str], value: Any) -> Dict[str, Any]:
    return build_device_state_block(device_id, CAP_RANGE, instance, convert_to_float(value))


def _color_setting(device_id: str, instance: Optional[str], value: Any) -> Optional[Dict[str, Any]]:
    """
    Normalize instances to Yandex format:
      - rgb → instance='color', value=int(RGB)
      - temperature_k → instance='temperature_k', value=int
    """
    if instance in ("rgb"):
        if isinstance(value, int):
            rgb_int = value
        else:
            rgb_int = convert_rgb_wb_to_int(str(value))
            if rgb_int is None:
                logger.warning("Failed to parse RGB value: %r", value)
                return None
        return build_device_state_block(device_id, CAP_COLOR_SETTING, "rgb", rgb_int)
    elif instance == "temperature_k":
        return build_device_state_block(
            device_id,
            CAP_COLOR_SETTING,
            "temperature_k",
            int(float(value)),
        )
    else:
        logger.debug("Unsupported instance %r — dropped", instance)
        return None
    # HSV and color scene add there in future


def _event_prop(device_id: str, instance: Optional[str], value: Any) -> Dict[str, Any]:
    # Directly build event property block, value is array of events
    return build_device_state_block(device_id, PROP_EVENT, instance, value)


def _not_implemented(cap_type: str) -> Callable[..., None]:
    def _stub(*_a, **_kw) -> None:
        raise NotImplementedError("Handler for %r is not implemented yet" % cap_type)

    return _stub


# Yandex types handler table for select format logic from MQTT to Yandex
_HANDLERS: Dict[str, Callable[[str, Optional[str], Any], None]] = {
    # Capabilities
    CAP_ON_OFF: _on_off,
    CAP_COLOR_SETTING: _color_setting,
    CAP_VIDEO_STREAM: _not_implemented("cap.video_stream"),
    CAP_MODE: _not_implemented("cap.mode"),
    CAP_RANGE: _range_cap,
    CAP_TOGGLE: _not_implemented("cap.toggle"),
    # Properties
    PROP_FLOAT: _float_prop,
    PROP_EVENT: _event_prop,
}


def _build_state_block(
    block_type: str,
    instance: Optional[str],
    value: Any,
    *,
    is_property: bool,
) -> Dict[str, Any]:
    """
    Builds either a capability **or** a property state block depending
    on *is_property* flag
    """
    key = "properties" if is_property else "capabilities"
    state_key = {
        "type": block_type,
        "state": {"instance": instance, "value": value},
    }
    return {key: [state_key]}


def build_device_state_block(
    device_id: str,
    block_type: str,
    instance: Optional[str],
    value: Any,
) -> Dict[str, Any]:
    """
    Build a device state block dict for a single capability/property update

    Returns:
        Dict with "id", "status", and one of "capabilities"/"properties" keys
    """
    logger.debug(
        "[YANDEX] Building state block: device=%r, type=%r, instance=%r, value=%r",
        device_id,
        block_type,
        instance,
        value,
    )

    is_prop = block_type.startswith("devices.properties")

    # Normalize known value types
    if block_type.endswith("on_off"):
        value = convert_to_bool(value)
    elif block_type.endswith("float"):
        value = convert_to_float(value)

    return {
        "id": device_id,
        "status": "online",
        **_build_state_block(block_type, instance, value, is_property=is_prop),
    }


def emit_batched_states(devices: list) -> None:
    """
    Emit a batch of device state updates to the cloud proxy via Socket.IO
    Called after collecting and merging device blocks

    Args:
        devices: List of device dicts, each with "id", "status",
                 and "capabilities"/"properties" keys
    """
    if not devices:
        return

    payload = {
        "ts": int(time.time()),
        "payload": {
            # user_id will be added by the proxy
            "devices": devices,
        },
    }
    logger.debug("[YANDEX] Emitting batched state for %d device(s)", len(devices))
    try:
        if _emit_callback:
            _emit_callback("device_state", payload)
        else:
            logger.warning("[YANDEX] Emit callback not set, dropping batch")
    except Exception:
        logging.exception("Error when emitting batched state")


def convert_to_yandex_block(
    device_id: str, cap_type: str, instance: Optional[str], value: Any
) -> Optional[Dict[str, Any]]:
    """
    Convert capability/property to Yandex device format state block
    Used by DeviceRegistry as callback

    Returns a device state block dict or None on error
    """
    handler = _HANDLERS.get(cap_type)
    if handler is None:
        logger.error(
            "[YANDEX] Unknown capability/property %r. Supported: %r",
            cap_type,
            ", ".join(_HANDLERS.keys()),
        )
        return None

    try:
        return handler(device_id, instance, value)
    except NotImplementedError as err:
        # Explicit and loud: capability exists in table but lacks implementation
        logger.error("[YANDEX] %r", err)
    except Exception as exc:
        logger.exception("[YANDEX] Handler error for %r: %r", cap_type, exc)
    return None
