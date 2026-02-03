# upstream/wb_proxy_socketio/adapter.py

import logging
import asyncio
from typing import Any, Dict, Optional

from ..base import BaseUpstreamAdapter
from ..types import UpstreamState

from .sio_connection_manager import SioConnectionManager

logger = logging.getLogger(__name__)

class SocketIOAdapter(BaseUpstreamAdapter):
    """
    Адаптер для работы через прокси-сервер Wiren Board (WB Cloud)
    Использует Socket.IO для двусторонней связи
    
    Роль:
    1. Инициализирует SioConnectionManager (драйвер)
    2. Транслирует события сети (connect/disconnect) в статусы UpstreamState
    3. Передает входящие команды от Яндекса в Ядро через методы BaseUpstreamAdapter
    """

    def __init__(self, config: dict):
        """
        Инициализация адаптера
        
        Args:
            config: Словарь конфигурации. Ожидаемые ключи:
                - url (str): Адрес сервера (по умолчанию http://localhost:8042)
                - controller_sn (str): Серийный номер контроллера
                - client_pkg_ver (str): Версия пакета клиента
                - debug (bool): Включить отладку socketio
        """
        super().__init__(config)
        self._manager: Optional[SioConnectionManager] = None
        
        # Извлекаем параметры из конфига с дефолтными значениями
        self.server_url = config.get("url", "http://localhost:8042")
        self.controller_sn = config.get("controller_sn", "unknown_sn")
        self.client_pkg_ver = config.get("client_pkg_ver", "1.0.0")
        self.debug_logging = config.get("debug", False)
        
        # Настройки реконнекта (можно также вынести в конфиг)
        self.reconnection_enabled = True

    async def start(self) -> None:
        """
        Запуск адаптера:
        1. Создание менеджера соединений
        2. Подписка на системные и бизнес-события
        3. Запуск фонового процесса подключения
        """
        if self._state != UpstreamState.INITIALIZING and self._state != UpstreamState.STOPPED:
            logger.warning("SocketIOAdapter already started")
            return

        logger.info(f"Starting SocketIOAdapter (Server: {self.server_url})")

        # 1. Инициализируем драйвер (SioConnectionManager)
        self._manager = SioConnectionManager(
            server_url=self.server_url,
            controller_sn=self.controller_sn,
            client_pkg_ver=self.client_pkg_ver,
            debug_logging=self.debug_logging,
            reconnection=self.reconnection_enabled,
            # Можно прокинуть дополнительные параметры таймаутов, если они есть в конфиге
        )

        # 2. Регистрируем обработчики событий СОЕДИНЕНИЯ (Infrastructure)
        self._manager.on("connect", self._on_connect)
        self._manager.on("disconnect", self._on_disconnect)
        self._manager.on("connect_error", self._on_connect_error)

        # 3. Регистрируем обработчики событий АЛИСЫ (Business Logic)
        # Имена событий ('alice_devices_list' и т.д.) определены протоколом общения с сервером
        self._manager.on("alice_devices_list", self._on_alice_devices_list)
        self._manager.on("alice_devices_query", self._on_alice_devices_query)
        self._manager.on("alice_devices_action", self._on_alice_devices_action)

        # 4. Запускаем подключение (неблокирующе)
        # SioConnectionManager.connect() запускает свои таски мониторинга
        loop = asyncio.get_running_loop()
        loop.create_task(self._manager.connect())
        
        # Примечание: Мы НЕ ставим state=READY здесь. 
        # State станет READY только когда реально вызовется _on_connect.

    async def stop(self) -> None:
        """
        Остановка адаптера. Разрывает соединение и очищает ресурсы
        """
        if self._manager:
            await self._manager.disconnect_client()
        
        await self._set_state(UpstreamState.STOPPED)
        logger.info("SocketIOAdapter stopped")

    async def send_notification(self, notification_data: dict) -> None:
        """
        Отправка уведомления об изменении состояния в облако
        """
        # Если мы не в состоянии READY (нет связи), то слать бессмысленно
        if self._state != UpstreamState.READY or not self._manager:
            # Тут можно добавить логирование уровня DEBUG, чтобы не спамить в лог ошибками при разрыве
            return

        try:
            # Протокол требует обернуть данные в событие 'alice_devices_state'
            # Формат payload должен соответствовать спецификации Яндекса
            payload = {"payload": notification_data}
            
            # Используем emit менеджера
            await self._manager.emit("alice_devices_state", payload)
        except Exception as e:
            logger.error(f"Failed to send notification to Cloud: {e}")

    # =========================================================================
    # Внутренние обработчики (SioManager -> Adapter -> Core)
    # =========================================================================

    async def _on_connect(self) -> None:
        """
        Связь установлена
        """
        # Переводим в READY -> Core включит подписку на MQTT
        await self._set_state(UpstreamState.READY)

    async def _on_disconnect(self) -> None:
        """
        Связь потеряна
        """
        # Переводим в UNAVAILABLE -> Core поставит MQTT на паузу
        await self._set_state(UpstreamState.UNAVAILABLE)

    async def _on_connect_error(self, data: Any) -> None:
        """
        Ошибка соединения
        """
        # Тоже считаем недоступным
        if self._state != UpstreamState.UNAVAILABLE:
            await self._set_state(UpstreamState.UNAVAILABLE)

    # --- Обработка запросов от Яндекса ---

    async def _on_alice_devices_list(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Запрос списка устройств (Discovery)
        data: {"request_id": "..."}
        """
        # Делегируем в базовый класс, который вызовет injected handler (Core)
        return await self._handle_incoming_discovery(data)

    async def _on_alice_devices_query(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Запрос состояния устройств (Query)
        data: {"devices": [...], "request_id": "..."}
        """
        return await self._handle_incoming_query(data)

    async def _on_alice_devices_action(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Запрос на действие (Action)
        data: {"payload": {"devices": [...]}, "request_id": "..."}
        """
        return await self._handle_incoming_command(data)