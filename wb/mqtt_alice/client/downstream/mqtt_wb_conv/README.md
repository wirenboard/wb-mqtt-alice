# mqtt_wb_conv

Downstream implementation for MQTT with **Wiren Board conventions**.

## What is "WB convention" here?

In this project it means:

1. We read/write values via MQTT topics.
2. Payload format is a *convention*, not a universal MQTT rule.
   The codec currently supports minimal set:
   - devices.capabilities.on_off: accepts 0/1, true/false, ON/OFF (case-insensitive)
   - devices.properties.float: parses float from bytes/str

## Files

- adapter.py: real adapter built on top of paho-mqtt
- codec.py: payload conversion rules (WB convention)
