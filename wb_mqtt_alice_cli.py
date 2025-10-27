#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import logging
import sys
from http import HTTPStatus
from fetch_url import fetch_url
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config
from constants import WB_MQTT_ALICE_CLI_LOGGER_NAME

GET_LINK_STATUS_PREF = "Get link status result:"
UNLINK_ACTION_RESULT_PREF = "Unlink action result:"
CURRENT_LINK_STATUS_PREF = "Current link status:"

logger = logging.getLogger(WB_MQTT_ALICE_CLI_LOGGER_NAME)
logger.setLevel(logging.INFO)

def unlink_controller():
    """Unlink controller from yandex account."""
    logger.info("Unlinking controller from yandex account...")
    print("Unlinking controller from yandex account...")
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("%s failed (no server address)", UNLINK_ACTION_RESULT_PREF)
            print("%s failed (no server address)" % UNLINK_ACTION_RESULT_PREF)
            return

        if not get_link_status():
            logger.info("%s not required", UNLINK_ACTION_RESULT_PREF)
            print("%s not required" % UNLINK_ACTION_RESULT_PREF)
            return

        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )

        # validate response shape
        if not isinstance(response, dict):
            logger.error("%s failed %r", UNLINK_ACTION_RESULT_PREF, response)
            print("%s failed" % UNLINK_ACTION_RESULT_PREF)
            return

        status = int(response.get("status_code") or 0)
        data = response.get("data") or {}

        if status == HTTPStatus.NOT_FOUND and isinstance(data, dict) and data.get("detail") == "Controller not found":
            logger.info("%s successful", UNLINK_ACTION_RESULT_PREF)
            print("%s successful" % UNLINK_ACTION_RESULT_PREF)
            return
        if status > HTTPStatus.BAD_REQUEST:
            logger.error("%s unlink request failed %r", UNLINK_ACTION_RESULT_PREF, response)
            print("%s unlink request failed" % UNLINK_ACTION_RESULT_PREF)
            return
        logger.info("%s successful", UNLINK_ACTION_RESULT_PREF)
        print("%s successful" % UNLINK_ACTION_RESULT_PREF)
    except Exception as e:
        logger.error("%s Failed %r", UNLINK_ACTION_RESULT_PREF, e)
        print("%s Failed" % UNLINK_ACTION_RESULT_PREF)


def get_link_status():
    """Returns False if controller is unlinked, otherwise returns True for continue unlinking."""
    logger.info("%s...", GET_LINK_STATUS_PREF)
    print("%s..." % GET_LINK_STATUS_PREF)
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("%s failed (no server address)", GET_LINK_STATUS_PREF)
            print("%s failed (no server address)" % GET_LINK_STATUS_PREF)
            return False

        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )

        if not isinstance(response, dict):
            logger.error("%s failed %r", GET_LINK_STATUS_PREF, response)
            print("%s failed" % GET_LINK_STATUS_PREF)
            return True

        status = int(response.get("status_code") or 0)
        data = response.get("data") or {}
        if status > HTTPStatus.BAD_REQUEST:
            logger.error("%s failed (server error %r)", GET_LINK_STATUS_PREF, response)
            print("%s failed (server error)" % GET_LINK_STATUS_PREF)
            return True

        # Controller is not linked if registration_url present or data is empty
        if not data or ("registration_url" in data):
            logger.info("%s not linked", CURRENT_LINK_STATUS_PREF)
            print("%s not linked" % CURRENT_LINK_STATUS_PREF)
            return False

        # Controller linked if detail exists and is truthy
        if isinstance(data, dict) and data.get("detail"):
            logger.info("%s linked", CURRENT_LINK_STATUS_PREF)
            print("%s linked" % CURRENT_LINK_STATUS_PREF)
            return True

        # fallback
        logger.error("%s unknown error %r", CURRENT_LINK_STATUS_PREF, data)
        print("%s unknown error" % CURRENT_LINK_STATUS_PREF)
        return True
    except Exception as e:
        logger.error("%s Failed %r", CURRENT_LINK_STATUS_PREF, e)
        print("%s Failed" % CURRENT_LINK_STATUS_PREF)
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