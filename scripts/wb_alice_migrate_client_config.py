#!/usr/bin/env python3
"""
This script is used to migrate the wb-mqtt-alice client configuration.
It updates the `client_enabled` flag in the configuration file that was moved from
`/usr/lib/wb-mqtt-alice/wb-mqtt-alice-client.conf` to
`/etc/wb-mqtt-alice-client.conf`, preserving all other fields.
This migration is applied during upgrades of wb-mqtt-alice to version 0.6.0 and later.
"""
import json
import sys

from wb.mqtt_alice.common.models import ClientConfig

# Default configuration with all required fields
DEFAULTS = ClientConfig().dict()


def migrate_config(config_file: str, client_enabled: bool) -> None:
    """
    Update client_enabled in config file while preserving other fields.

    Args:
        config_file: Path to configuration file
        client_enabled: Value to set for client_enabled flag
    """
    with open(config_file, "r", encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("Client configuration root must be an object")

    # Merge with defaults (preserve existing values, add missing fields)
    for key, default_value in DEFAULTS.items():
        if key not in config:
            config[key] = default_value

    # Update client_enabled field with migrated value
    config["client_enabled"] = client_enabled

    # Write back with all fields
    with open(config_file, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <config_file> <True|False>", file=sys.stderr)
        sys.exit(1)

    config_file = sys.argv[1]
    client_enabled = sys.argv[2].lower() in ("true", "1", "yes")

    try:
        migrate_config(config_file, client_enabled)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        print(f"Client configuration was not migrated and was left unchanged: {error}", file=sys.stderr)
