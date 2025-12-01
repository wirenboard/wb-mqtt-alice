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

# Define handler functions for each event type
def handle_motion(raw_value:str) -> str:
    """
    Convert a raw motion sensor value to a detection status.

    Args:
        raw_value (str | int | float):
            Raw input from the sensor (commonly a string or numeric type). The
            function attempts to convert this value to an int for threshold
            comparison.

    Returns:
        str:"detected" if the integer-converted value is greater than 50,
        otherwise "not_detected".

    Examples:
        >>> handle_motion("75")
        "detected"
        >>> handle_motion("10")
        "not_detected"
        >>> handle_motion("abc")
        "not_detected"
    """
    try:
        value = int(raw_value)
        if value > 50:
            logger.debug("Motion detected with value: %r", raw_value)
            return "detected"
        logger.debug("No motion detected with value: %r", raw_value)
    except ValueError:
        logger.warning("Invalid motion value: %r", raw_value)
    return "not_detected"

def handle_battery_level(raw_value:str) -> str:
    """
    Classify a raw battery-level reading into a simple status string.

    This function attempts to convert the given raw_value to a float and classifies
    the battery level as either "low" or "normal". A numeric value strictly less
    than 20.0 is considered "low"; any other numeric value is considered "normal".
    If the input cannot be converted to a float, the function logs a warning and
    returns "normal".

    Args:
        raw_value (str | int | float): The raw battery level value to interpret.
            Typical inputs are strings received from external sources, but numeric
            values are also accepted.

    Returns:
        str: "low" when the parsed battery level is below 20.0, otherwise "normal".

    Examples:
        >>> handle_battery_level("15")
        "low"
        >>> handle_battery_level("85")
        "normal"
        >>> handle_battery_level("abc")
        "normal"
    """
    try:
        value = float(raw_value)
        if value < 20.0:
            logger.debug("Low battery level: %r", raw_value)
            return "low"
        logger.debug("Battery level normal: %r", raw_value)
    except ValueError:
        logger.warning("Invalid battery value: %r", raw_value)
    return "normal"

def handle_food_level(raw_value:str) -> str:
    """
    Convert a raw food level input into a categorical string.

    Args:
        raw_value (str | int | float):
            The raw input representing a food level. This is typically a string or numeric
            value that will be converted to float for evaluation.

    Returns:
        str:
            - "empty"  : when the numeric value is less than 1.0
            - "low"    : when the numeric value is greater than or equal to 1.0 and less than 20.0
            - "normal" : when the numeric value is greater than or equal to 20.0, or when the
                        input cannot be parsed as a float (parsing errors are treated as "normal")
        >>> handle_food_level("0")
        "empty"
        >>> handle_food_level("15")
        "low"
        >>> handle_food_level("50")
        "normal"
        >>> handle_food_level("abc")
        "normal"
    """
    try:
        value = float(raw_value)
        if value < 1.0:
            logger.debug("Empty food level: %r", raw_value)
            return "empty"
        if 1.0 <= value < 20.0:
            logger.debug("Low food level: %r", raw_value)
            return "low"
        logger.debug("Foodlevel normal: %r", raw_value)
    except ValueError:
        logger.warning("Invalid food level value: %r", raw_value)
    return "normal"

def handle_water_level(raw_value:str) -> str:
    """
    Convert a raw water level input into a categorical string.

    Args:
        raw_value (str | int | float):
            The raw input representing a water level. This is typically a string or numeric
            value that will be converted to float for evaluation.

    Returns:
        str:
            - "empty"  : when the numeric value is less than 1.0
            - "low"    : when the numeric value is greater than or equal to 1.0 and less than 20.0
            - "normal" : when the numeric value is greater than or equal to 20.0, or when the
                         input cannot be parsed as a float (parsing errors are treated as "normal")
        >>> handle_water_level("0")
        "empty"
        >>> handle_water_level("15")
        "low"
        >>> handle_water_level("50")
        "normal"
        >>> handle_water_level("abc")
        "normal"
    """
    try:
        value = float(raw_value)
        if value < 1.0:
            logger.debug("Empty water level: %r", raw_value)
            return "empty"
        if 1.0 <= value < 20.0:
            logger.debug("Low water level: %r", raw_value)
            return "low"
        logger.debug("Water level normal: %r", raw_value)
    except ValueError:
        logger.warning("Invalid water level value: %r", raw_value)
    return "normal"
