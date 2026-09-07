"""
  File with all constants in project
"""


class EventType:
    """Yandex Smart Home event types"""

    BUTTON = "button"
    OPEN = "open"
    WATER_LEAK = "water_leak"
    MOTION = "motion"


class OpenEventValue:
    """Values for open/close events"""

    OPENED = "opened"
    CLOSED = "closed"


class WaterLeakEventValue:
    """Values for water leak detection events"""

    DRY = "dry"
    LEAK = "leak"


class MotionEventValue:
    """Values for motion detection events"""

    DETECTED = "detected"
    NOT_DETECTED = "not_detected"


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
# Yandex action_result error codes
# https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/response-codes
ERR_DEVICE_UNREACHABLE = "DEVICE_UNREACHABLE"  # Current device state is not available
ERR_INVALID_ACTION = "INVALID_ACTION"  # Operation is not supported by the capability
ERR_INVALID_VALUE = "INVALID_VALUE"  # Value cannot be applied to the capability
ERR_INTERNAL_ERROR = "INTERNAL_ERROR"  # Anything that does not fit the codes above
# Configuration file paths
SHORT_SN_PATH = "/var/lib/wirenboard/short_sn.conf"
DEVICE_PATH = "/etc/wb-mqtt-alice-devices.conf"
CONFIG_EVENTS_RATE_PATH = "/usr/share/wb-mqtt-alice/wb-mqtt-alice-event-rates.json"
SERVER_CONFIG_PATH = "/usr/share/wb-mqtt-alice/wb-mqtt-alice-server.conf"
CLIENT_CONFIG_PATH = "/etc/wb-mqtt-alice-client.conf"
# Board info paths
BOARD_REVISION_PATH = "/proc/device-tree/wirenboard/board-revision"
BOARD_MODEL_PATH = "/proc/device-tree/model"
# For logging to syslog/journald with name "wb-mqtt-alice-cli"
WB_MQTT_ALICE_CLI_LOGGER_NAME = "wb-mqtt-alice-cli"
