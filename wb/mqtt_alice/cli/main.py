#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import logging
import sys
from enum import IntEnum
from http import HTTPStatus

from wb.mqtt_alice.lib.fetch_url import fetch_url
from wb.mqtt_alice.lib.wb_mqtt_load_config import (
    get_board_revision,
    get_key_id,
    load_server_config
)
from wb.mqtt_alice.lib.constants import WB_MQTT_ALICE_CLI_LOGGER_NAME

# Exit codes for get_link_status / CLI
class ExitCode(IntEnum):
    # Common linux codes (0-9)
    GEN_SUCCESS = 0 # Generic success for any command
    GEN_ERROR = 1  # Unexpected errors (no internet, server not reachable, etc)
    INIT_ERROR = 2  # Initialization errors (Not correct argument and etc)

    # Get status command (10-19)
    STATUS_CHECK_FAILED = 10
    STATUS_LINKED = 11
    STATUS_NOT_LINKED = 12

    # Get registration link command (20-29)
    GET_REG_LINK_FAILED = 20
    GET_REG_LINK_SUCCESS = 21
    GET_REG_LINK_REUSE = 22
    ALREADY_LINKED = 23

    # Unlink command (30-39)
    UNLINK_FAILED = 30
    UNLINK_SUCCESS = 31
    ALREADY_UNLINKED = 32

    # Next sub command add with code 40...

GET_LINK_STATUS_PREF = "Get link status result:"
UNLINK_ACTION_RESULT_PREF = "Unlink action result:"
CURRENT_LINK_STATUS_PREF = "Current link status:"

logger = logging.getLogger(WB_MQTT_ALICE_CLI_LOGGER_NAME)
logger.setLevel(logging.INFO)

