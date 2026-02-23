#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wb_alice_config.models import PointSpec
from wb_alice_config.downstream.models import RawDownstreamMessage, DownstreamWrite
from wb_alice_config.downstream.mqtt_wb_conv.codec import MqttWbConvCodec
from wb_alice_config.downstream.mqtt_wb_conv.adapter import MqttWbConvAdapter, MqttConnectionConfig


def _point_onoff() -> PointSpec:
    return PointSpec(
        device_id="dev1",
        point="capabilities:devices.capabilities.on_off:on",
        y_type="devices.capabilities.on_off",
        instance="on",
        parameters={"instance": "on"},
    )


def _point_float_temp() -> PointSpec:
    return PointSpec(
        device_id="dev1",
        point="properties:devices.properties.float:temperature",
        y_type="devices.properties.float",
        instance="temperature",
        parameters={"instance": "temperature", "unit": "unit.temperature.celsius"},
    )


@pytest.mark.parametrize(
    "payload,expected",
    [
        (b"1", True),
        (b"0", False),
        (b"true", True),
        (b"OFF", False),
    ],
)
def test_codec_decode_on_off(payload: bytes, expected: bool):
    codec = MqttWbConvCodec()
    p = _point_onoff()
    v = codec.decode(p, "value", payload)
    assert v == expected


def test_codec_encode_on_off():
    codec = MqttWbConvCodec()
    p = _point_onoff()
    assert codec.encode(p, "value", True) == b"1"
    assert codec.encode(p, "value", False) == b"0"

def test_codec_decode_float():
    codec = MqttWbConvCodec()
    p = _point_float_temp()
    v = codec.decode(p, "value", b"23.5")
    assert v == 23.5


def test_codec_encode_float():
    codec = MqttWbConvCodec()
    p = _point_float_temp()
    out = codec.encode(p, "value", 23.5)
    assert out in (b"23.5", b"23.500000")  # depends on float->str


def test_adapter_subscribe_and_emit_raw_message():
    paho_client = MagicMock()

    cfg = MqttConnectionConfig(host="localhost", port=1883)
    adapter = MqttWbConvAdapter(cfg=cfg, subscriptions=["wb/dev1/relay"], client=paho_client)

    received = []

    def on_raw(msg: RawDownstreamMessage) -> None:
        received.append(msg)

    adapter.start(on_raw)

    # Subscribe should happen immediately in start()
    paho_client.subscribe.assert_any_call("wb/dev1/relay")
    paho_client.loop_start.assert_called_once()

    # Simulate inbound message from paho
    fake_msg = MagicMock()
    fake_msg.topic = "wb/dev1/relay"
    fake_msg.payload = b"1"
    fake_msg.qos = 0
    fake_msg.retain = False

    adapter._on_message(paho_client, None, fake_msg)

    assert len(received) == 1
    assert received[0].downstream_name == "mqtt_wb_conv"
    assert received[0].address == "wb/dev1/relay"
    assert received[0].payload == b"1"
    assert isinstance(received[0].meta, dict)
    assert "qos" in received[0].meta
    assert "retain" in received[0].meta


def test_adapter_write_publish():
    paho_client = MagicMock()
    cfg = MqttConnectionConfig(host="localhost", port=1883, qos=1, retain=True)
    adapter = MqttWbConvAdapter(cfg=cfg, subscriptions=[], client=paho_client)

    adapter.write(DownstreamWrite(downstream_name="mqtt_wb_conv", address="wb/dev1/relay", payload=b"1"))
    paho_client.publish.assert_called_once_with("wb/dev1/relay", payload=b"1", qos=1, retain=True)
