#!/usr/bin/env python3
"""
Migration script for wb-mqtt-alice client configuration.
Updates client_enabled flag while preserving all other fields.
"""
import json
import sys
from models import ClientConfig

# Default configuration with all required fields
DEFAULTS = ClientConfig().dict()

def migrate_config(config_file: str, client_enabled: bool) -> None:
    """
    Update client_enabled in config file while preserving other fields.
    
    Args:
        config_file: Path to configuration file
        client_enabled: Value to set for client_enabled flag
    """
    # Read existing config (if any)
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    
    # Merge with defaults (preserve existing values, add missing fields)
    for key, default_value in DEFAULTS.items():
        if key not in config:
            config[key] = default_value
    
    # Update client_enabled field with migrated value
    config['client_enabled'] = client_enabled
    
    # Write back with all fields
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <config_file> <True|False>", file=sys.stderr)
        sys.exit(1)
    
    config_file = sys.argv[1]
    client_enabled = sys.argv[2].lower() in ('true', '1', 'yes')
    
    migrate_config(config_file, client_enabled)
