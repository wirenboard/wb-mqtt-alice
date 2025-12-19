#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Converters for Wiren Board Alice Integration
Handles type conversions between WirenBoard and Yandex Smart Home formats
"""

import logging
from typing import Any, Optional


logger = logging.getLogger(__name__)


def convert_to_bool(raw_state: Any) -> bool:
    """
    Convert value to bool according to Yandex on_off rules

    Args:
        raw_state: Value to convert (bool, int, float, str)

    Returns:
        Boolean value

    Examples:
        >>> convert_to_bool("1")
        True
        >>> convert_to_bool("off")
        False
        >>> convert_to_bool(0)
        False
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
            logger.debug("Unknown string %r, set False", raw_state)
            return False
    else:
        logger.debug("Unknown value type %s, set False", type(raw_state).__name__)
        return False


def convert_to_float(raw: Any) -> float:
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.debug("Cannot convert %r to float, set 0.0", raw)
        return 0.0


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


def convert_rgb_int_to_wb(val: int) -> str:
    """
    Convert RGB value from Yandex integer format to WirenBoard 
    MQTT format (semicolon-separated)
    
    Extracts RGB components from a 24-bit integer and formats them 
    as semicolon-separated string for WirenBoard MQTT

    Args:
        val: RGB integer value (0-16777215)

    Returns:
        str: RGB components in WirenBoard format "R;G;B"

    Example:
        >>> convert_rgb_int_to_wb(16744448)
        "255;128;0"  # 0xFF8000 convertes to "255;128;0"
        >>> convert_rgb_int_to_wb(0)
        "0;0;0"
    """
    rgb_value = int(val) & 0xFFFFFF
    red = (rgb_value >> 16) & 0xFF
    green = (rgb_value >> 8) & 0xFF
    blue = rgb_value & 0xFF
    return f"{red};{green};{blue}"


def convert_rgb_wb_to_int(raw: str = "") -> Optional[int]:
    """
    Convert RGB  value from WirenBoard MQTT format (semicolon-separated)
    to Yandex integer format

    Args:
        string: RGB in WirenBoard format "R;G;B" - for example "255;128;0"

    Returns:
        int: converted RGB value as integer 0..16777215, or None if failed

    Example:
        >>> convert_rgb_wb_to_int("255;128;0")
        16744448  # 0xFF8000
        >>> convert_rgb_wb_to_int("invalid")
        None
        >>> convert_rgb_wb_to_int("")
        None
    """
    payload_str = raw.strip()
    try:
        if not payload_str:
            return None
        if ";" not in payload_str:
            logger.warning("RGB must be in WirenBoard format 'R;G;B': %r", raw)
            return None

        # WirenBoard format: R;G;B
        rgb_components = payload_str.split(";")
        if len(rgb_components) != 3:
            logger.warning("RGB must have exactly 3 components: %r", raw)
            return None

        red, green, blue = [int(component.strip()) for component in rgb_components]
        return _rgb_to_int(red, green, blue)

    except Exception as parse_error:
        logger.warning("Failed to convert WB RGB %r: %r", raw, parse_error)
        return None


def convert_temp_percent_to_kelvin(percent: float, min_k: int, max_k: int) -> int:
    """
    Convert WirenBoard temperature percentage (0-100) to Yandex Kelvin format
    Result is rounded to nearest 100K to avoid echo issues when converting back

    Args:
        percent: Temperature value in percentage (0-100)
        min_k: Minimum temperature in Kelvin (typically 2700K)
        max_k: Maximum temperature in Kelvin (typically 6500K)

    Returns:
        int: Temperature in Kelvin, rounded to nearest 100K
    
    Examples:
        >>> convert_temp_percent_to_kelvin(0, 2700, 6500)
        2700
        >>> convert_temp_percent_to_kelvin(47.4, 2700, 6500)
        4500  # 4501K rounded to 4500K
        >>> convert_temp_percent_to_kelvin(100, 2700, 6500)
        6500
    """
    percent = max(0.0, min(100.0, float(percent)))
    kelvin_raw = min_k + (max_k - min_k) * (percent / 100.0)
    kelvin_rounded = int(round(kelvin_raw / 100.0) * 100)
    logger.debug(
        "Converted temp: %r%% → %rK → rounded %rK (range: %r-%rK)",
        percent, kelvin_raw, kelvin_rounded, min_k, max_k
    )
    return kelvin_rounded


def convert_temp_kelvin_to_percent(kelvin: int, min_k: int, max_k: int) -> float:
    """
    Convert Yandex Kelvin temperature to WirenBoard percentage (0-100)
    Input is expected to be pre-rounded to nearest 100K by convert_temp_percent_to_kelvin

    Args:
        kelvin: Temperature in Kelvin
        min_k: Minimum temperature in Kelvin (typically 2700K)
        max_k: Maximum temperature in Kelvin (typically 6500K)
    
    Returns:
        float: Temperature in percentage (0-100)
    
    Examples:
        >>> convert_temp_kelvin_to_percent(2700, 2700, 6500)
        0.0
        >>> convert_temp_kelvin_to_percent(4500, 2700, 6500)
        47.4
        >>> convert_temp_kelvin_to_percent(6500, 2700, 6500)
        100.0
    """
    kelvin = max(min_k, min(max_k, int(kelvin)))
    if max_k == min_k:
        return 0.0
    percent = ((kelvin - min_k) / (max_k - min_k)) * 100.0
    return round(percent, 1)


def convert_mqtt_event_value(event_type:str, event_type_value:str, value:str, single_event_value:bool=False)->Optional[str]:
    """ Transform raw value from MQTT topics to Yandex events text values """
    # Each event has a dedicated topic.
    # Event Open: value : 'Opened' -> Topic: 'opened', values - 1, true
    # Event Open: value : 'Closed' -> Topic: 'closed', values - 1, true

    if event_type == "button":
        # Event Button -  trigger one of topic"
        value = event_type_value if value.lower() not in ["0", "false", "off"] else None
        return value

    # If event has a single topic.
    # Event Open: value : 'Opened' -> Topic: 'opened'
    # If values in: "1", "True"  -> 'opened'
    # If values in: "0", "False" -> 'closed'
    if single_event_value:
        if event_type == "open":
            if event_type_value == "opened":
                value = event_type_value if convert_to_bool(value) else "closed"
            elif event_type_value.lower() == "closed":
                value = event_type_value if convert_to_bool(value) else "opened"
        elif event_type == "water_leak":
            if event_type_value == "dry":
                value = event_type_value if convert_to_bool(value) else "leak"
            elif event_type_value == "leak":
                value = event_type_value if convert_to_bool(value) else "dry"
        elif event_type == "motion":
            if event_type_value == "detected":
                value = event_type_value if convert_to_bool(value) else "not_detected"
            elif event_type_value == "not_detected":
                value = event_type_value if convert_to_bool(value) else "detected"
    else:
        value = event_type_value if convert_to_bool(value) else None

    return value
