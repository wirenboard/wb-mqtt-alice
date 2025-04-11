"""
mqtt_topic.py - MQTT Topic handling utility for WirenBoard devices

This module provides a class for parsing and manipulating MQTT topics 
in both formats used by WirenBoard devices:
- short format (device/control)
- full format (/devices/device/controls/control)

Typical usage:
    from mqtt_topic import MQTTTopic
    
    topic_one = MQTTTopic("/devices/wb-mr6cv3_127/controls/K1")
    print(topic_one.device)  # wb-mr6cv3_127
    print(topic_one.short)   # wb-mr6cv3_127/K1
    print(topic_one.full)    # /devices/wb-mr6cv3_127/controls/K1
    
    topic_two = MQTTTopic("wb-mr6cv3_127/K1")
    print(topic_two.device)  # wb-mr6cv3_127
    print(topic_two.short)   # wb-mr6cv3_127/K1
    print(topic_two.full)    # /devices/wb-mr6cv3_127/controls/K1
"""


class MQTTTopic:
    """Class for handling MQTT topics in WirenBoard format
    
    This class parses both short format (device/control) and full format
    (/devices/device/controls/control) MQTT topics and provides properties
    to access their components and convert between formats
    
    Attributes:
        device (str): The device name extracted from the topic
        control (str): The control name extracted from the topic
        is_valid (bool): Whether the topic was successfully parsed
        original (str): The original topic string
        short (str): The topic in short format: device/control
        full (str): The topic in full format: /devices/device/controls/control
    
    Example:
        >>> topic = MQTTTopic("/devices/wb-mr6cv3_127/controls/K1")
        >>> print(topic.device)
        wb-mr6cv3_127
        >>> print(topic.short)
        wb-mr6cv3_127/K1
    """
    
    def __init__(self, topic_str):
        """Initialize an MQTTTopic instance.
        
        Args:
            topic_str (str): The MQTT topic string in either short or full format
        """
        self._original = topic_str
        self._device = None
        self._control = None
        self._is_valid = False
        
        self._parse_topic(topic_str)
    
    def _parse_topic(self, topic_str):
        """Parse the topic string into device and control components
        
        This method handles both short format (device/control) and
        full format (/devices/device/controls/control).
        
        Args:
            topic_str (str): The topic string to parse.
        """
        if "/" in topic_str and not topic_str.startswith("/devices/"):
            # Short format "device/control"
            try:
                device, control = topic_str.split("/", 1)
                self._device = device
                self._control = control
                self._is_valid = True
            except ValueError:
                # Invalid format
                self._is_valid = False
        else:
            # Full format "/devices/device/controls/control"
            parts = topic_str.strip("/").split("/")
            if len(parts) >= 4 and parts[0] == "devices" and parts[2] == "controls":
                self._device = parts[1]
                self._control = parts[3]
                self._is_valid = True
    
    @property
    def original(self):
        """str: The original topic string provided during initialization"""
        return self._original
    
    @property
    def device(self):
        """str: The device name extracted from the topic, or None if invalid"""
        return self._device
    
    @property
    def control(self):
        """str: The control name extracted from the topic, or None if invalid"""
        return self._control
    
    @property
    def is_valid(self):
        """bool: Whether the topic was successfully parsed into device and control"""
        return self._is_valid
    
    @property
    def short(self):
        """str: The topic in short format (device/control)
        
        Returns the original string if the topic is invalid
        """
        if not self._is_valid:
            return self._original
        return f"{self._device}/{self._control}"
    
    @property
    def full(self):
        """str: The topic in full format (/devices/device/controls/control)
        
        Returns the original string if the topic is invalid
        """
        if not self._is_valid:
            return self._original
        return f"/devices/{self._device}/controls/{self._control}"
    
    def __str__(self):
        """Return the string representation of the topic
        
        Returns:
            str: The short format if valid, otherwise an error message
        """
        if not self._is_valid:
            return f"Invalid MQTT Topic: {self._original}"
        return self.short
    
    def __repr__(self):
        """Return the developer string representation of the topic
        
        Returns:
            str: A string that could be used to recreate this object
        """
        if not self._is_valid:
            return f"MQTTTopic('{self._original}') [invalid]"
        return f"MQTTTopic(device='{self._device}', control='{self._control}')"
