import asyncio
import logging
import os
import time
import threading
from collections import defaultdict
from os import getenv
from asyncio import AbstractEventLoop, TimerHandle
from typing import Any, Dict, List, Optional

from .device_registry import DeviceRegistry
from .yandex_handlers import emit_batched_states

logger = logging.getLogger(__name__)

PROP_EVENT_TYPE = "devices.properties.event"

MAX_BUFFER_SIZE = int(getenv("MAX_BUFFER_SIZE", "10"))

# Batch flush windows (seconds)
# NORMAL: default window for regular state updates (temperature, brightness, etc.)
# FAST: shortened window for time-sensitive types (events, etc.)
#       50ms is enough to batch simultaneous events, but fast enough for UX
BATCH_WINDOW_NORMAL = float(getenv("BATCH_WINDOW_NORMAL", "1.0"))
BATCH_WINDOW_FAST = float(getenv("BATCH_WINDOW_FAST", "0.05"))

# Types that use fast delivery window (BATCH_WINDOW_FAST instead of BATCH_WINDOW_NORMAL)
# When a block of this type passes Stage 1 rate limiter,
# the batch flush timer is set/shortened to BATCH_WINDOW_FAST
IMMEDIATE_FLUSH_TYPES: frozenset = frozenset({
    "devices.properties.event",  # Events MUST be send fast
    # Can add new types on this place
})

