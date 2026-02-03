# Upstream Layer (Связь с Яндекс Алисой)

Этот модуль отвечает за взаимодействие с платформой умного дома\
Yandex Smart Home (Алиса). Модуль абстрагирует транспортный уровень,\
позволяя приложению работать одинаково как через облако (Socket.IO),\
так и через локальный сервер (FastAPI) или тестовый интерфейс.

## Архитектура

Архитектура строится том что адаптер не знает о существовании бизнес-логики,\
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
Ядро (Core) использует состояние адаптера, чтобы решать, нужно\
ли подписываться на MQTT-топики устройств.

Состояния определены в `UpstreamState`:

| Состояние | Описание | Реакция Ядра (Core) |
| :--- | :--- | :--- |
| `INITIALIZING` | Адаптер создан, но не запущен. | Ожидание. |
| **`READY`** | Канал связи установлен, адаптер готов к работе. | **Подключить MQTT**, начать обработку событий. |
| **`UNAVAILABLE`** | Временная потеря связи (Reconnecting...). | **Приостановить MQTT** (отписаться), чтобы не накапливать очередь событий. |
| `STOPPED` | Явная остановка работы. | Полное отключение. |

---

## Интерфейс (Handlers)

Ядро инжектирует свою логику в адаптер через методы регистрации:

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

Чтобы добавить новый способ связи (например, прямой MQTT-мост или CLI для тестов), нужно наследоваться от `BaseUpstreamAdapter`.

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

1. **Не импортируйте Core**
   В коде адаптера не должно быть импортов из `core/`, `infrastructure/` или `downstream/`

2. **Используйте Helpers**
   Для вызова логики ядра используйте методы родительского класса:

* `self._handle_incoming_discovery(payload)`
* `self._handle_incoming_command(payload)`
* `self._handle_incoming_query(payload)`

1. **Управляйте состоянием**
   Обязательно вызывайте `await self._set_state(...)` при потере\
   и восстановлении связи. Это управляет остальной системой.

---

## Структура папок

```text
upstream/
├── __init__.py          # Экспорт адаптеров
├── base.py              # Базовый абстрактный класс
├── types.py             # Enums и DTO
├── socketio/            # Адаптер: Cloud Proxy
│   ├── __init__.py
│   └── adapter.py
└── fastapi/             # Адаптер: Local Server
    ├── __init__.py
    └── adapter.py
```
