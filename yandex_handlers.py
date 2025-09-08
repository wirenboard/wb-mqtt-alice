#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Yandex Smart Home Handlers for Wiren Board Alice Integration
Handles type conversions and state management for Yandex Smart Home API
"""
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

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
            logger.debug("[to_bool] Unknown string %s → False", raw_state)
            return False
    else:
        logger.debug("[to_bool] Unknown value type %s → False", type(raw_state))
        return False


def _to_float(raw: Any) -> float:
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.debug("[to_float] Cannot convert %s to float → 0.0", raw)
        return 0.0


def _on_off(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, "devices.capabilities.on_off", instance, _to_bool(value)
    )


def _float_prop(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, "devices.properties.float", instance, _to_float(value)
    )


def _to_int(raw: Any) -> int:
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        logger.debug("[to_int] Cannot convert %s to int: return 0", raw)
        return 0


def _rgb_to_int(r: int, g: int, b: int) -> int:
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return (r << 16) | (g << 8) | b


def int_to_rgb_wb_format(val: int) -> str:
    v = int(val) & 0xFFFFFF
    r = (v >> 16) & 0xFF
    g = (v >> 8) & 0xFF
    b = v & 0xFF
    return f"{r};{g};{b}"


def parse_rgb_payload(raw: str) -> Optional[int]:
    """
    Parse RGB payload from various MQTT formats:
      - JSON {"r":..,"g":..,"b":..}
      - int number 0..16777215
      - string '#RRGGBB' or '0xRRGGBB'
      - 'R,G,B' (0..255) - comma separated
      - 'R;G;B' (0..255) - semicolon separated (WirenBoard format)
      - decimal
    Return converted:
      - int 0..16777215
    """
    s = (raw or "").strip()
    try:
        if not s:
            return None
        if s.startswith("{"):
            obj = json.loads(s)
            return _rgb_to_int(obj.get("r", 0), obj.get("g", 0), obj.get("b", 0))
        if s.startswith("#"):
            return int(s[1:], 16)
        if s.lower().startswith("0x"):
            return int(s, 16)
        if ";" in s:
            # WirenBoard format: R;G;B
            r, g, b = [int(x.strip()) for x in s.split(";", 2)]
            return _rgb_to_int(r, g, b)
        if "," in s:
            # Comma format: R,G,B
            r, g, b = [int(x) for x in s.split(",", 2)]
            return _rgb_to_int(r, g, b)
        else:
            # dec
            return int(float(s))
    except Exception as e:
        logger.warning("parse RGB failed for %r: %s", raw, e)
        return None


def _range_cap(device_id: str, instance: Optional[str], value: Any) -> None:
    send_state_to_server(
        device_id, "devices.capabilities.range", instance, _to_float(value)
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
            rgb_int = _parse_rgb_payload(str(value))
            if rgb_int is None:
                logger.warning("Failed to parse RGB value: %s", value)
                return
        send_state_to_server(
            device_id, "devices.capabilities.color_setting", "rgb", rgb_int
        )
    elif instance == "temperature_k":
        send_state_to_server(
            device_id,
            "devices.capabilities.color_setting",
            "temperature_k",
            _to_int(value),
        )
    else:
        logger.debug("Unsupported instance %s — dropped", instance)
    # HSV and color scene add there in future


def _not_implemented(cap_type: str) -> Callable[..., None]:
    def _stub(*_a, **_kw) -> None:
        raise NotImplementedError(f"Handler for '{cap_type}' is not implemented yet")

    return _stub


# Yandex types handler table for select send logic from MQTT to Yandex
_HANDLERS: Dict[str, Callable[[str, Optional[str], Any], None]] = {
    # Capabilities
    "devices.capabilities.on_off": _on_off,
    "devices.capabilities.color_setting": _color_setting,
    "devices.capabilities.video_stream": _not_implemented("cap.video_stream"),
    "devices.capabilities.mode": _not_implemented("cap.mode"),
    "devices.capabilities.range": _range_cap,
    "devices.capabilities.toggle": _not_implemented("cap.toggle"),
    # Properties
    "devices.properties.float": _float_prop,
    "devices.properties.event": _not_implemented("prop.event"),
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
        "[YANDEX] Sending state: device=%s, type=%s, instance=%s, value=%s",
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
            "[YANDEX] Unknown capability/property '%s'. Supported: %s",
            cap_type,
            ", ".join(_HANDLERS.keys()),
        )
        return

    try:
        handler(device_id, instance, value)
    except NotImplementedError as err:
        # Explicit and loud: capability exists in table but lacks implementation
        logger.error("[YANDEX] %s", err)
    except Exception as exc:
        logger.exception("[YANDEX] Handler error for '%s': %s", cap_type, exc)
