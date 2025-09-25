"""
  File with all constants in project
"""

# Capability  type constants
CAP_ON_OFF = "devices.capabilities.on_off"
CAP_COLOR_SETTING = "devices.capabilities.color_setting"
CAP_RANGE = "devices.capabilities.range"
CAP_MODE = "devices.capabilities.mode"
CAP_TOGGLE = "devices.capabilities.toggle"
CAP_VIDEO_STREAM = "devices.capabilities.video_stream"
# Capability property type constants
PROP_FLOAT = "devices.properties.float"
PROP_EVENT = "devices.properties.event"
# Configuration file paths
SHORT_SN_PATH = "/var/lib/wirenboard/short_sn.conf"
CONFIG_PATH = "/usr/lib/wb-mqtt-alice/wb-mqtt-alice-client.conf"
DEVICE_PATH = "/etc/wb-mqtt-alice-devices.conf"
CONFIG_EVENTS_RATE = "/usr/lib/wb-mqtt-alice/wb-mqtt-alice-event-rates.json"
