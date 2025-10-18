#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Yandex Smart Home Handlers for Wiren Board Alice Integration
Handles type conversions and state management for Yandex Smart Home API
"""
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from constants import (
  CAP_ON_OFF, CAP_COLOR_SETTING, CAP_RANGE, CAP_TOGGLE, CAP_MODE, CAP_VIDEO_STREAM,
  PROP_FLOAT, PROP_EVENT
)


from converters import (
    convert_to_bool,
    convert_to_float,
    convert_rgb_wb_to_int,
)

logger = logging.getLogger(__name__)

# Global callback for emitting events (set by main module)
_emit_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None


def set_emit_callback(callback: Callable[[str, Dict[str, Any]], None]) -> None:
    """Set callback function for emitting events to SocketIO"""
    global _emit_callback
    _emit_callback = callback


def _to_bool(raw_state: Any) -> bool:
    """
    Conversion to bool according to Yandex on_off rules
    """
    if isinstance(raw_state, bool):
        return raw_state
    elif isinstance(raw_state, (int, float)):
        return raw_state != 0
    elif isinstance(raw_state, str):
        raw_state = raw_state.strip().lower()
        if raw_state in {"1", "true", "on"}:
            return True
        elif raw_state in {"0", "false", "off"}:
            return False
        elif raw_state.isdigit():
            return int(raw_state) != 0
        else:
            logger.debug("Unknown string %r → False", raw_state)
            return False
    else:
        logger.debug("Unknown value type %s → False", type(raw_state).__name__)
        return False


def _to_float(raw: Any) -> float:
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.debug("Cannot convert %r to float → 0.0", raw)
        return 0.0


def _on_off(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, CAP_ON_OFF, instance, _to_bool(value)
    )


def _float_prop(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, PROP_FLOAT, instance, _to_float(value)
    )


def _rgb_to_int(red: int, green: int, blue: int) -> int:
    """
    Convert RGB components to a single integer value
    
    Combines red, green, and blue color components (0-255) into a single 
    24-bit integer using bit shifting: (R << 16) | (G << 8) | B
    
    Args:
        red: Red component (0-255)
        green: Green component (0-255) 
        blue: Blue component (0-255)
        
    Returns:
        int: RGB value as 24-bit integer (0-16777215)
        
    Example:
        >>> _rgb_to_int(255, 128, 0)
        16744448  # 0xFF8000
    """
    red = max(0, min(255, int(red)))
    green = max(0, min(255, int(green)))
    blue = max(0, min(255, int(blue)))
    return (red << 16) | (green << 8) | blue


def int_to_rgb_wb_format(val: int) -> str:
    """
    Convert integer RGB value to WirenBoard format string
    
    Extracts RGB components from a 24-bit integer and formats them 
    as semicolon-separated string for WirenBoard MQTT
    
    Args:
        val: RGB integer value (0-16777215)
        
    Returns:
        str: RGB components in WirenBoard format "R;G;B"
        
    Example:
        >>> int_to_rgb_wb_format(16744448)
        "255;128;0"  # 0xFF8000 → "255;128;0"
    """
    rgb_value = int(val) & 0xFFFFFF
    red = (rgb_value >> 16) & 0xFF
    green = (rgb_value >> 8) & 0xFF
    blue = rgb_value & 0xFF
    return f"{red};{green};{blue}"


def parse_rgb_payload(raw: str = "") -> Optional[int]:
    """
    Parse RGB payload from WirenBoard MQTT format, semicolon separated:
      - 'R;G;B' (0..255)

    Args:
        raw: RGB string in WirenBoard format "R;G;B"
        
    Returns:
        int: converted RGB value as integer 0..16777215, or None if failed
        
    Example:
        >>> parse_rgb_payload("255;128;0")
        16744448  # 0xFF8000
        >>> parse_rgb_payload("invalid")
        None
    """
    payload_str = raw.strip()
    try:
        if not payload_str:
            return None
        if ";" not in payload_str:
            logger.warning("RGB payload must be in WirenBoard format 'R;G;B': %r", raw)
            return None

        # WirenBoard format: R;G;B
        rgb_components = payload_str.split(";")
        if len(rgb_components) != 3:
            logger.warning("RGB payload must have exactly 3 components: %r", raw)
            return None

        red, green, blue = [int(component.strip()) for component in rgb_components]
        return _rgb_to_int(red, green, blue)

    except Exception as parse_error:
        logger.warning("Failed to parse RGB payload %r: %r", raw, parse_error)
        return None


def _range_cap(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, CAP_RANGE, instance, _to_float(value)
    )


def _color_setting(device_id: str, instance: Optional[str], value: Any) -> None:
    """
    Normilize instances to Yandex format:
      - rgb → instance='color', value=int(RGB)
      - temperature_k → instance='temperature_k', value=int
    """
    if instance in ("rgb"):
        if isinstance(value, int):
            rgb_int = value
        else:
            rgb_int = parse_rgb_payload(str(value))
            if rgb_int is None:
                logger.warning("Failed to parse RGB value: %r", value)
                return None
        send_state_to_server(
            device_id, CAP_COLOR_SETTING, "rgb", rgb_int
        )
    elif instance == "temperature_k":
        send_state_to_server(
            device_id,
            CAP_COLOR_SETTING,
            "temperature_k",
            int(float(value)),
        )
    else:
        logger.debug("Unsupported instance %r — dropped", instance)
    # HSV and color scene add there in future


def _not_implemented(cap_type: str) -> Callable[..., None]:
    def _stub(*_a, **_kw) -> None:
        raise NotImplementedError("Handler for %r is not implemented yet" % cap_type)

    return _stub


# Yandex types handler table for select send logic from MQTT to Yandex
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
    PROP_EVENT: _not_implemented("prop.event"),
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


def send_state_to_server(
    device_id: str,
    block_type: str,
    instance: Optional[str],
    value: Any,
) -> None:
    """
    Pushes **any** single capability / property
    state update to the cloud proxy via Socket.IO
    """
    logger.debug(
        "[YANDEX] Sending state: device=%r, type=%r, instance=%r, value=%r",
        device_id,
        block_type,
        instance,
        value,
    )

    is_prop = block_type.startswith("devices.properties")

    # Normalize known value types
    if block_type.endswith("on_off"):
        value = _to_bool(value)
    elif block_type.endswith("float"):
        value = _to_float(value)

    payload = {
        "ts": int(time.time()),
        "payload": {
            # user_id will be added by the proxy
            "devices": [
                {
                    "id": device_id,
                    "status": "online",
                    **_build_state_block(
                        block_type, instance, value, is_property=is_prop
                    ),
                }
            ]
        },
    }
    try:
        if _emit_callback:
            _emit_callback("device_state", payload)
        else:
            logger.warning("[YANDEX] Emit callback not set, dropping event")
    except Exception:
        logging.exception("Error when processing MQTT message")


def send_to_yandex_state(
    device_id: str, cap_type: str, instance: Optional[str], value: Any
) -> None:
    """
    Unified sender to Yandex used by registry
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
        handler(device_id, instance, value)
    except NotImplementedError as err:
        # Explicit and loud: capability exists in table but lacks implementation
        logger.error("[YANDEX] %r", err)
    except Exception as exc:
        logger.exception("[YANDEX] Handler error for %r: %r", cap_type, exc)