import asyncio
import logging
import os
import time
from collections import defaultdict
from os import getenv
from typing import Dict, List

from device_registry import DeviceRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)

MAX_BUFFER_SIZE = int(getenv("MAX_BUFFER_SIZE", "10"))


class AliceDeviceStateSender:
    """
    Class to send device states with time-rate
    """

    def __init__(self, device_registry: DeviceRegistry):
        self.buffers = defaultdict(list)
        self.last_send_times = {}  # dict of last times by topic
        self.lock = asyncio.Lock()
        self.running = False
        self.device_registry = device_registry

    async def start(self):
        """
        start task to time-rated send states
        """
        logger.info("start alice device state sender")
        self.running = True
        asyncio.create_task(self.send_to_yandex_loop())

    async def stop(self):
        """
        stop task
        """
        self.running = False
        logger.info("stop alice device state sender")

    async def add_message(self, topic_str: str, payload_str: str):
        """
        Add message to buffer
        """
        logger.debug("add message %s %s", topic_str, payload_str)
        # struct for device info
        async with self.lock:
            _, cap_prop, _, event_rate = self.get_device_info_by_topic(topic=topic_str)
            logger.debug("add message: cap_prop is: %s", cap_prop)
            logger.debug("add message: event_rate: %s", event_rate)
            message = {
                "topic": topic_str,
                "cap_prop": cap_prop,
                "payload": payload_str,
                "origin_rate": event_rate,
            }
            logger.debug("add message: message %s", message)
            self.buffers[topic_str].append(message)
            # limits the size of the buffer (slice window)
            if len(self.buffers[topic_str]) > MAX_BUFFER_SIZE:
                self.buffers[topic_str] = self.buffers[topic_str][-MAX_BUFFER_SIZE:]

    async def send_to_yandex_loop(self):
        """
        Send message to yandex device
        """
        while self.running:
            try:
                current_time = time.time()
                for topic, message_info in self.buffers.items():
                    if not message_info:
                        logger.debug("not message info for topic=%s", topic)
                        continue
                    last_send_time = self.last_send_times.get(topic, 0)
                    # check time-rate
                    if current_time - last_send_time > message_info[-1]["origin_rate"].time_rate:
                        self.process_and_send_message(topic, message_info)
                        self.buffers[topic].clear()
                        self.last_send_times[topic] = current_time
            except Exception as e:
                logger.error("Error in send loop: %s", e, exc_info=True)
            await asyncio.sleep(0.1)
        logger.info("send loop finished")

    def process_and_send_message(self, topic: str, message_info: List[Dict]):
        message_info = self.modify_messages_by_rule(
            rule=message_info[-1]["origin_rate"].rule,
            messages=message_info,
        )
        # send message to Yandex
        if os.getenv("__DEBUG__"):
            self.log_test_send(topic=topic, raw=message_info["payload"])
        else:
            logger.info("-> forward_mqtt_to_yandex")
            self.device_registry.forward_mqtt_to_yandex(topic=topic, raw=message_info["payload"])

    def get_device_info_by_topic(self, topic):
        return self.device_registry.topic2info.get(topic)

    @staticmethod
    def modify_messages_by_rule(rule: str, messages: List[dict]):
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

    @staticmethod
    def log_test_send(topic: str, raw: str):
        logger.info("log test send for %s : %s", topic, raw)
