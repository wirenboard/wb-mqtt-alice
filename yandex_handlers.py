#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Yandex Smart Home Handlers for Wiren Board Alice Integration
Handles type conversions and state management for Yandex Smart Home API
"""

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


def _not_implemented(cap_type: str) -> Callable[..., None]:
    def _stub(*_a, **_kw) -> None:
        raise NotImplementedError(f"Handler for '{cap_type}' is not implemented yet")

    return _stub


# Yandex types handler table for select send logic from MQTT to Yandex
_HANDLERS: Dict[str, Callable[[str, Optional[str], Any], None]] = {
    # Capabilities
    "devices.capabilities.on_off": _on_off,
    "devices.capabilities.color_setting": _not_implemented("cap.color_setting"),
    "devices.capabilities.video_stream": _not_implemented("cap.video_stream"),
    "devices.capabilities.mode": _not_implemented("cap.mode"),
    "devices.capabilities.range": _not_implemented("cap.range"),
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
    is_prop = block_type.startswith("devices.properties")

    # normalise known value types
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
        _emit_async("device_state", payload)
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