def unlink_controller():
    """Unlink controller from yandex account."""
    logger.info("Unlinking controller from yandex account...")
    try:
        cfg = load_server_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("%s failed (no server address)", UNLINK_ACTION_RESULT_PREF)
            print("%s failed (no server address)" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.INIT_ERROR

        exit_code = get_link_status()
        # If controller already not linked, nothing to do
        if exit_code == ExitCode.GEN_ERROR:
            logger.error("%s skipped", UNLINK_ACTION_RESULT_PREF)
            print("%s skipped" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.GEN_ERROR
        if exit_code == ExitCode.STATUS_NOT_LINKED:
            logger.info("%s not required", UNLINK_ACTION_RESULT_PREF)
            print("%s not required" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.GEN_SUCCESS

        # Proceed only if controller is reported as linked
        if exit_code != ExitCode.STATUS_LINKED:
            logger.info("%s skipped", UNLINK_ACTION_RESULT_PREF)
            print("%s skipped" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.UNLINK_FAILED

        response = fetch_url(
            url=f"https://{server_address}/request-unlink",
            key_id=key_id,
        )

        # validate response shape
        if not isinstance(response, dict):
            logger.error("%s failed %r", UNLINK_ACTION_RESULT_PREF, response)
            print("%s failed" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.UNLINK_FAILED

        status_code = int(response.get("status_code") or 0)
        data = response.get("data") or {}
        if status_code == HTTPStatus.NOT_FOUND and isinstance(data, dict) and data.get("detail") == "Controller not found":
            logger.info("%s successful", UNLINK_ACTION_RESULT_PREF)
            print("%s successful" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.ALREADY_UNLINKED
        if status_code == 0:
            logger.error("%s failed (no internet, server not reachable, etc)", UNLINK_ACTION_RESULT_PREF)
            print("%s failed (no internet, server not reachable, etc)" % UNLINK_ACTION_RESULT_PREF)
            return ExitCode.GEN_ERROR
        if status_code > HTTPStatus.BAD_REQUEST:
            logger.error("%s unlink request failed %r", UNLINK_ACTION_RESULT_PREF, response)
            print("%s unlink request failed (server error %s)" % (UNLINK_ACTION_RESULT_PREF, status_code))
            return ExitCode.UNLINK_FAILED
        logger.info("%s successful", UNLINK_ACTION_RESULT_PREF)
        print("%s successful" % UNLINK_ACTION_RESULT_PREF)
        return ExitCode.GEN_SUCCESS
    except Exception as e:
        logger.error("%s Failed %r", UNLINK_ACTION_RESULT_PREF, e)
        print("%s Failed" % UNLINK_ACTION_RESULT_PREF)
        return ExitCode.UNLINK_FAILED


def get_link_status():
    """
    Check controller link status and return an ExitCode.

    Returns:
        ExitCode
    """
    logger.info("%s...", GET_LINK_STATUS_PREF)
    try:
        cfg = load_server_config()
        server_address = cfg.get("server_address")
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)

        if not server_address:
            logger.error("%s failed (no server address)", CURRENT_LINK_STATUS_PREF)
            print("%s failed (no server address)" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.INIT_ERROR

        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )

        if not isinstance(response, dict):
            logger.error("%s failed (response is not dict: %r)", CURRENT_LINK_STATUS_PREF, response)
            print("%s failed (bad type of response)" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.GET_REG_LINK_FAILED

        status_code = int(response.get("status_code") or 0)
        if status_code == 0:
            logger.error("%s failed (no internet, server not reachable, etc)", CURRENT_LINK_STATUS_PREF)
            print("%s failed (no internet, server not reachable, etc)" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.GEN_ERROR
        if status_code > HTTPStatus.BAD_REQUEST:
            # there is no HTTPStatus for status code 0, so treat it as an error
            print("%s failed (server returned error %r)" % (CURRENT_LINK_STATUS_PREF, status_code))
            logger.error("%s failed (server returned error %r)", CURRENT_LINK_STATUS_PREF, status_code)
            return ExitCode.GET_REG_LINK_FAILED

        data = response.get("data") or {}

        # Controller is not linked if registration_url present or data is empty
        if not data or ("registration_url" in data):
            logger.info("%s not linked", CURRENT_LINK_STATUS_PREF)
            print("%s not linked" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.STATUS_NOT_LINKED

        # Controller linked if detail exists and is truthy
        if isinstance(data, dict) and data.get("detail"):
            logger.info("%s linked", CURRENT_LINK_STATUS_PREF)
            print("%s linked" % CURRENT_LINK_STATUS_PREF)
            return ExitCode.STATUS_LINKED

        # fallback
        logger.error("%s unknown error %r", CURRENT_LINK_STATUS_PREF, data)
        print("%s unknown error" % CURRENT_LINK_STATUS_PREF)
        return ExitCode.STATUS_CHECK_FAILED
    except Exception as e:
        logger.error("%s Failed %r", CURRENT_LINK_STATUS_PREF, e)
        print("%s Failed with unknown error" % CURRENT_LINK_STATUS_PREF)
        return ExitCode.STATUS_CHECK_FAILED


def main():
    parser = argparse.ArgumentParser(
    prog='wb-mqtt-alice',
    description='Manage Yandex Alice integration for Wiren Board controllers',
    usage='wb-mqtt-alice [-h] <command>',  #  need for to hide "..." in end of  string
    add_help=False,
    epilog="""
Example:
  wb-mqtt-alice unlink-controller
"""
    )

    parser.add_argument(
        '-h', '--help',
        action='help',
        help='Show this help message and exit'
    )

    subparsers = parser.add_subparsers(
        dest='command',
        title='Available commands',
        metavar='<command>          ' # 10 Spaces needed for it
    )

    subparsers.add_parser(
        'unlink-controller',
        help='Unlink from Yandex account'
    )
    subparsers.add_parser(
        'get-link-status',
        help='Check link status'
    )
    args = parser.parse_args()
    if args.command == "unlink-controller":
        return int(unlink_controller())
    if args.command == "get-link-status":
        return int(get_link_status())
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
