# timer in seconds (maybe float ?)
from __future__ import annotations

DEFAULT_EVENT_RATE = 1
DEFAULT_EVENT_RULE = "last_value"  # default rule for events


class AliceDeviceEventRate:
    """
    Represents an event rate
    time_rate - like min timeout between messages
    rule - last_value, average_value, etc
    """

    time_rate: float | int = DEFAULT_EVENT_RATE
    rule: str = DEFAULT_EVENT_RULE

    def __init__(self, values: dict = None) -> None:
        if values is None:
            values = {}
        self.time_rate = values.get("rate_timer", DEFAULT_EVENT_RATE)
        self.rule = values.get("rate_rule", DEFAULT_EVENT_RULE)
