import asyncio
import logging
import os
import time
from collections import defaultdict
from os import getenv
from typing import Any, Dict, List, Optional

from .device_registry import DeviceRegistry
from .yandex_handlers import emit_batched_states

PROP_EVENT_TYPE = "devices.properties.event"

# Types that trigger immediate batch flush (bypass BATCH_INTERVAL)
# When a block of this type passes Stage 1 rate limiter,
# accumulated batch is flushed immediately instead of waiting
IMMEDIATE_FLUSH_TYPES: frozenset = frozenset({
    "devices.properties.event",  # Events MUST be send fast
    # Can add new types on this place
})

logger = logging.getLogger(__name__)

MAX_BUFFER_SIZE = int(getenv("MAX_BUFFER_SIZE", "10"))
BATCH_INTERVAL = float(getenv("BATCH_INTERVAL", "1.0"))

class BatchDeviceStore:
    """
    Deduplicated storage for device state blocks awaiting batch flush

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

    def __len__(self) -> int:
        """
        Number of devices in storage
        """
        return len(self._devices)

    @property
    def is_empty(self) -> bool:
        return not self._devices

    def merge_block(self, block: Dict[str, Any]) -> None:
        """
        Merge block into storage with deduplication by (device_id, type, instance)
        If same (type, instance) already exists for this device, value is overwritten
        """
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
        """
        result: List[Dict[str, Any]] = []
        for device_id, device in self._devices.items():
            entry: Dict[str, Any] = {"id": device_id, "status": device["status"]}
            caps = list(device["capabilities"].values())
            props = list(device["properties"].values())
            if caps:
                entry["capabilities"] = caps
            if props:
                entry["properties"] = props
            result.append(entry)
        self._devices.clear()
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
      Flushed every BATCH_INTERVAL seconds or on event property arrival
      Blocks are deduplicated by (device_id, type, instance)
    """

    def __init__(self, device_registry: DeviceRegistry):
        self.topic_buffers = defaultdict(list)
        self.last_send_times = {}  # dict of last times by topic
        self.lock = asyncio.Lock()
        self.running = False
        self.device_registry = device_registry

        self.flush_event = asyncio.Event()
        self._batch_store = BatchDeviceStore()
        self._task: Optional[asyncio.Task] = None


    async def start(self):
        """
        Start the background send loop
        """
        logger.info(
            "Starting alice device state sender (BATCH_INTERVAL=%.1fs)", BATCH_INTERVAL
        )
        self.running = True
        self._task = asyncio.create_task(self.send_to_yandex_loop())

    async def stop(self):
        """
        Stop the background send loop
        Wakes loop so it can do a final flush before exiting
        """
        self.running = False
        self.flush_event.set()
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

        async with self.lock:
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


    async def send_to_yandex_loop(self):
        """
        Background loop for send batched messages to yandex device in 2 stages:
          1. Rate limiter
          2. Batch flush
        """
        last_batch_time = time.time()

        while self.running:
            try:
                current_time = time.time()
                # --- Stage 1: Per-topic rate limiter ---
                # Collect data under lock, convert outside
                topics_to_convert: List[tuple] = []
                async with self.lock:
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

                            # Event property trigger immediate batch flush
                            if self._needs_immediate_flush(topic):
                                logger.debug(
                                    "Triggered immediate batch flush, because got update topic: %r",
                                    topic
                                )
                                self.flush_event.set()

                # --- Stage 2: Batch flush ---
                batch_age = current_time - last_batch_time
                flush_triggered = self.flush_event.is_set()
                should_flush = (
                    not self._batch_store.is_empty
                    and (batch_age >= BATCH_INTERVAL or flush_triggered)
                )
                if should_flush:
                    self.flush_event.clear()
                    logger.debug(
                        "Now will be flushed batch: age=%.2fs, interval=%.1fs, event_trigger=%s, %d block(s)",
                        batch_age,
                        BATCH_INTERVAL,
                        flush_triggered,
                        len(self._batch_store),
                    )
                    merged = self._batch_store.drain()
                    logger.debug(
                        "Batch flush: done - flushed %d device(s)",
                        len(merged),
                    )
                    emit_batched_states(merged)
                    last_batch_time = current_time

            except Exception as e:
                logger.error("Error in send loop: %s", e, exc_info=True)
            await asyncio.sleep(0.1)

        # Final flush on shutdown
        if not self._batch_store.is_empty:
            merged = self._batch_store.drain()
            emit_batched_states(merged)
            logger.debug("Final flush on shutdown: %d device(s)", len(merged))

        logger.info("send loop finished")


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
