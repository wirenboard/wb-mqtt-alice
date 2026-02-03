#!/usr/bin/env python3

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1].split("~")[0]


setup(
    name="wb-mqtt-alice",
    version=get_version(),
    author="Vitalii Gaponov",
    author_email="vitalii.gaponov@wirenboard.com",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Yandex Alice to MQTT integration for Wiren Board controllers",
    license="MIT",
    url="https://github.com/wirenboard/wb-mqtt-alice",
    packages=[
        "wb.mqtt_alice.lib",  # Shared files
        "wb.mqtt_alice.cli",
        "wb.mqtt_alice.config",  # Backend for WEBUI
        
        "wb.mqtt_alice.client",
        "wb.mqtt_alice.client.downstream",
        "wb.mqtt_alice.client.downstream.file_poll",
        "wb.mqtt_alice.client.downstream.http_poll",
        "wb.mqtt_alice.client.downstream.mqtt_wb_conv",
        "wb.mqtt_alice.client.upstream",
        "wb.mqtt_alice.client.upstream.wb_proxy_socketio",
    ],
    # Other files (scripts, configs and etc) installed by debian/install file
    # Requirements installed from debian/control file
)
