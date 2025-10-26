#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import logging
import sys
from fetch_url import fetch_url
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config

logger = logging.getLogger("wb-mqtt-alice-cli")
logger.setLevel(logging.INFO)


def unlink_controller():
    logger.info("Unlinking controller from yandex account...")
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("No server_address in config")
            print("Unlink action result: failed (no server address)")
            return

        if get_link_status():
            logger.info("Controller status: linked, proceeding to unlink...")
        else:
            logger.info("Controller status: unlinked, not required.")
            print("Unlink action result: not needed")
            return

        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )

        # validate response shape
        if not isinstance(response, dict):
            logger.error("Invalid response from server: %r", response)
            print("Unlink action result: failed (invalid response)")
            return

        status = response.get("status_code") or 0
        data = response.get("data") or {}

        logger.debug("Unlink response status=%s data=%r", status, data)

        if status == 404 and isinstance(data, dict) and data.get("detail") == "Controller not found":
            logger.info("Unlink action result: successful.")
            print("Unlink action result: successful")
            return
        if status >= 400:
            logger.error("Unlink request failed: %r", response)
            print("Unlink action result: failed (server error)")
            return

        logger.info("Controller unlinked successfully")
        print("Unlink action result: successful")
    except Exception:
        logger.exception("Failed to unlink")
        print("Unlink action result: failed (exception)")


def get_link_status():
    logger.info("Get link status controller...")
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("No server_address in config")
            print("Get link status result: failed (no server address)")
            return False

        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )

        if not isinstance(response, dict):
            logger.error("Invalid response from server: %r", response)
            print("Get link status result: failed (invalid response)")
            return False

        status = response.get("status_code") or 0
        data = response.get("data") or {}

        logger.debug("Get-link response status=%s data=%r", status, data)

        if status >= 400:
            logger.error("Registration request failed: %r", response)
            print("Get link status result: failed (server error)")
            return False

        # Controller is not linked if registration_url present or data is empty
        if not data or ("registration_url" in data):
            print("Current link status: not linked")
            logger.info("Controller not linked")
            return False

        # Controller linked if detail exists and is truthy
        if isinstance(data, dict) and data.get("detail"):
            print("Current link status: linked")
            logger.debug("Controller linked")
            return True

        # fallback
        logger.debug("Unexpected registration response data: %r", data)
        print("Current link status: unknown")
        return False
    except Exception:
        logger.exception("Failed to get link status")
        print("Failed to get link status")
        return False


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