class BatchDeviceStore:
    """
    Deduplicated storage for device state blocks awaiting batch flush

    Thread safety:
        All public methods are protected by threading.Lock
        Safe to call from any context (sync, async, threaded)
    
    Blocks are added and merged by device_id, capabilities and properties
    are deduplicated by (type, instance) - only latest value is kept

    Methods:
        merge_block(block) - add or update a block
        drain() - return merged list and clear storage
        is_empty   - check if storage has data

    Internal structure (self._devices):
        {
            "60ba5cbd-...-d7d7": {
                "status": "online",
                "capabilities": {
                    ("devices.capabilities.on_off", "on"):
                        {"type": "devices.capabilities.on_off",
                         "state": {"instance": "on", "value": True}},
                    ("devices.capabilities.range", "brightness"):
                        {"type": "devices.capabilities.range",
                         "state": {"instance": "brightness", "value": 75}},
                },
                "properties": {
                    ("devices.properties.float", "temperature"):
                        {"type": "devices.properties.float",
                         "state": {"instance": "temperature", "value": 22.5}},
                    ("devices.properties.event", "open"):
                        {"type": "devices.properties.event",
                         "state": {"instance": "open", "value": "opened"}},
                },
            },
            "abc123-...-e2e3": {
                "status": "online",
                "capabilities": {},
                "properties": {
                    ("devices.properties.float", "temperature"):
                        {"type": "devices.properties.float",
                         "state": {"instance": "temperature", "value": -3.2}},
                },
            },
        }
    """

    def __init__(self) -> None:
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """
        Number of devices in storage
        """
        with self._lock:
            return len(self._devices)

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return not self._devices

    def merge_block(self, block: Dict[str, Any]) -> None:
        """
        Merge block into storage with deduplication by (device_id, type, instance)
        If same (type, instance) already exists for this device, value is overwritten
        """
        with self._lock:
            device_id = block["id"]
            if device_id not in self._devices:
                self._devices[device_id] = {
                    "status": block.get("status", "online"),
                    "capabilities": {},
                    "properties": {},
                }
            device = self._devices[device_id]
            for cap in block.get("capabilities", []):
                key = (cap["type"], cap["state"]["instance"])
                device["capabilities"][key] = cap
            for prop in block.get("properties", []):
                key = (prop["type"], prop["state"]["instance"])
                device["properties"][key] = prop

    def drain(self) -> List[Dict[str, Any]]:
        """
        Return all accumulated devices as list and clear storage

        After this call, is_empty is True
        Uses reference swap: new writes go to a fresh dict,
        result is built from the old one — safe if merge_block()
        is called during _build_result() processing
        """
        with self._lock:
            # swap: merge_block() will now writes to new dict
            snapshot = self._devices
            self._devices = {}
        return self._build_result(snapshot)

    @staticmethod
    def _build_result(devices: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for device_id, device in devices.items():
            entry: Dict[str, Any] = {"id": device_id, "status": device["status"]}
            caps = list(device["capabilities"].values())
            props = list(device["properties"].values())
            if caps:
                entry["capabilities"] = caps
            if props:
                entry["properties"] = props
            result.append(entry)
        return result

class AliceDeviceStateSender:
    """
    Two-stage sender for device state updates to Yandex Smart Home

    Stage 1 — Per-topic rate limiter (existing logic):
      Each topic has its own time_rate. Messages arriving faster than
      time_rate are buffered and aggregated (last_value / average_value)
      Only when time_rate has elapsed does the topic "pass" to stage 2

    Stage 2 — Batch accumulator:
      Converted Yandex blocks from Stage 1 accumulate in BatchDeviceStore
      Flushed after BATCH_WINDOW_NORMAL (1s) or BATCH_WINDOW_FAST (50ms)
      for time-sensitive types. Timer is managed by call_later.
      Blocks are deduplicated by (device_id, type, instance)
    """

    def __init__(self, device_registry: DeviceRegistry):
        self.topic_buffers = defaultdict(list)
        self.last_send_times = {}  # dict of last times by topic
        self._topic_buffers_lock = asyncio.Lock()
        self.running = False
        self.device_registry = device_registry

        self._batch_store = BatchDeviceStore()
        self._flush_handle: Optional[TimerHandle] = None
        self._current_window: float = 0.0
        self._loop: Optional[AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None


    async def start(self):
        """
        Start the background send loop
        """
        logger.info(
            "Starting alice device state sender (normal=%.2fs, fast=%.3fs)",
            BATCH_WINDOW_NORMAL,
            BATCH_WINDOW_FAST,
        )
        self.running = True
        self._task = asyncio.create_task(self.send_to_yandex_loop())

    async def stop(self):
        """
        Stop the background send loop
        Wakes loop so it can do a final flush before exiting
        """
        self.running = False
        logger.info("Stopping alice device state sender")
        if self._task is not None:
            try:
                await self._task
            except Exception:
                logger.error("Error in send loop task during shutdown", exc_info=True)

    async def add_message(self, topic_str: str, payload_str: str):
        """
        Add MQTT message to per-topic buffer
        If message is an event property, triggers immediate batch flush
        """
        logger.debug("add message %s %s", topic_str, payload_str)

        async with self._topic_buffers_lock:
            info = self.device_registry.topic2info.get(topic_str)
            if info is None:
                logger.debug("Unknown topic %r, ignoring", topic_str)
                return

            device_id, cap_prop, idx, event_rate = info
            message = {
                "topic": topic_str,
                "cap_prop": cap_prop,
                "payload": payload_str,
                "origin_rate": event_rate,
            }
            logger.debug("add message: message %s", message)
            self.topic_buffers[topic_str].append(message)
            # Limit buffer size (sliding window)
            if len(self.topic_buffers[topic_str]) > MAX_BUFFER_SIZE:
                self.topic_buffers[topic_str] = self.topic_buffers[topic_str][-MAX_BUFFER_SIZE:]


    def _needs_immediate_flush(self, topic: str) -> bool:
        """
        Check if topic type is in IMMEDIATE_FLUSH_TYPES
        """
        info = self.device_registry.topic2info.get(topic)
        if info is None:
            return False
        device_id, cap_prop, idx, _ = info
        try:
            blk_type = self.device_registry.devices[device_id][cap_prop][idx].get("type", "").lower()
        except (KeyError, IndexError):
            return False
        return blk_type in IMMEDIATE_FLUSH_TYPES

    def _schedule_flush(self, time_sensitive: bool = False) -> None:
        """
        Schedule or shorten batch flush timer

        Rules:
          - First block in empty batch: schedule after FAST or NORMAL window
          - Time-sensitive block during NORMAL window: shorten to FAST
          - Time-sensitive block during FAST window: do nothing (already fast)
          - Normal block during any window: do nothing (keep current timer)
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        window = BATCH_WINDOW_FAST if time_sensitive else BATCH_WINDOW_NORMAL

        if self._flush_handle is None:
            # No timer yet — schedule new one
            self._current_window = window
            self._flush_handle = self._loop.call_later(window, self._do_flush)
            logger.debug("Flush scheduled in %.3fs (time_sensitive=%s)", window, time_sensitive)

        elif time_sensitive and self._current_window > BATCH_WINDOW_FAST:
            # Shorten: cancel current timer and reschedule with fast window
            self._flush_handle.cancel()
            self._current_window = BATCH_WINDOW_FAST
            self._flush_handle = self._loop.call_later(BATCH_WINDOW_FAST, self._do_flush)
            logger.debug("Flush rescheduled to %.3fs (time_sensitive block arrived)", BATCH_WINDOW_FAST)


    def _do_flush(self) -> None:
        """
        Timer callback: reset timer state and flush.
        Called by loop.call_later — must be sync.
        """
        self._flush_handle = None
        self._current_window = 0.0
        self._try_flush()


    def _try_flush(self) -> bool:
        """
        Flush batch store if not empty
        Called from _do_flush (timer) and shutdown

        Returns:
            True if data was flushed, False if store was empty
        """
        if self._batch_store.is_empty:
            return False
        merged = self._batch_store.drain()
        logger.debug("Batch flush: done - flushed %d device(s)", len(merged))
        emit_batched_states(merged)
        return True

    def _cancel_flush_timer(self) -> None:
        """
        Cancel scheduled flush if any
        """
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
            self._current_window = 0.0

    async def send_to_yandex_loop(self):
        """
        Background loop for send batched messages to yandex device in 2 stages:
          Stage 1: per-topic rate limiter
          Stage 2: (batch flush) is handled by _schedule_flush / call_later
        """
        while self.running:
            try:
                current_time = time.time()
                # --- Stage 1: Per-topic rate limiter ---
                # Collect data under lock, convert outside
                topics_to_convert: List[tuple] = []
                async with self._topic_buffers_lock:
                    for topic, messages in self.topic_buffers.items():
                        if not messages:
                            logger.debug("not message info for topic=%s", topic)
                            continue
                        last_send_time = self.last_send_times.get(topic, 0)
                        rate = messages[-1]["origin_rate"].time_rate
                        
                        # check time-rate
                        if current_time - last_send_time <= rate:
                            continue  # Rate limit not passed yet

                        # Rate limit passed - do aggregate and convert
                        aggregated = self.modify_messages_by_rule(
                            rule=messages[-1]["origin_rate"].rule,
                            messages=messages,
                        )
                        aggregated_payload = str(aggregated["payload"])
                        topics_to_convert.append((topic, aggregated_payload))
                        messages.clear()
                        self.last_send_times[topic] = current_time


                # Only pass topics whose time_rate elapsed, result goes to _batch_store
                # If All ok - send message to Yandex
                for topic, aggregated_payload in topics_to_convert:
                    if os.getenv("__DEBUG__"):
                        logger.info("[DEBUG] rate-passed: topic=%s, aggregated_payload=%s", topic, aggregated_payload)
                    else:
                        block = self.device_registry.convert_mqtt_to_yandex_block(
                            topic=topic, raw=aggregated_payload
                        )
                        if block is not None:
                            self._batch_store.merge_block(block)

                            # Schedule batch flush with appropriate window
                            time_sensitive = self._needs_immediate_flush(topic)
                            self._schedule_flush(time_sensitive=time_sensitive)
                            if time_sensitive:
                                logger.debug(
                                    "Triggered fast batch flush, because got update time-sensitive block from topic: %r",
                                    topic
                                )

            except Exception as e:
                logger.error("Error in send loop: %s", e, exc_info=True)
            await asyncio.sleep(0.1)

        # Shutdown: cancel pending timer and flush remaining
        self._cancel_flush_timer()
        self._try_flush()
        logger.info("Send loop finished")


    @staticmethod
    def modify_messages_by_rule(rule: str, messages: List[dict]) -> dict:
        """
        Apply aggregation rule to buffered messages for one topic
        Returns single message dict with aggregated payload
        """
        if not rule:
            return messages[-1]
        if rule.lower() == "last_value":
            # last message value return
            return messages[-1]
        if rule.lower() == "average_value":
            # calc average value in messages["payload"]
            logger.debug(messages)
            # be carful! for float types correctly !
            all_values_list = []
            for items in messages:
                try:
                    all_values_list.append(float(items["payload"]))
                except ValueError:
                    logger.error("not a number: %s", items["payload"])
            message = messages[-1]
            if len(all_values_list) > 0:
                message["payload"] = sum(all_values_list) / len(all_values_list)
            return message
        # process by default = last value
        return messages[-1]
