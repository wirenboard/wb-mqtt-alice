# Upstream Layer (Связь с Яндекс Алисой)

Этот модуль отвечает за взаимодействие с платформой умного дома\
Yandex Smart Home (Алиса). Модуль абстрагирует транспортный уровень,\
позволяя приложению работать одинаково как через облако (Socket.IO),\
так и через локальный сервер (FastAPI) или тестовый интерфейс.

## Архитектура

Архитектура строится на том, что адаптер не знает о существовании бизнес-логики,\
реестров устройств или MQTT. Он лишь транслирует события через зарегистрированные\
коллбеки (Handlers).

### Основные компоненты

* **`base.py`**: Абстрактный базовый класс `BaseUpstreamAdapter`.
  Определяет контракт, который обязан реализовать любой транспорт.
* **`types.py`**: Общие типы данных и перечисления (Enum), включая `UpstreamState`.
* **`socketio/`**: Реализация адаптера для облачного подключения.
* **`fastapi/`**: Реализация локального HTTP-сервера (для локальных интеграций или тестов).

---

## Жизненный цикл и Состояния

Управление состоянием критически важно для корректной работы системы.\
Диспетчер использует состояние адаптера, чтобы решать, нужно\
ли подписываться на MQTT-топики устройств.

Состояния определены в `UpstreamState`:

| Состояние | Описание | Реакция Диспетчера |
| :--- | :--- | :--- |
| `INITIALIZING` | Адаптер создан, но не запущен. | Ожидание. |
| **`READY`** | Канал связи установлен, адаптер готов к работе. | **Подключить MQTT**, начать обработку событий. |
| **`UNAVAILABLE`** | Временная потеря связи (Reconnecting...). | **Приостановить MQTT** (отписаться), чтобы не накапливать очередь событий. |
| `STOPPED` | Явная остановка работы. | Полное отключение. |

---

## Интерфейс (Handlers)

Диспетчер инжектирует свою логику в адаптер через методы регистрации:

1.  **`register_discovery_handler(func)`**:
    * Вызывается, когда Яндекс просит список устройств.
    * *Ожидает:* Список устройств в формате Yandex Smart Home JSON.

2.  **`register_command_handler(func)`**:
    * Вызывается, когда пользователь просит выполнить действие (включить свет).
    * *Аргумент:* `payload` (JSON команды).

3.  **`register_query_handler(func)`**:
    * Вызывается, когда Яндекс запрашивает актуальное состояние датчиков.
    * *Аргумент:* `payload` (JSON запроса).

4.  **`register_state_handler(func)`**:
    * Вызывается при изменении состояния адаптера (`READY` <-> `UNAVAILABLE`).
    * Используется для управления подписками `Downstream`.

---

## Как реализовать новый адаптер

Чтобы добавить новый способ связи (например, прямой MQTT-мост или CLI для\
тестов), нужно наследоваться от `BaseUpstreamAdapter`.

### Шаблон реализации

```python
from .base import BaseUpstreamAdapter
from .types import UpstreamState

class MyCustomAdapter(BaseUpstreamAdapter):

    async def start(self) -> None:
        # 1. Запустить транспорт (слушать порт, коннектиться к сокету)
        print("Starting transport...")
        
        # 2. Если всё ок — перевести в READY
        await self._set_state(UpstreamState.READY)

    async def stop(self) -> None:
        # 1. Остановить транспорт
        print("Stopping...")
        
        # 2. Перевести в STOPPED
        await self._set_state(UpstreamState.STOPPED)

    async def send_notification(self, notification_data: dict) -> None:
        # Отправить данные в Яндекс
        # Если транспорта нет — игнорировать или логировать
        if self._state == UpstreamState.READY:
            transport.send(notification_data)

    # --- Прием данных извне ---
    
    async def _on_network_data_received(self, raw_data):
        # Если пришла команда Action:
        response = await self._handle_incoming_command(raw_data)
        # Отправить response обратно...

```

