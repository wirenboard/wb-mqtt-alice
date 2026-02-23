#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Downstream adapters and codecs

Downstream = "southbound" side of the client (WB controller environment)

Each downstream implementation is placed into its own package under:

  wb_alice_config.downstream.<name>/

Example:
  - mqtt_wb_conv  (MQTT using Wiren Board conventions)
  - file_poll     (read values from JSON file; useful for tests)
  - http_poll     (fetch JSON from HTTP; useful for quick experiments)
"""
