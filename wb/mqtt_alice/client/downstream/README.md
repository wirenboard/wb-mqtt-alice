# Downstream Layer (Связь с устройствами)

Этот модуль отвечает за взаимодействие приложения с физическими устройствами\
или локальными шинами данных (например, MQTT-брокером контроллера Wiren Board).

Главная задача слоя - абстрагировать Диспетчер от особенностей конкретных\
протоколов (MQTT, HTTP, Modbus) и форматов полезной нагрузки (payload),\
приводя всё к единому внутреннему стандарту с которым уже будет работать\
диспетчер.

## Архитектура

Архитектура слоя `downstream` строится на разделении ответственности между\
**Транспортом** и **Преобразованием данных**.

Вместо монолитных клиентов используются две независимые сущности:
1. **Adapter (Транспорт)** - ничего не знает о смысле данных.
   Его задача: подключиться, подписаться, получить сырые байты (или строку)\
   и передать их дальше.
2. **Codec (Транслятор)** - ничего не знает о сети.
   Его задача:
   - взять сырые данные (например, байты `b"1"`) и превратить их
     в значение (например, `True`) понятное платформе Яндекса
   - или наоборот превратить данные яндекса в данные понятные MQTT топику WB

### Основные компоненты

* **`base.py`**: Абстрактные интерфейсы `DownstreamAdapter` и `DownstreamCodec`\
   Определяют контракты, которым обязаны следовать все реализации.

* **`models.py`**: Общие DTO-классы для обмена данными:\
   `RawDownstreamMessage` (входящие сырые данные) и `DownstreamWrite` (исходящие сырые данные).

* **`<Папки_с_адаптерами>/`**: Реализация адаптера (`adapter.py`) и кодека (`codec.py`)\
   Нужны например для работы через MQTT с конвенциями контроллеров Wiren Board.

---

## Жизненный цикл и Интерфейс

Взаимодействие Диспетчера с Downstream-слоем строится через инъекцию коллбека\
(обработчика сырых сообщений) при старте адаптера.

* `start(handler)`: Запуск транспорта (например, подключение к MQTT-брокеру).
  Адаптер начинает слушать сеть и при получении данных вызывает переданный
  ему `handler(RawDownstreamMessage)`.

* `stop()`: Корректное отключение (отписка, разрыв соединения).

* `write(req)`: Отправка сырых данных (публикация) в сеть, упакованных
  в объект `DownstreamWrite`.

---

## Как добавить новый протокол или формат

Чтобы добавить поддержку нового способа связи, необходимо создать свои\
реализации `DownstreamAdapter` и `DownstreamCodec`.

### Шаблон реализации Адаптера и Кодека

```python
from typing import Any
from .base import DownstreamAdapter, DownstreamCodec, RawMessageHandler
from .models import RawDownstreamMessage, DownstreamWrite

# Заглушка, в реальности импортируется из моделей
class PointSpec:
    y_type: str

class MyCustomAdapter(DownstreamAdapter):
    @property
    def downstream_name(self) -> str:
        return "my_custom_protocol"

    def start(self, handler: RawMessageHandler) -> None:
        self._handler = handler
        print("Подключение к шине данных...")
        # Имитация входящего сообщения от устройства
        raw_msg = RawDownstreamMessage(self.downstream_name, "device/sensor", b"ON")
        self._handler(raw_msg)

    def stop(self) -> None:
        print("Отключение от шины...")

    def write(self, req: DownstreamWrite) -> None:
        print(f"Отправка данных {req.payload} в {req.address}")


class MyCustomCodec(DownstreamCodec):
    @property
    def downstream_name(self) -> str:
        return "my_custom_protocol"

    def decode(self, spec: PointSpec, value_path: str, raw: Any) -> Any:
        # Превращаем сырые байты в понятный формат (например, boolean)
        if raw in (b"ON", "1", True):
            return True
        return False

    def encode(self, spec: PointSpec, value_path: str, canonical: Any) -> Any:
        # Превращаем команду от Алисы обратно в сырые байты для устройства
        return b"ON" if canonical else b"OFF"

```

## Как использовать Downstream

В вызывающем коде (обычно это Диспетчер или тесты) работа с адаптером\
и кодеком строится по следующему алгоритму:

1. Инициализация экземпляров адаптера и кодека.
2. Создание функции-обработчика входящих сообщений (`raw_message_handler`).
3. Внутри обработчика: использование **Кодека** для преобразования сырых данных в канонические.
4. Запуск **Адаптера** с передачей ему обработчика.
5. Для отправки команд: использование **Кодека** для генерации сырой нагрузки, затем передача её в `adapter.write()`.

### Абстрактный минимальный пример вызывающего кода

```python
from wb.mqtt_alice.client.downstream.models import DownstreamWrite
# Импортируем наши реализации из шаблона выше
from my_project.downstream import MyCustomAdapter, MyCustomCodec

# Заглушка спецификации (в реальности берется из реестра устройств)
class MockPointSpec:
    y_type = "devices.capabilities.on_off"


# 1. Обработчик входящих сообщений (Инжектируется Диспетчером)
def raw_message_handler(raw_msg):
    print(f"[ВХОДЯЩИЕ] Получены сырые данные: {raw_msg.payload}")
    
    # Переводим сырые данные в понятные Алисе значения
    canonical_value = codec.decode(spec, "value", raw_msg.payload)
    print(f"[ДЕКОДЕР] Значение для Алисы: {canonical_value}")


def main():
    adapter = MyCustomAdapter()
    codec = MyCustomCodec()
    spec = MockPointSpec()

    # 2. Запуск адаптера (начинаем слушать сеть)
    adapter.start(raw_message_handler)

    # 3. Имитация отправки команды от Алисы к устройству
    command_from_alice = True
    print(f"\n[АЛИСА] Команда: включить устройство ({command_from_alice})")
    
    # Кодируем True обратно в формат устройства
    raw_payload = codec.encode(spec, "value", command_from_alice)
    
    # Отправляем через адаптер
    adapter.write(DownstreamWrite(
        downstream_name=adapter.downstream_name,
        address="device/relay",
        payload=raw_payload
    ))

    # 4. Остановка
    adapter.stop()

if __name__ == "__main__":
    main()

```

## Структура папок

Актуальная структура downstream модуля выглядит следующим образом:

```text
downstream/
├── __init__.py
├── base.py                 # Базовые абстрактные классы (DownstreamAdapter, DownstreamCodec)
├── models.py               # DTO (RawDownstreamMessage, DownstreamWrite)
└── mqtt_wb_conv/           # Конкретная реализация (Wiren Board conventions)
    ├── __init__.py
    ├── adapter.py          # Реализация транспорта через paho-mqtt
    └── codec.py            # Правила парсинга и форматирования payload

```
