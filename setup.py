#!/usr/bin/env python3

from setuptools import setup

from wb.mqtt_alice.common.version import get_version_from_changelog

setup(
    name="wb-mqtt-alice",
    version=get_version_from_changelog(),
    author="Vitalii Gaponov",
    author_email="vitalii.gaponov@wirenboard.com",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Yandex Alice to MQTT integration for Wiren Board controllers",
    license="MIT",
    url="https://github.com/wirenboard/wb-mqtt-alice",
    packages=[
        # "wb"                   # Explicitly excluded: provided by base package
        # "wb.mqtt_alice"        # Explicitly excluded: avoid namespace collision
        "wb.mqtt_alice.common",  # Shared files via several modules
        "wb.mqtt_alice.cli",
        "wb.mqtt_alice.config",  # Backend for WEBUI
        "wb.mqtt_alice.client",
    ],
    # Other files (scripts, configs and etc):
    # - Installed by debian/install file
    # Requirements:
    # - Installed from debian/control file
)
