#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import logging
import sys
from enum import IntEnum
from http import HTTPStatus
from fetch_url import fetch_url
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config
from constants import WB_MQTT_ALICE_CLI_LOGGER_NAME

# Exit codes for get_link_status / CLI
class ExitCode(IntEnum):
    SUCCESS = 0        # generic success
    ERROR = 1          # generic error / failure
    NOT_LINKED = 2     # controller is not linked
    LINKED = 3         # controller is linked
    UNKNOWN = 4        # unknown / unexpected response

GET_LINK_STATUS_PREF = "Get link status result:"
UNLINK_ACTION_RESULT_PREF = "Unlink action result:"
CURRENT_LINK_STATUS_PREF = "Current link status:"

logger = logging.getLogger(WB_MQTT_ALICE_CLI_LOGGER_NAME)
logger.setLevel(logging.INFO)

def unlink_controller():
    """Unlink controller from yandex account."""
    logger.info("Unlinking controller from yandex account...")
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("%s failed (no server address)", UNLINK_ACTION_RESULT_PREF)
            print("%s failed (no server address)" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.ERROR

        status_code = get_link_status()
        if status_code == ExitCode.NOT_LINKED:
            logger.info("%s not required", UNLINK_ACTION_RESULT_PREF)
            print("%s not required" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.SUCCESS

        if status_code != ExitCode.LINKED:
            logger.info("%s skipped", UNLINK_ACTION_RESULT_PREF)
            print("%s skipped" % UNLINK_ACTION_RESULT_PREF)
            return  ExitCode.ERROR

        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )

        # validate response shape
        if not isinstance(response, dict):
            logger.error("%s failed %r", UNLINK_ACTION_RESULT_PREF, response)
            print("%s failed" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.ERROR

        status_code = int(response.get("status_code") or 0)
        data = response.get("data") or {}

        if status_code == HTTPStatus.NOT_FOUND and isinstance(data, dict) and data.get("detail") == "Controller not found":
            logger.info("%s successful", UNLINK_ACTION_RESULT_PREF)
            print("%s successful" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.SUCCESS
        if not status_code or status_code > HTTPStatus.BAD_REQUEST:
            logger.error("%s unlink request failed %r", UNLINK_ACTION_RESULT_PREF, response)
            print("%s unlink request failed" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.ERROR
        logger.info("%s successful", UNLINK_ACTION_RESULT_PREF)
        print("%s successful" % UNLINK_ACTION_RESULT_PREF)
    except Exception as e:
        logger.error("%s Failed %r", UNLINK_ACTION_RESULT_PREF, e)
        print("%s Failed" % UNLINK_ACTION_RESULT_PREF)
        return ExitCode.ERROR


def get_link_status():
    """
    Check controller link status and return an ExitCode.

    Returns:
        ExitCode.NOT_LINKED  -> controller is not linked
        ExitCode.LINKED      -> controller is linked
        ExitCode.ERROR       -> fatal error (config / network / parsing)
        ExitCode.UNKNOWN     -> unexpected response structure
    """
    logger.info("%s...", GET_LINK_STATUS_PREF)
    try:
        cfg = load_client_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("%s failed (no server address)", CURRENT_LINK_STATUS_PREF)
            print("%s failed (no server address)" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.ERROR

        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )

        if not isinstance(response, dict):
            logger.error("%s failed (response is not dict: %r)", CURRENT_LINK_STATUS_PREF, response)
            print("%s failed (bad type of response)" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.ERROR

        status_code = int(response.get("status_code") or 0)
        if not status_code or status_code > HTTPStatus.BAD_REQUEST:
            # there is no HTTPStatus for status code 0, so treat it as an error
            print("%s failed (server returned error %r)" % (CURRENT_LINK_STATUS_PREF, status_code))
            logger.error("%s failed (server returned error %r)", CURRENT_LINK_STATUS_PREF, status_code)
            return ExitCode.ERROR

        data = response.get("data") or {}

        # Controller is not linked if registration_url present or data is empty
        if not data or ("registration_url" in data):
            logger.info("%s not linked", CURRENT_LINK_STATUS_PREF)
            print("%s not linked" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.NOT_LINKED

        # Controller linked if detail exists and is truthy
        if isinstance(data, dict) and data.get("detail"):
            logger.info("%s linked", CURRENT_LINK_STATUS_PREF)
            print("%s linked" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.LINKED

        # fallback
        logger.error("%s unknown error %r", CURRENT_LINK_STATUS_PREF, data)
        print("%s unknown error" % CURRENT_LINK_STATUS_PREF)
        return ExitCode.UNKNOWN
    except Exception as e:
        logger.error("%s Failed %r", CURRENT_LINK_STATUS_PREF, e)
        print("%s Failed with unknown error" % CURRENT_LINK_STATUS_PREF)
        return ExitCode.ERROR


def main():
    parser = argparse.ArgumentParser(description="Args parsing for wb-mqtt-alice-client-cli")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    subparsers.add_parser('unlink-controller', help='Unlink controller from yandex account')
    subparsers.add_parser('get-link-status', help='Get link status controller')
    args = parser.parse_args()
    if args.command == "unlink-controller":
        return int(unlink_controller())
    if args.command == "get-link-status":
        return int(get_link_status())
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())