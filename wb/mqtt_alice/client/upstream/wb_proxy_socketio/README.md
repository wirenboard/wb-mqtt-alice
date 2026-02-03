# WB Proxy Socket.IO Adapter

Этот модуль реализует **основной (продуктовый) режим** работы интеграции.\
Он обеспечивает связь с платформой умного дома Яндекс через промежуточный\
прокси-сервер Wiren Board (WB Cloud Proxy), используя протокол **Socket.IO**.

## Назначение

Адаптер служит мостом между Ядром приложения (Core) и облачным сервисом.

* **Отвечает за:** Поддержание постоянного соединения (WebSocket),\
  автоматическое переподключение, авторизацию по серийному номеру контроллера.
* **Инкапсулирует:** Всю сложность работы с библиотекой `python-socketio` и сетевыми ошибками.
* **Предоставляет:** Абстракцию `UpstreamState` для Ядра, чтобы управлять подписками на MQTT.

## Структура модуля

* **`adapter.py`**
  Реализация класса `SocketIOAdapter`, который наследуется от `BaseUpstreamAdapter`. Это "клей", который связывает драйвер соединения с бизнес-логикой приложения. Он транслирует события сети в статусы (`READY`, `UNAVAILABLE`) и передает команды от Яндекса в обработчики Ядра.

* **`sio_connection_manager.py`**
  Низкоуровневый "драйвер" транспорта. Содержит логику мониторинга соединения,\
  кастомный цикл переподключения (Reconnection Loop) и сбор метрик. Работает\
  как "черный ящик" для адаптера.

## Конфигурация

Адаптер инициализируется словарем конфигурации (обычно секция `wb_proxy` в главном конфиге).

### Пример JSON-конфига

```json
{
  "upstream_mode": "wb_proxy",
  "wb_proxy": {
    "url": "[https://proxy.wirenboard.com](https://proxy.wirenboard.com)",
    "controller_sn": "A1B2C3D4",
    "client_pkg_ver": "1.0.0",
    "debug": false
  }
}

```

### Параметры

* **`url`**: Адрес прокси-сервера (по умолчанию `https://proxy.wirenboard.com`).
* **`controller_sn`**: Серийный номер контроллера (используется для авторизации).
* **`client_pkg_ver`**: Версия клиента (отправляется в заголовках для статистики/совместимости).
* **`debug`**: Включает подробное логирование библиотеки `python-socketio` (полезно при отладке разрывов).

## Управление состоянием (Lifecycle)

Адаптер активно управляет состоянием `UpstreamState`, что позволяет Ядру экономить ресурсы при потере связи.

| Событие Socket.IO | Новый статус | Реакция системы |
| --- | --- | --- |
| **`connect`** | `READY` | Связь есть. Ядро **подписывается** на топики MQTT и начинает отправку событий. |
| **`disconnect`** | `UNAVAILABLE` | Связь потеряна. Ядро **отписывается** от MQTT (Pause), чтобы не накапливать очередь событий. |
| **`connect_error`** | `UNAVAILABLE` | Ошибка входа/сети. Режим ожидания восстановления. |
| `stop()` | `STOPPED` | Полная остановка сервиса. |

## Обработка событий

Адаптер слушает следующие события от сервера:

1. **`alice_devices_list`** (Discovery): Запрос списка устройств.
2. **`alice_devices_query`** (Query): Запрос текущего состояния.
3. **`alice_devices_action`** (Action): Команда на изменение состояния.

Исходящие уведомления (State Changes) отправляются событием **`alice_devices_state`**.

---

## Зависимости

* `python-socketio[asyncio_client]`
* `aiohttp` (как транспорт для socketio)

## Пример использования (в main.py)

Ниже показано, как инициализировать адаптер и связать его с логикой переключения MQTT.

```python
import asyncio
from upstream.wb_proxy_socketio import SocketIOAdapter
from upstream.types import UpstreamState

async def main():
    # 1. Конфигурация (из файла или ENV)
    config = {
        "url": "[https://proxy.wirenboard.com](https://proxy.wirenboard.com)",
        "controller_sn": "AABBCCDD",
        "client_pkg_ver": "1.2.0",
        "debug": False
    }

    # 2. Инициализация адаптера
    adapter = SocketIOAdapter(config)

    # 3. Определение логики реакции на связь (Важно для MQTT)
    async def on_upstream_state(state: UpstreamState):
        if state == UpstreamState.READY:
            print("Облако доступно. Подключаем MQTT...")
            # await mqtt_client.subscribe()
        elif state == UpstreamState.UNAVAILABLE:
            print("Облако недоступно. Отключаем MQTT (пауза)...")
            # await mqtt_client.unsubscribe()

    # 4. Регистрация хендлеров
    adapter.register_state_handler(on_upstream_state)
    # adapter.register_discovery_handler(registry.get_yandex_devices)
    # adapter.register_command_handler(router.handle_action)

    # 5. Запуск (неблокирующий)
    await adapter.start()

    # ... основной цикл приложения ...
```
