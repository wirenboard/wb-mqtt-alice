#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import importlib.util
import logging
import sys
from fetch_url import fetch_url
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

def unlink_controller():
    try:
        server_address = load_client_config()["server_address"]
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)
        if get_status_controller():
            logger.info("Controller status: linked, proceeding to unlink...")
        else:
            logger.info("Controller status: unlinked, not required.")
            print("Unlink action result: not needed")
            return
        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )
        if response["status_code"] == 404:
            if response["data"]["detail"] == "Controller not found":
                logger.info("Controller not found on the server.")
                return
        if response["status_code"] >= 400:
            raise Exception("Unlink request failed with status code %r", response)
        logger.info("Controller unlinked Successfully")
        print("Unlink action result: successful")
    except Exception as e:
        logger.error("Failed to unlink: %r,", e, exc_info=True)

def get_status_controller():
    try:
        server_address = load_client_config()["server_address"]
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)
        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )
        if response["data"] and "registration_url" in response["data"]:
            # Controller is not registered - provide registration link
            logger.debug("Controller not registered")
            print("Current link status: not linked")
            return False
        elif response["data"]["detail"]:
            # Controller is registered
            logger.debug("Controller registered")
            print("Current link status: linked")
            return True
    except Exception as e:
        logger.error("Failed to get status: %r,", e, exc_info=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="Args parsinfg for wb-mqtt-alice-client-cli")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    unlink_parser = subparsers.add_parser('unlink-controller', help='Unlink controller from yandex account')
    unlink_parser = subparsers.add_parser('get-link-status', help='Get link status controller')
    args = parser.parse_args()
    if args.command == "unlink-controller":
        logger.info("Unlinking controller from yandex account...")
        unlink_controller()
        sys.exit(0)
    if args.command == "get-link-status":
        logger.info("Get link status controller...")
        get_status_controller()
        sys.exit(0)
if __name__ == "__main__":
    main()