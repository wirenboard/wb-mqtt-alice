from __future__ import annotations


class AliceDeviceEventRate:
    """
    Represents an event rate
    time_rate - like min timeout between messages
    rule - last_value, average_value, etc
    """

    time_rate: float | int = 1
    rule: str = "last_value"

    def __init__(self, values: dict = None) -> None:
        if values is None:
            # default values
            values = {"rate_timer": AliceDeviceEventRate.time_rate, "rate_rule": AliceDeviceEventRate.rule}
        self.time_rate = values.get("rate_timer")
        self.rule = values.get("rate_rule")

    def __repr__(self):
        return f"AliceDeviceEventRate({self.time_rate}, {self.rule})"
