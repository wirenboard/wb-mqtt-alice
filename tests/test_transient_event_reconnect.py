"""Transient events must not be replayed on a new connection; ordinary state survives."""
import asyncio
import time
from types import SimpleNamespace
import pytest
from wb.mqtt_alice.client.device_registry import DeviceRegistry
from wb.mqtt_alice.client import wb_alice_device_state_sender as module

@pytest.mark.parametrize(
    'case',
    ['rate_limited', 'batch_timer', 'while_stopped', 'ordinary_rate_buffer', 'fresh_event_after_restart'],
)
async def test_transient_events_do_not_cross_connection(case, monkeypatch):
    monkeypatch.setattr(module, 'BATCH_WINDOW_NORMAL', 0.01)
    monkeypatch.setattr(module, 'BATCH_WINDOW_FAST', 0.01)
    event_topic = '/devices/fixture/controls/click'
    state_topic = '/devices/fixture/controls/switch'
    r = object.__new__(DeviceRegistry)
    r.devices = {
        'fixture': {
            'properties': [{
                'type': 'devices.properties.event',
                'parameters': {'instance': 'button', 'value': 'value.click'},
            }],
            'capabilities': [{
                'type': 'devices.capabilities.on_off',
                'parameters': {'instance': 'on'},
            }],
        },
    }
    rate = SimpleNamespace(time_rate=1000, rule='last_value')
    r.topic2info = {
        event_topic: ('fixture', 'properties', 0, rate),
        state_topic: ('fixture', 'capabilities', 0, rate),
    }

    def convert(device, kind, instance, value):
        field = 'properties' if kind.startswith('devices.properties') else 'capabilities'
        return {'id': device, field: [{'type': kind, 'state': {'instance': instance, 'value': value}}]}
    r._send_to_yandex = convert
    emitted = []
    monkeypatch.setattr(module, 'emit_batched_states', emitted.extend)

    def events():
        return [p for b in emitted for p in b.get('properties', []) if p['type'] == 'devices.properties.event']
    sender = module.AliceDeviceStateSender(r)
    if case == 'rate_limited':
        await sender.start()
        sender.last_send_times[event_topic] = time.time()
        await sender.add_message(event_topic, '1')
        assert len(sender.topic_buffers[event_topic]) == 1
        await sender.stop()
        assert not sender.topic_buffers.get(event_topic)
        assert event_topic not in sender.last_send_times
        await sender.start()
        await asyncio.sleep(0.15)
        await sender.stop()
        assert not events()
    elif case == 'batch_timer':
        await sender.start()
        sender._batch_store.merge_block(r.convert_mqtt_to_yandex_block(event_topic, '1'))
        sender._batch_store.merge_block(r.convert_mqtt_to_yandex_block(state_topic, '1'))
        sender._schedule_flush(time_sensitive=True)
        handle = sender._flush_handle
        await sender.stop()
        await asyncio.sleep(0.03)
        assert handle.cancelled() and sender._flush_handle is None
        assert not events()
        assert [c['state']['value'] for b in emitted for c in b.get('capabilities', [])] == [True]
    elif case == 'while_stopped':
        await sender.add_message(event_topic, '1')
        await sender.add_message(state_topic, '1')
        assert not sender.topic_buffers.get(event_topic)
        assert len(sender.topic_buffers[state_topic]) == 1
        sender.last_send_times[state_topic] = 0
        await sender.start()
        await asyncio.sleep(0.15)
        await sender.stop()
        assert not events()
        assert any((b.get('capabilities') for b in emitted))
    elif case == 'ordinary_rate_buffer':
        await sender.start()
        sender.last_send_times[state_topic] = time.time()
        await sender.add_message(state_topic, '1')
        await sender.stop()
        assert len(sender.topic_buffers[state_topic]) == 1
        assert state_topic in sender.last_send_times
        sender.last_send_times[state_topic] = 0
        await sender.start()
        await asyncio.sleep(0.15)
        await sender.stop()
        assert not events()
        assert any((b.get('capabilities') for b in emitted))
    elif case == 'fresh_event_after_restart':
        await sender.start()
        await sender.stop()
        await sender.start()
        sender.last_send_times[event_topic] = 0
        await sender.add_message(event_topic, '1')
        await asyncio.sleep(0.15)
        await sender.stop()
        assert len(events()) == 1
    else:
        raise AssertionError(case)
