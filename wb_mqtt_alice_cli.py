#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import logging
import sys
from fetch_url import fetch_url
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config

logger = logging.getLogger("wb-mqtt-alice-cli")


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

        status = response.get("status_code", 0)
        data = response.get("data", {})
        if status == 404 and data.get("detail") == "Controller not found":
            logger.info("Controller not found on the server.")
            print("Unlink action result: controller not found")
            return
        if status >= 400:
            raise Exception(f"Unlink request failed: {response}")

        logger.info("Controller unlinked successfully")
        print("Unlink action result: successful")
    except Exception as e:
        logger.exception("Failed to unlink")


def get_link_status():
    logger.info("Get link status controller...")
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("No server_address in config")
            print("Unlink action result: failed (no server address)")
            return

        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )
        status = response.get("status_code", 0)
        data = response.get("data", {})
        if response["data"] and "registration_url" in response["data"]:
            # Controller is not registered - provide registration link
            print("Current link status: not linked")
            logger.info("Controller not registered")
            return False
        if response["data"]["detail"]:
            # Controller is registered
            print("Current link status: linked")
            logger.debug("Controller registered")
            return True
    except Exception as e:
        print("Failed to get link status")
        logger.error("Failed to get link status: %r,", e, exc_info=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="Args parsinfg for wb-mqtt-alice-client-cli")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    unlink_parser = subparsers.add_parser('unlink-controller', help='Unlink controller from yandex account')
    unlink_parser = subparsers.add_parser('get-link-status', help='Get link status controller')
    args = parser.parse_args()
    if args.command == "unlink-controller":
        unlink_controller()
        sys.exit(0)
    if args.command == "get-link-status":
        get_link_status()
        sys.exit(0)
if __name__ == "__main__":
    main()