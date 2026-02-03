#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import json
import time
from typing import Dict, Any

from wb.mqtt_alice.client.upstream.wb_proxy_socketio.adapter import SocketIOAdapter
from wb.mqtt_alice.client.upstream import UpstreamState

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
        "debug": False
    }
}

# --- Данные тестового устройства ---
DEVICE_ID = "d5a5a2ea-4bb4-4daa-99f5-ddbfde30a09d"
CURRENT_TEMP = -45.3

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


async def notification_loop(adapter: SocketIOAdapter):
    """
    Цикл, который каждые 5 секунд шлет обновленную температуру
    """
    global CURRENT_TEMP
    
    logger.info("Starting notification loop...")
    
    while True:
        # 1. Проверяем, готова ли связь
        if adapter._state != UpstreamState.READY:
            logger.debug("Upstream not ready, waiting...")
            await asyncio.sleep(1)
            continue

        # 2. Инкрементируем значение
        CURRENT_TEMP += 1.0
        
        # 3. Формируем пайлоад для Яндекса
        # Нам нужно отправить только то, что изменилось.
        # Формат: State Update Object
        notification_data = {
            "devices": [{
                "id": DEVICE_ID,
                "properties": [{
                    "type": "devices.properties.float",
                    "state": {
                        "instance": "temperature",
                        "value": round(CURRENT_TEMP, 1)
                    }
                }]
            }]
        }

        logger.info(f"Sending notification: Temp = {CURRENT_TEMP}")

        try:
            # 4. Отправляем через адаптер
            await adapter.send_notification(notification_data)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

        # 5. Ждем 5 секунд
        await asyncio.sleep(5)


async def main():
    # 1. Инициализация адаптера
    logger.info("Initializing Upstream Adapter...")
    adapter = SocketIOAdapter(CONFIG["wb_proxy"])

    # 2. Инициализация "фейкового" ядра и регистрация хендлеров
    # Адаптер не запустится (или будет сыпать ошибками), если хендлеры не назначены
    core = MockCore()
    adapter.register_discovery_handler(core.handle_discovery)
    adapter.register_query_handler(core.handle_query)
    adapter.register_command_handler(core.handle_action)

    # 3. Регистрация обработчика состояний (для логирования)
    async def on_state_change(state: UpstreamState):
        if state == UpstreamState.READY:
            logger.info(">>> UPSTREAM IS READY! (MQTT Subscriptions would start here) <<<")
        elif state == UpstreamState.UNAVAILABLE:
            logger.warning(">>> UPSTREAM UNAVAILABLE! (MQTT Subscriptions paused) <<<")
    
    adapter.register_state_handler(on_state_change)

    # 4. Запуск адаптера
    try:
        await adapter.start()
        
        # Опционально: Ждем готовности перед запуском цикла уведомлений, 
        # но наш цикл сам умеет ждать внутри while, так что не блокируем main
        
        # 5. Запуск цикла уведомлений
        notify_task = asyncio.create_task(notification_loop(adapter))

        # Бесконечное ожидание, чтобы программа не закрылась
        # В реальном приложении тут будет работа с другими компонентами
        while True:
            await asyncio.sleep(1)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Stopping...")
    finally:
        await adapter.stop()
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
