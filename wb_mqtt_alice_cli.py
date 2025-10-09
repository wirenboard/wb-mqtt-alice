#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import importlib.util
import logging
import sys

from fetch_url import fetch_url

logger = logging.getLogger(__name__)


def unlink_controller():
    logger.info("Controller unlinked successfully.")
    try:
        spec = importlib.util.spec_from_file_location("wb_mqtt_alice_config", "wb-mqtt-alice-config.py")
        wb_mqtt_alice_config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wb_mqtt_alice_config)
        server_address = wb_mqtt_alice_config.load_client_config()["server_address"]
        controller_version = wb_mqtt_alice_config.get_board_revision()
        key_id = wb_mqtt_alice_config.get_key_id(controller_version)
        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )
        logger.info("Unregistration response: %s", response)
    except Exception as e:
        logger.error("Failed to fetch unregistration URL: %r", e)


def main():
    parser = argparse.ArgumentParser(description="Args parsinfg for wb-mqtt-alice-client-cli")
    parser.add_argument(
        "--unlink-controller",
        action="store_const",
        const=True,
        default=False,
        help="Unlink controller from account",
    )
    args, unknown = parser.parse_known_args()
    if args.unlink_controller:
        logger.info("Unlinking controller from account...")
        unlink_controller()
        sys.exit(0)

if __name__ == "__main__":
    main()