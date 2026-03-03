import asyncio
import logging
import os
import time
from collections import defaultdict
from os import getenv
from typing import Any, Dict, List, Optional

from .device_registry import DeviceRegistry
from .yandex_handlers import emit_batched_states

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)

MAX_BUFFER_SIZE = int(getenv("MAX_BUFFER_SIZE", "10"))
BATCH_INTERVAL = float(getenv("BATCH_INTERVAL", "1.0"))


class AliceDeviceStateSender:
    """
    Two-stage sender for device state updates to Yandex Smart Home

    Stage 1 — Per-topic rate limiter (existing logic):
      Each topic has its own time_rate. Messages arriving faster than
      time_rate are buffered and aggregated (last_value / average_value)
      Only when time_rate has elapsed does the topic "pass" to stage 2

    Stage 2 — Batch accumulator:
      Converted Yandex blocks from stage 1 accumulate in batch_buffer
      Flushed every BATCH_INTERVAL seconds or immediately when
      an event property arrives. Blocks are merged by device_id
    """

    def __init__(self, device_registry: DeviceRegistry):
        self.buffers = defaultdict(list)
        self.last_send_times = {}  # dict of last times by topic
        self.lock = asyncio.Lock()
        self.running = False
        self.device_registry = device_registry

        # Batch accumulator for converted Yandex blocks
        self.batch_buffer: List[Dict[str, Any]] = []
        self.flush_event = asyncio.Event()


    async def start(self):
        """
        Start the background send loop
        """
        logger.info(
            "Starting alice device state sender (batch_interval=%.1fs)", BATCH_INTERVAL
        )
        self.running = True
        asyncio.create_task(self.send_to_yandex_loop())

    async def stop(self):
        """
        Stop the background send loop
        Wakes loop so it can do a final flush before exiting
        """
        self.running = False
        self.flush_event.set()
        logger.info("Stopping alice device state sender")

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
            self.buffers[topic_str].append(message)
            # Limit buffer size (sliding window)
            if len(self.buffers[topic_str]) > MAX_BUFFER_SIZE:
                self.buffers[topic_str] = self.buffers[topic_str][-MAX_BUFFER_SIZE:]

            # Event property trigger immediate batch flush
            blk = self.device_registry.devices[device_id][cap_prop][idx]
            if blk.get("type", "").lower() == "devices.properties.event":
                logger.debug("Event on %r, triggering batch flush", topic_str)
                self.flush_event.set()


    async def send_to_yandex_loop(self):
        """
        Send message to yandex device
        This is background loop: stage 1 (rate limiter) + stage 2 (batch flush)
        """
        last_batch_time = time.time()

        while self.running:
            try:
                current_time = time.time()
                # --- Stage 1: Per-topic rate limiter ---
                # Only pass topics whose time_rate elapsed, result goes to batch_buffer
                async with self.lock:
                    for topic, messages in self.buffers.items():
                        if not messages:
                            logger.debug("not message info for topic=%s", topic)
                            continue
                        last_send_time = self.last_send_times.get(topic, 0)
                        rate = messages[-1]["origin_rate"].time_rate
                        
                        # check time-rate
                        if current_time - last_send_time <= rate:
                            continue  # Rate limit not passed yet

                        # Rate limit passed → aggregate and convert
                        aggregated = self.modify_messages_by_rule(
                            rule=messages[-1]["origin_rate"].rule,
                            messages=messages,
                        )
                        raw = str(aggregated["payload"])
                        # send message to Yandex
                        if os.getenv("__DEBUG__"):
                            logger.info("[DEBUG] rate-passed: topic=%s, raw=%s", topic, raw)
                        else:
                            block = self.device_registry.convert_mqtt_to_yandex_block(
                                topic=topic, raw=raw
                            )
                            if block is not None:
                                self.batch_buffer.append(block)

                        self.buffers[topic].clear()
                        self.last_send_times[topic] = current_time

                # --- Stage 2: Batch flush ---
                batch_age = current_time - last_batch_time
                should_flush = (
                    self.batch_buffer
                    and (batch_age >= BATCH_INTERVAL or self.flush_event.is_set())
                )
                if should_flush:
                    self.flush_event.clear()
                    merged = merge_device_blocks(self.batch_buffer)
                    logger.debug(
                        "Batch flush: %d block(s) → %d device(s)",
                        len(self.batch_buffer), len(merged),
                    )
                    emit_batched_states(merged)
                    self.batch_buffer.clear()
                    last_batch_time = current_time

            except Exception as e:
                logger.error("Error in send loop: %s", e, exc_info=True)
            await asyncio.sleep(0.1)

        # Final flush on shutdown
        if self.batch_buffer:
            merged = merge_device_blocks(self.batch_buffer)
            emit_batched_states(merged)
            self.batch_buffer.clear()
            logger.debug("Final flush on shutdown: %d device(s)", len(merged))

        logger.info("send loop finished")

    def get_device_info_by_topic(self, topic):
        return self.device_registry.topic2info.get(topic)

    @staticmethod
    def modify_messages_by_rule(rule: str, messages: List[dict]):
        """
        Apply aggregation rule to buffered messages for one topic
        Returns single message dict with aggregated payload
        """
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

def merge_device_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge multiple device state blocks by device_id

    If the same device_id appears in multiple blocks (e.g. on_off and brightness
    changed in the same batch window), their capabilities and properties lists
    are concatenated into a single device entry
    """
    merged: Dict[str, Dict[str, Any]] = {}

    for block in blocks:
        device_id = block["id"]
        if device_id not in merged:
            merged[device_id] = {
                "id": device_id,
                "status": block.get("status", "online"),
                "capabilities": [],
                "properties": [],
            }
        if "capabilities" in block:
            merged[device_id]["capabilities"].extend(block["capabilities"])
        if "properties" in block:
            merged[device_id]["properties"].extend(block["properties"])

    # Remove empty lists before sending
    result: List[Dict[str, Any]] = []
    for device in merged.values():
        if not device["capabilities"]:
            del device["capabilities"]
        if not device["properties"]:
            del device["properties"]
        result.append(device)

    return result
