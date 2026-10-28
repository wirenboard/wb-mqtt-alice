#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tests for saving the devices config from the configurator.

wb_move keeps the devices config in /mnt/data and leaves a symlink to it in /etc.
A save that renamed its temporary file onto that symlink replaced the link with a
regular file, and the next boot put the link back to the stale copy in /mnt/data,
so every change made on the Alice page was lost.
"""

from wb.mqtt_alice.common.models import Room
from wb.mqtt_alice.config import main


def test_save_writes_through_the_wb_move_symlink(tmp_path, monkeypatch):
    data_conf = tmp_path / "mnt/data/etc/wb-mqtt-alice-devices.conf"
    data_conf.parent.mkdir(parents=True)
    etc_conf = tmp_path / "etc/wb-mqtt-alice-devices.conf"
    etc_conf.parent.mkdir()
    etc_conf.symlink_to(data_conf)
    monkeypatch.setattr(main, "DEVICES_CONFIG_PATH", etc_conf)

    config = main.load_config()
    config.rooms["kitchen"] = Room(name="Кухня")
    main.save_devices_config(config)

    assert etc_conf.is_symlink()
    assert etc_conf.resolve() == data_conf.resolve()
    assert "Кухня" in data_conf.read_text(encoding="utf-8")
