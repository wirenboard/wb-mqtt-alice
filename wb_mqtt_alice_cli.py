#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import logging
import sys
from fetch_url import fetch_url
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config

logger = logging.getLogger("wb-mqtt-alice-cli")
logger.setLevel(logging.INFO)

def print_message(message: str, level:int, args: tuple = ()):
    """Utility function to print messages with parameters."""
    if level == logging.DEBUG:
        logger.debug(message, *args)
        return
    if level == logging.INFO:
        logger.info(message, *args)
    elif level == logging.WARNING:
        logger.warning(message, *args)
    elif level == logging.ERROR:
        logger.error(message, *args)
        # cut message for user console output
        args = {}
        message = message.replace("%r", "")
    print(message % args if args else message)


def unlink_controller():
    """Unlink controller from yandex account."""
    print_message("Unlinking controller from yandex account...", level=logging.INFO)
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            print_message("Unlink action result: failed (no server address)", level=logging.ERROR)
            return

        if not get_link_status():
            print_message("Unlink action result: not required", level=logging.INFO)
            return

        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )

        # validate response shape
        if not isinstance(response, dict):
            print_message("Unlink action result: failed %r", level=logging.ERROR, args=(response,))
            return

        status = response.get("status_code") or 0
        data = response.get("data") or {}

        logger.debug("Unlink response status=%s data=%r", status, data)

        if status == 404 and isinstance(data, dict) and data.get("detail") == "Controller not found":
            print_message("Unlink action result: successful", level=logging.INFO)
            return
        if status >= 400:
            print_message("Unlink request failed %r", level=logging.ERROR, args=(response,))
            return
        print_message("Unlink action result: successful", level=logging.INFO)
    except Exception as e:
        print_message("Failed to unlink controller %r", level=logging.ERROR, args=(e,))


def get_link_status():
    """Returns False if controller is unlinked, otherwise returns True for continue unlinking."""
    print_message("Get link status...", level=logging.INFO)
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            print_message("Get link status result: failed (no server address)", level=logging.ERROR)
            return False

        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )

        if not isinstance(response, dict):
            print_message("Get link status result: failed %r", level=logging.ERROR, args=(response,))
            return True

        status = response.get("status_code") or 0
        data = response.get("data") or {}
        if status > 400:
            print_message("Get link status result: failed (server error %r)", level=logging.ERROR, args=(response,))
            return True

        # Controller is not linked if registration_url present or data is empty
        if not data or ("registration_url" in data):
            print_message("Current link status: not linked", level=logging.INFO)
            return False

        # Controller linked if detail exists and is truthy
        if isinstance(data, dict) and data.get("detail"):
            print_message("Current link status: linked", level=logging.INFO)
            return True

        # fallback
        print_message("Current link status: unknown error %r", level=logging.ERROR, args=(data,))
        return True
    except Exception as e:
        print_message("Failed to get link status %r", level=logging.ERROR, args=(e,))
        return True


def main():
    parser = argparse.ArgumentParser(description="Args parsing for wb-mqtt-alice-client-cli")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    subparsers.add_parser('unlink-controller', help='Unlink controller from yandex account')
    subparsers.add_parser('get-link-status', help='Get link status controller')
    args = parser.parse_args()
    if args.command == "unlink-controller":
        unlink_controller()
        return 0
    if args.command == "get-link-status":
        get_link_status()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())