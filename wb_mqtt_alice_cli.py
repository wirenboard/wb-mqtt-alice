#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import importlib.util
import logging
import sys

from fetch_url import fetch_url
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config

logger = logging.getLogger(__name__)


def unlink_controller():
    logger.info("Controller unlinked start...")
    try:
        server_address = load_client_config()["server_address"]
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)
        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )
        if response["status_code"] >= 400:
            raise Exception("Unlink request failed with status code %r", response)
        logger.info("Controller unlinked Success...")
    except Exception as e:
        logger.error("Failed to unlink: %r,", e, exc_info=True)


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