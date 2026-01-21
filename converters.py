#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Converters for Wiren Board Alice Integration
Handles type conversions between WirenBoard and Yandex Smart Home formats
"""

import logging
from typing import Any, Optional

from constants import (EventType, MotionEventValue, OpenEventValue,
                       WaterLeakEventValue)

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


def convert_mqtt_event_value(event_type:str, event_type_value:str, value:str, event_single_topic:bool=False)->Optional[str]:
    """
    Transform raw value from MQTT topics to Yandex Smart Home event text values
    
    Converts MQTT topic values to appropriate Yandex event values based on event type.
    Handles both multi-topic events (separate topics for each state) and single-topic 
    events (one topic with boolean values).
    
    Args:
        event_type: Type of event (e.g., "button", "open", "water_leak", "motion")
        event_type_value: Expected event value from Yandex format (e.g., "opened", "closed", 
                         "dry", "leak", "detected", "not_detected")
        value: Raw value from MQTT topic (e.g., "1", "0", "true", "false", "on", "off")
        event_single_topic: If True, handles single-topic events with value inversion.
                           If False (default), handles multi-topic events
    
    Returns:
        Optional[str]: Yandex event value string if event is triggered, None otherwise
    
    Examples:
        Multi-topic events (default):
        >>> convert_mqtt_event_value("open", "opened", "1")
        "opened"
        >>> convert_mqtt_event_value("open", "closed", "0")
        None
        
        Button events:
        >>> convert_mqtt_event_value("button", "click", "1")
        "click"
        >>> convert_mqtt_event_value("button", "click", "0")
        None
        
        Single-topic events:
        >>> convert_mqtt_event_value("open", "opened", "1", event_single_topic=True)
        "opened"
        >>> convert_mqtt_event_value("open", "opened", "0", event_single_topic=True)
        "closed"
        >>> convert_mqtt_event_value("water_leak", "dry", "1", event_single_topic=True)
        "dry"
        >>> convert_mqtt_event_value("water_leak", "dry", "0", event_single_topic=True)
        "leak"
    """
    if event_type == EventType.BUTTON:
        # Event Button -  trigger one of topic"
        value = event_type_value if value.lower() not in ["0", "false", "off"] else None
        return value

    if event_single_topic:
        if event_type == EventType.OPEN:
            if event_type_value == OpenEventValue.OPENED:
                value = event_type_value if convert_to_bool(value) else OpenEventValue.CLOSED
            elif event_type_value.lower() == OpenEventValue.CLOSED:
                value = event_type_value if convert_to_bool(value) else OpenEventValue.OPENED
        elif event_type == EventType.WATER_LEAK:
            if event_type_value == WaterLeakEventValue.DRY:
                value = event_type_value if convert_to_bool(value) else WaterLeakEventValue.LEAK
            elif event_type_value == WaterLeakEventValue.LEAK:
                value = event_type_value if convert_to_bool(value) else WaterLeakEventValue.DRY
        elif event_type == EventType.MOTION:
            if event_type_value == MotionEventValue.DETECTED:
                value = event_type_value if convert_to_bool(value) else MotionEventValue.NOT_DETECTED
            elif event_type_value == MotionEventValue.NOT_DETECTED:
                value = event_type_value if convert_to_bool(value) else MotionEventValue.DETECTED
    else:
        value = event_type_value if convert_to_bool(value) else None

    return value