### Правила для разработчика адаптера

1. **Не импортируйте Диспетчер**
   В коде адаптера не должно быть импортов из `dispatcher/` или `downstream/`

2. **Используйте Helpers**
   Для вызова логики диспетчера используйте методы родительского класса:

* `self._handle_incoming_discovery(payload)`
* `self._handle_incoming_command(payload)`
* `self._handle_incoming_query(payload)`

3. **Управляйте состоянием**
   Обязательно вызывайте `await self._set_state(...)` при потере\
   и восстановлении связи. Это управляет остальной системой.


## Как использовать адаптер

В вызывающем коде (например, в main.py или в тестах) работа с адаптером\
строится по следующему алгоритму:

1. Импортируем наш кастомный адаптер, написанный по шаблону выше
2. Создание экземпляра адаптера
3. Описание функций-обработчиков для последующего использования в адаптере
4. Регистрация обработчиков запросов
   Инъекция методов Диспетчера (discovery, command, query) в адаптер.\
   Без этого адаптер не будет знать, как отвечать на запросы платформы\
   умного дома
5. Регистрация обработчика состояний.
   Диспетчеру нужно знать, когда связь установлена (READY), чтобы поднять\
   подписки на устройства (Downstream), и когда она потеряна (UNAVAILABLE),\
   чтобы эти подписки поставить на паузу.
6. Запуск адаптера (start()).
7. Корректная остановка (stop()) при завершении работы приложения.

Абстрактный минимальный пример вызывающего кода:

```python
import asyncio
import logging
from wb.mqtt_alice.client.upstream.types import UpstreamState

# 1. Импортируем наш кастомный адаптер, написанный по шаблону выше
from my_project.upstream import MyCustomAdapter

logging.basicConfig(level=logging.INFO)


# 2. Описание функций-обработчиков для последующего использования в адаптере
async def handle_discovery(payload: dict) -> dict:
    return {"request_id": payload.get("request_id"), "payload": {"devices": []}}

async def handle_query(payload: dict) -> dict:
    return {"request_id": payload.get("request_id"), "payload": {"devices": []}}

async def handle_action(payload: dict) -> dict:
    return {"request_id": payload.get("request_id"), "payload": {"devices": []}}

async def main():
    # 3. Создание экземпляра адаптера
    adapter = MyCustomAdapter()
    
    # 4. Регистрация обработчиков запросов от платформы
    adapter.register_discovery_handler(handle_discovery)
    adapter.register_command_handler(handle_action)
    adapter.register_query_handler(handle_query)

    # 5. Регистрация обработчика состояний
    async def on_state_change(state: UpstreamState):
        if state == UpstreamState.READY:
            logging.info(">>> UPSTREAM ГОТОВ! (Здесь запускаются подписки на устройства)")
        elif state == UpstreamState.UNAVAILABLE:
            logging.warning(">>> UPSTREAM НЕДОСТУПЕН! (Здесь подписки ставятся на паузу)")
            
    adapter.register_state_handler(on_state_change)

    # 6. Запуск адаптера
    try:
        await adapter.start()
        
        # Бесконечный цикл для поддержания работы (заглушка)
        while True:
            await asyncio.sleep(1)
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("Остановка приложения...")
    finally:
        # 7. Корректная остановка
        await adapter.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

## Структура папок

Актуальная структура upstream модуля связи выглядит следующим образом:

```text
upstream/
├── __init__.py
├── base.py                  # Базовый абстрактный класс (BaseUpstreamAdapter)
├── types.py                 # Enums (UpstreamState) и DTO
├── wb_proxy_socketio/       # Адаптер: Cloud Proxy (Socket.IO)
│   ├── __init__.py
│   └── adapter.py
├── direct_connect_fastapi/  # Адаптер: Local Server (FastAPI)
│   ├── __init__.py
│   └── adapter.py
└── offline_debug/           # Адаптер: Для локальной отладки
    ├── __init__.py
    └── adapter.py
```
