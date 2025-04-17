#!/usr/bin/env python3

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1]


setup(
    name="wb-mqtt-alice",
    version=get_version(),
    description="Yandex Alice to MQTT integration for Wiren Board controllers",
    license="MIT",
    author="Vitalii Gaponov",
    author_email="vitalii.gaponov@wirenboard.com",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    url="https://github.com/wirenboard/wb-alice-client",
    # Files installed by debian/install file
    # Files installed by debian/install file
    # Requirements installed from debian/control file
)
