#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging

from wb.mqtt_alice.client.upstream.wb_proxy_socketio.adapter import SocketIOAdapter
from wb.mqtt_alice.client.upstream.types import UpstreamState
from wb.mqtt_alice.client.downstream.models import RawDownstreamMessage
from wb.mqtt_alice.client.downstream.mqtt_wb_conv.adapter import (
    MqttWbConvAdapter,
    MqttConnectionConfig,
)
from wb.mqtt_alice.client.downstream.mqtt_wb_conv.codec import MqttWbConvCodec
from .models import PointSpec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Main")

# --- Конфигурация (в реальности читается из файла) ---
CONFIG = {
    "mode": "wb_proxy",
    "wb_proxy": {
        "url": "http://localhost:8042",
        "controller_sn": "TEST_CONTROLLER_001",
        "client_pkg_ver": "0.1.0-alpha",
        "debug": False,
    },
}

# --- Данные тестового устройства ---
DEVICE_ID = "60ba5cbd-260128210654-604e8e10-748e-43f5-a028-1f3ce82b22e3"
TEST_TOPIC = "/devices/alice_devices/controls/color_temperature_k"

# --- Спецификация для Кодека ---
# В реальном Диспетчере это собирается динамически из конфигурации
TEST_POINT_SPEC = PointSpec(
    device_id=DEVICE_ID,
    point="properties:devices.properties.float:temperature",
    y_type="devices.properties.float",
    instance="temperature",
    parameters={"instance": "temperature", "unit": "unit.temperature.celsius"},
    is_single_topic=False,
)


class MockCore:
    """
    Заглушка для Ядра (Core)
    Нужна, чтобы зарегистрировать обязательные хендлеры в адаптере,
    пока у нас нет настоящего Router и Registry
    """

    async def handle_discovery(self, payload: dict) -> dict:
        logger.info(f"Incoming DISCOVERY request: {payload}")
        # Возвращаем пустой список или тестовый девайс
        return {"request_id": payload.get("request_id"), "payload": {"devices": []}}

    async def handle_query(self, payload: dict) -> dict:
        logger.info(f"Incoming QUERY request: {payload}")
        return {"request_id": payload.get("request_id"), "payload": {"devices": []}}

    async def handle_action(self, payload: dict) -> dict:
        logger.info(f"Incoming ACTION request: {payload}")
        return {"request_id": payload.get("request_id"), "payload": {"devices": []}}


async def main():
    # 1. Инициализация Upstream адаптера (Алиса)
    logger.info("Initializing Upstream Adapter...")
    upstream_adapter = SocketIOAdapter(CONFIG["wb_proxy"])

    # 2. Инициализация "фейкового" ядра и регистрация хендлеров
    # Адаптер не запустится (или будет сыпать ошибками), если хендлеры не назначены
    dispatcher = MockCore()
    upstream_adapter.register_discovery_handler(dispatcher.handle_discovery)
    upstream_adapter.register_command_handler(dispatcher.handle_action)
    upstream_adapter.register_query_handler(dispatcher.handle_query)

    # 3. Инициализация Downstream (Устройства)
    logger.info("Initializing Downstream Adapter...")
    mqtt_config = MqttConnectionConfig(host="localhost", port=1883)
    downstream_adapter = MqttWbConvAdapter(cfg=mqtt_config, subscriptions=[TEST_TOPIC])
    codec = MqttWbConvCodec()

    # 4. Обработчик входящих сообщений от MQTT
    def on_mqtt_message(raw_msg: RawDownstreamMessage):
        if upstream_adapter._state != UpstreamState.READY:
            logger.debug("Upstream not ready, dropping MQTT message")
            return

        # Декодируем сырые байты в число
        canonical_value = codec.decode(
            spec=TEST_POINT_SPEC, value_path="value", raw=raw_msg.payload
        )
        if canonical_value is None:
            logger.warning(f"Failed to decode value from topic: {raw_msg.address}")
            return

        logger.info(
            f"MQTT msg received: topic={raw_msg.address}, decoded_val={canonical_value}"
        )

        # Формируем пайлоад для Яндекса
        notification_data = {
            "devices": [
                {
                    "id": DEVICE_ID,
                    "properties": [
                        {
                            "type": TEST_POINT_SPEC.y_type,
                            "state": {
                                "instance": TEST_POINT_SPEC.instance,
                                "value": canonical_value,
                            },
                        }
                    ],
                }
            ]
        }

        # В paho-mqtt коллбек вызывается в отдельном потоке,
        # поэтому нужно закинуть корутину в основной asyncio-цикл
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(
            upstream_adapter.send_notification(notification_data), loop
        )

    # 5. Регистрация обработчика состояний Upstream (Связь с Алисой)
    async def on_state_change(state: UpstreamState):
        if state == UpstreamState.READY:
            logger.info(">>> UPSTREAM IS READY! Resuming MQTT Subscriptions <<<")
            downstream_adapter.resume_subscriptions()
        elif state == UpstreamState.UNAVAILABLE:
            logger.warning(">>> UPSTREAM UNAVAILABLE! Pausing MQTT Subscriptions <<<")
            downstream_adapter.pause_subscriptions()

    upstream_adapter.register_state_handler(on_state_change)

    # 6. Запуск компонентов
    try:
        # Запускаем downstream. Изначально мы не подписываемся на топики,
        # так как это произойдет в on_state_change при READY
        downstream_adapter.start(on_mqtt_message)
        await upstream_adapter.start()

        # Бесконечное ожидание, чтобы программа не закрылась
        # В реальном приложении тут будет работа с другими компонентами
        while True:
            await asyncio.sleep(1)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Stopping...")
    finally:
        await upstream_adapter.stop()
        downstream_adapter.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
