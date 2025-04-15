# client.py (Socket.IO client)
import asyncio
import json
import logging
import os
import signal
import time

import paho.mqtt.client as mqtt
import paho.mqtt.subscribe as subscribe
import requests
import socketio
from wb_common.mqtt_client import MQTTClient

from mqtt_topic import MQTTTopic

logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

# Configuration file paths
SHORT_SN_PATH = "/var/lib/wirenboard/short_sn.conf"
CONFIG_PATH = "/etc/wb-alice-client.conf"

YANDEX_SKILL_ID = "add0dfb6-5b76-4eb9-a07e-7f4fe401881d"  # Скил с именем: 'Wiren Board'
YANDEX_SKILL_OAUTH_TOKEN = "y0__xCAvtL0Bxij9xMg1aay2RJywqcZFALIyCvP-9OR3B2gAZIzFA"
YANDEX_USER_ID = "wb-test-user"

# Если до этого яндекс не получал пакет обновления девайсов с этим именем то у него будут ошибки возвращаться
# INFO:__main__:[INCOME] Server response: {'data': 'Message received'}
# INFO:__main__:[YANDEX] Ошибка 400: {"request_id":"c21191a3-ce61-4f77-8d3b-a5f5da9163f6","status":"error","error_code":"UNKNOWN_USER"}
# INFO:__main__:[MQTT] Получено сообщение: 0 в топике /devices/wb-mr6c_1/controls/K2
# INFO:__main__:[MQTT] Получено сообщение: 22.29 в топике /devices/wb-msw-v4_2/controls/Temperature
# INFO:__main__:[YANDEX] Ошибка 400: {"request_id":"58c1d457-9651-42b8-8a7c-52f1cea4d44f","status":"error","error_code":"UNKNOWN_USER"}

mqtt_topics = {
    "light_corridor": None,
    "light_bedroom": None,
    "temperature_corridor": None,
}


def read_mqtt_topics(repo):
    try:
        with open("/etc/wb-alice-devices.conf", "r") as f:
            config = json.load(f)

        repo["light_corridor"] = MQTTTopic(
            config["devices"][0]["capabilities"][0]["mqtt"]
        )
        repo["light_bedroom"] = MQTTTopic(
            config["devices"][1]["capabilities"][0]["mqtt"]
        )
        repo["temperature_corridor"] = MQTTTopic(
            config["devices"][2]["properties"][0]["mqtt"]
        )

        logger.info("Загружены топики:")
        if repo["light_corridor"]:
            logger.info(
                f"Свет light_corridor: {repo['light_corridor'].short} (полный: {repo['light_corridor'].full})"
            )
        else:
            logger.info("Топик для света light_corridor не найден")

        if repo["light_bedroom"]:
            logger.info(
                f"Свет light_bedroom: {repo['light_bedroom'].short} (полный: {repo['light_bedroom'].full})"
            )
        else:
            logger.info("Топик для света light_bedroom не найден")

        if repo["temperature_corridor"]:
            logger.info(
                f"Температура: {repo['temperature_corridor'].short} (полный: {repo['temperature_corridor'].full})"
            )
        else:
            logger.info("Топик для температуры не найден")
        return True
    except Exception as e:
        logger.info(f"Ошибка при чтении конфигурационного файла: {e}")
        return False


read_mqtt_topics(mqtt_topics)

last_temp_value = None


def send_temperature_to_yandex(device_id, temp_value):
    timestamp = int(time.time())
    url = f"https://dialogs.yandex.net/api/v1/skills/{YANDEX_SKILL_ID}/callback/state"
    headers = {
        "Authorization": f"OAuth {YANDEX_SKILL_OAUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "ts": timestamp,
        "payload": {
            "user_id": YANDEX_USER_ID,
            "devices": [
                {
                    "id": device_id,
                    "status": "online",
                    "properties": [
                        {
                            "type": "devices.properties.float",
                            "state": {"instance": "temperature", "value": temp_value},
                        }
                    ],
                }
            ],
        },
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 202:
            logger.info(f"[YANDEX] Отправлено новое значение температуры: {temp_value}")
        else:
            logger.info(f"[YANDEX] Ошибка {response.status_code}: {response.text}")
    except Exception as e:
        logger.info(f"[YANDEX] Ошибка при отправке: {e}")


def send_relay_state_to_yandex(device_id, raw_state):
    """
    Преобразует входное значение (строка или число) в булево и отправляет состояние реле в Яндекс.Диалоги.

    Поддерживаемые значения:
      - строки: "1", "0", "true", "false", "on", "off"
      - числа: 1, 0, любые другие
      - булевы значения: True, False
    """

    is_on = False  # по умолчанию — выключено

    try:
        if isinstance(raw_state, str):
            raw_state = raw_state.strip().lower()
            if raw_state in ["1", "true", "on"]:
                is_on = True
            elif raw_state in ["0", "false", "off"]:
                is_on = False
            elif raw_state.isdigit():
                is_on = int(raw_state) != 0
            else:
                logger.info(
                    f"[RELAY] Неизвестная строка состояния: '{raw_state}' — по умолчанию выключено"
                )
        elif isinstance(raw_state, (int, float)):
            is_on = raw_state != 0
        elif isinstance(raw_state, bool):
            is_on = raw_state
        else:
            logger.info(
                f"[RELAY] Неизвестный тип состояния: {type(raw_state)} — по умолчанию выключено"
            )
    except Exception as e:
        logger.info(f"[RELAY] Ошибка при преобразовании состояния в bool: {e}")
        is_on = False

    timestamp = int(time.time())
    url = f"https://dialogs.yandex.net/api/v1/skills/{YANDEX_SKILL_ID}/callback/state"
    headers = {
        "Authorization": f"OAuth {YANDEX_SKILL_OAUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "ts": timestamp,
        "payload": {
            "user_id": YANDEX_USER_ID,
            "devices": [
                {
                    "id": device_id,
                    "status": "online",
                    "capabilities": [
                        {
                            "type": "devices.capabilities.on_off",
                            "state": {"instance": "on", "value": is_on},
                        }
                    ],
                }
            ],
        },
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 202:
            logger.info(f"[YANDEX] Отправлено новое состояние реле: {is_on}")
        else:
            logger.info(f"[YANDEX] Ошибка {response.status_code}: {response.text}")
    except Exception as e:
        logger.info(f"[YANDEX] Ошибка при отправке состояния реле: {e}")


def on_connect(client, userdata, flags, rc):
    logger.info(f"[MQTT] Подключено с кодом: {rc}")

    # Подписываемся на все топики в mqtt_topics
    for topic_key, topic_obj in mqtt_topics.items():
        if topic_obj:  # Проверка, что топик не None
            client.subscribe(topic_obj.full, qos=0)
            logger.info(f"[MQTT] Подписан на топик '{topic_key}': {topic_obj.full}")
        else:
            logger.info(f"[MQTT] Топик '{topic_key}' не определен, подписка невозможна")


def on_message(client, userdata, message):
    global last_temp_value

    topic = message.topic
    payload_str = message.payload.decode().strip()

    logger.info(f"[MQTT] Получено сообщение: {payload_str} в топике {topic}")

    if topic == mqtt_topics["temperature_corridor"].full:
        try:
            new_temp = float(payload_str)

            # TODO:(vg) Реализовать больше оптимизаций по передаваемым данным
            #           Важно сделать ограничение частоты отправляемых данных чтобы
            #           не отправлять все данные с провогдного датчика температуры
            #           если он изменяется 10 раз в секунду.
            #           Возможные оптимизации:
            #             - Минимальое изменение численное, например для температуры 0.2,
            #               чтобы не передавать каждый раз изменения на 0.01 градус
            #             - Минимальное время - если есть выбросы копить и усреднять,
            #               чтобы не передавать изменения температуры от каждого
            #               дуновения ветра из окна
            if last_temp_value is None or abs(new_temp - last_temp_value) >= 0.2:
                last_temp_value = new_temp
                send_temperature_to_yandex(
                    "7065fcf8-963e-42a2-a4f5-e668e412d1c4", new_temp
                )
            else:
                logger.info(
                    "[TEMP] Температура не изменилась существенно — не отправляем."
                )
        except ValueError:
            logger.info("[TEMP] Ошибка при разборе float температуры")

    elif topic == mqtt_topics["light_corridor"].full:
        try:
            relay_state = payload_str.strip().lower()
            if relay_state in ["0", "off", "false"]:
                relay_on = False
            else:
                relay_on = True

            send_relay_state_to_yandex("f52856d1-4fca-40ce-8ee3-e5ed37298d6e", relay_on)

        except Exception as e:
            logger.info(f"[RELAY] Ошибка при разборе состояния реле: {e}")


mqtt_client = MQTTClient(
    client_id_prefix="my_simple_app",
    broker_url="tcp://localhost:1883",
    is_threaded=True,
)
mqtt_client.on_message = on_message
mqtt_client.on_connect = on_connect


# === Основной async-запуск с бесконечным ожиданием ===
async def main():
    logger.info("[MAIN] Запускаем MQTT клиент...")
    mqtt_client.start()

    # Создаем asyncio-событие, чтобы держать программу "живой" и блокировать цикл до SIGINT (Ctrl+C)
    stop_event = asyncio.Event()

    # Обработчик SIGINT/SIGTERM (Перехватываем Ctrl+C, systemd stop)
    def shutdown_handler():
        logger.info("\n[MAIN] Получен сигнал завершения")
        stop_event.set()

    # Кроссплатформенно: в Unix можно использовать `loop.add_signal_handler`
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig), shutdown_handler)
        except NotImplementedError:
            # На Windows add_signal_handler не работает
            pass

    # Параллельно запускаем контроллер (Socket.IO)
    controller_task = asyncio.create_task(connect_controller())

    # Ждём завершения либо контроллера, либо сигнала
    logger.info("[MAIN] Клиент работает. Для выхода нажмите Ctrl+C.")
    done, pending = await asyncio.wait(
        [controller_task, stop_event.wait()],  # ждем, пока не вызовется .set()
        return_when=asyncio.FIRST_COMPLETED,
    )

    if controller_task in done and controller_task.exception():
        logger.info("[MAIN] Ошибка в connect_controller:", controller_task.exception())

    logger.info("[MAIN] Останавливаем MQTT клиент...")
    mqtt_client.stop()
    logger.info("[MAIN] Завершено.")


def get_controller_sn():
    """Get controller ID from the configuration file"""
    try:
        with open(SHORT_SN_PATH, "r") as file:
            controller_sn = file.read().strip()
            logger.info(f"[INFO] Read controller ID: {controller_sn}")
            return controller_sn
    except FileNotFoundError:
        logger.info(
            f"[ERR] Controller ID file not found! Check the path: {SHORT_SN_PATH}"
        )
        return None
    except Exception as e:
        logger.info(f"[ERR] Reading controller ID exception: {e}")
        return None


def read_config():
    """Read configuration file"""
    try:
        if not os.path.exists(CONFIG_PATH):
            logger.info(f"[ERR] Configuration file not found at {CONFIG_PATH}")
            return None

        with open(CONFIG_PATH, "r") as file:
            config = json.load(file)
            return config
    except json.JSONDecodeError:
        logger.info("[ERR] Parsing configuration file: Invalid JSON format")
        return None
    except Exception as e:
        logger.info(f"[ERR] Reading configuration exception: {e}")
        return None


def read_mqtt_state(topic: str, mqtt_host="localhost", timeout=1) -> bool:
    """
    Читает значение топика (0/1, "false"/"true" и т.п.) и возвращает Python bool.
    Используем subscribe.simple(...) из paho.mqtt, который БЛОКИРУЕТСЯ на время чтения.
    По умолчанию timeout=1 секунду.
    """
    logger.info(f"[read_mqtt_state] try read topic: {topic}")
    try:
        msg = subscribe.simple(topic, hostname=mqtt_host, retained=True)
        logger.info("Current topic '%s' state: '%s'" % (msg.topic, msg.payload))

        payload_str = msg.payload.decode().strip().lower()
        # Интерпретируем разные варианты
        if payload_str in ["1", "true", "on"]:
            return True
        elif payload_str in ["0", "false", "off"]:
            return False
        else:
            logger.info(
                f"[WARN] Unexpected payload in topic '{topic}': {payload_str}, defaulting to False"
            )
            return False
    except Exception as e:
        logger.info(f"[WARN] Could not read MQTT topic '{topic}': {e}")
        # При ошибке чтения вернём False или делайте как вам нужно
        return False


def write_mqtt_state(mqtt_client: mqtt.Client, topic: str, is_on: bool):
    """
    Публикует "1" (True) или "0" (False) в указанный топик.
    """
    payload_str = "1" if is_on else "0"
    mqtt_client.publish(topic + "/on", payload_str)
    logger.info(f"[MQTT] Published '{payload_str}' to '{topic}'")


async def connect_controller():
    config = read_config()
    if not config:
        logger.info("[ERR] Cannot proceed without configuration")
        return

    # TODO(vg): On this moment this parameter hardcoded - must set after
    #           register controller on web server automatically
    if not config.get("is_registered", False):
        logger.info(
            "[ERR] Controller is not registered. Please register the controller first."
        )
        return

    controller_sn = get_controller_sn()
    if not controller_sn:
        logger.info("[ERR] Cannot proceed without controller ID")
        return

    server_address = config.get("server_address")
    if not server_address:
        logger.info("[ERR] 'server_address' not specified in configuration")
        return

    # Connect to local MQTT broker (assuming Wiren Board default: localhost:1883)
    mqtt_client = mqtt.Client("wb-alice-client")
    try:
        mqtt_client.connect("localhost", 1883, 60)
        mqtt_client.loop_start()
        logger.info(
            f"[INFO] Connected to local MQTT broker, using topic '{mqtt_topics['light_corridor'].full}'"
        )
    except Exception as e:
        logger.info(f"[ERR] MQTT connect failed: {e}")
        # Можно прервать работу или продолжить без MQTT
        # Жёстко прерываем при отсутствии брокера
        return

    server_url = f"https://{server_address}"
    logger.info(f"[INFO] Connecting to Socket.IO server: {server_url}")

    sio = socketio.AsyncClient()

    @sio.event
    async def connect():
        logger.info("[SUCCESS] Connected to Socket.IO server!")
        await sio.emit("message", {"controller_sn": controller_sn, "status": "online"})

    @sio.event
    def private_event(data):
        """
        Called when server sends 'private_event'.
        Here we also publish to the MQTT topic.
        """
        logger.info(f"[SOCKET.IO] private_event received: {data}")
        # Publish to MQTT
        try:
            current_state = read_mqtt_state(
                mqtt_topics["light_corridor"].full, mqtt_host="localhost", timeout=1
            )
            new_state = not current_state  # Инвертируем
            write_mqtt_state(mqtt_client, mqtt_topics["light_corridor"].full, new_state)
            logger.info(f"Inverted state from {current_state} to {new_state}")
        except Exception as e:
            logger.info(f"[ERR] Error in private_event logic: {e}")

        # payload = json.dumps(data)

        # msg = subscribe.simple(mqtt_topics['light_corridor'].full, hostname="localhost")
        # logger.info("Current topic '%s' state: '%s'" % (msg.topic, msg.payload))

        # current_k1_state = msg.payload.decode()

        # # Инвертируем
        # new_state = "1" if current_k1_state == "0" else "0"
        # logger.info(f"Inverting K1 from {current_k1_state} to {new_state}")

        # # Публикуем новое состояние
        # mqtt_client.publish(mqtt_topics['light_corridor'].full + '/on', new_state)
        # logger.info(f"[MQTT] Published to topic '{mqtt_topics['light_corridor'].full}/on': {new_state}")

    @sio.event
    async def disconnect():
        logger.info("[ERR] Disconnected from server")

    @sio.event
    async def response(data):
        logger.info(f"[INCOME] Server response: {data}")

    @sio.event
    async def error(data):
        logger.info(f"[ERR] Server error: {data}")
        logger.info("[ERR] Terminating connection due to server error")
        await sio.disconnect()

    @sio.event
    async def connect_error(data):
        logger.info(f"❌ Connection refused by server: {data}")

    # ----------------- ВАЖНО: Обработчики alice_* -----------------
    # При использовании sio.call("alice_devices_list") на сервере,
    # клиенту прилетит событие "alice_devices_list". Нужно вернуть ответ.

    @sio.on("alice_devices_list")
    async def on_alice_devices_list(data):
        """
        Получаем запрос на список устройств и формируем ответ.
        В ответе сохраняем текущие состояния (если нужно).
        """
        logger.info(f"[SOCKET.IO] alice_devices_list event: {data}")

        # Формируем список всех устройств (без state или вместе? вроде лучше вместе с состоянием).
        devices_list = []

        # Свет
        devices_list.append(
            {
                "id": "f52856d1-4fca-40ce-8ee3-e5ed37298d6e",
                "name": "Свет",
                "status_info": {"reportable": False},
                "description": "Свет в коридоре",
                "room": "Коридор",
                "type": "devices.types.light",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "retrievable": True,
                    }
                ],
            }
        )

        devices_list.append(
            {
                "id": "67a2d9b9-666b-41e1-957e-90cd742ec554",
                "name": "Свет",
                "status_info": {"reportable": False},
                "description": "Свет в спальне",
                "room": "Спальня",
                "type": "devices.types.light",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "retrievable": True,
                    }
                ],
            }
        )

        # Датчик температуры
        devices_list.append(
            {
                "id": "7065fcf8-963e-42a2-a4f5-e668e412d1c4",
                "name": "Температура",
                "status_info": {"reportable": False},
                "description": "Датчик температуры в спальне",
                "room": "Спальня",
                "type": "devices.types.sensor.climate",
                "properties": [
                    {
                        "type": "devices.properties.float",
                        "retrievable": True,
                        "reportable": True,
                        "parameters": {
                            "instance": "temperature",
                            "unit": "unit.temperature.celsius",
                        },
                    }
                ],
            }
        )

        # Возвращаем
        devices_response = {
            "requestId": 123,
            "payload": {"user_id": "test-user", "devices": devices_list},
        }

        return devices_response

    @sio.on("alice_devices_query")
    async def on_alice_devices_query(data):
        """
        Получаем запрос на текущее состояние устройств
        """
        logger.info("[SOCKET.IO] alice_devices_query event:")
        logger.info(json.dumps(data, ensure_ascii=False, indent=2))

        request_id = data.get("requestId", "unknown")

        # Массив ответов: сюда добавим блоки "id" + "capabilities"/"properties" для каждого
        devices_response = []

        for dev_request in data.get("devices", []):
            device_id = dev_request.get("id")
            if not device_id:
                continue

            if device_id == "f52856d1-4fca-40ce-8ee3-e5ed37298d6e":
                # Считываем из MQTT текущее состояние (bool)
                is_on = read_mqtt_state(
                    mqtt_topics["light_corridor"].full, mqtt_host="localhost"
                )
                devices_response.append(
                    {
                        "id": device_id,
                        "capabilities": [
                            {
                                "type": "devices.capabilities.on_off",
                                "state": {"instance": "on", "value": is_on},
                            }
                        ],
                    }
                )

            elif device_id == "67a2d9b9-666b-41e1-957e-90cd742ec554":
                # Считываем из MQTT текущее состояние (bool)
                is_on = read_mqtt_state(
                    mqtt_topics["light_bedroom"].full, mqtt_host="localhost"
                )
                devices_response.append(
                    {
                        "id": device_id,
                        "capabilities": [
                            {
                                "type": "devices.capabilities.on_off",
                                "state": {"instance": "on", "value": is_on},
                            }
                        ],
                    }
                )

            elif device_id == "7065fcf8-963e-42a2-a4f5-e668e412d1c4":
                # Считываем из MQTT float-значение температуры
                temp_topic = mqtt_topics["temperature_corridor"].full
                # temp_topic = "/devices/vd-water-meter-1/controls/litres_used_value"

                temperature_value = 0.0
                try:
                    msg = subscribe.simple(
                        temp_topic, hostname="localhost", retained=True
                    )
                    temperature_value = float(msg.payload.decode().strip())
                    logger.info(f"[INFO] Read temperature: {temperature_value}")
                except Exception as e:
                    logger.info(f"[WARN] Could not read temperature: {e}")

                devices_response.append(
                    {
                        "id": device_id,
                        "properties": [
                            {
                                "type": "devices.properties.float",
                                "state": {
                                    "instance": "temperature",
                                    "value": temperature_value,
                                },
                            }
                        ],
                    }
                )

            else:
                # Если Алиса запросила устройство, о котором мы ничего не знаем
                logger.info(f"[WARN] Unknown device id={device_id} requested.")
                devices_response.append(
                    {"id": device_id, "error_code": "DEVICE_NOT_FOUND"}
                )

        query_response = {
            "requestId": request_id,
            "payload": {"devices": devices_response},
        }
        return query_response

    @sio.on("alice_devices_action")
    async def on_alice_devices_action(data):
        """
        Обработка действия над устройством (включить/выключить).
        Меняем состояние в LIGHT_STATE, возвращаем результат.
        """
        logger.info("[SOCKET.IO] alice_devices_action event:")
        logger.info(json.dumps(data, ensure_ascii=False, indent=2))

        request_id = data.get("requestId", "unknown")

        # May be "f52856d1-4fca-40ce-8ee3-e5ed37298d6e" or "67a2d9b9-666b-41e1-957e-90cd742ec554"
        device_id = data.get("payload", {}).get("devices", [])[0].get("id")

        for device in data.get("payload", {}).get("devices", []):
            if device.get("id") == "f52856d1-4fca-40ce-8ee3-e5ed37298d6e":
                for capability in device.get("capabilities", []):
                    if capability.get("type") == "devices.capabilities.on_off":
                        value = capability.get("state", {}).get("value")
                        if value is not None:
                            # value True -> "1", False -> "0"
                            write_mqtt_state(
                                mqtt_client, mqtt_topics["light_corridor"].full, value
                            )
                            logger.info(
                                f"Set device '{device_id}' to {value} (MQTT published)"
                            )
            if device.get("id") == "67a2d9b9-666b-41e1-957e-90cd742ec554":
                for capability in device.get("capabilities", []):
                    if capability.get("type") == "devices.capabilities.on_off":
                        value = capability.get("state", {}).get("value")
                        if value is not None:
                            # value True -> "1", False -> "0"
                            write_mqtt_state(
                                mqtt_client, mqtt_topics["light_bedroom"].full, value
                            )
                            logger.info(
                                f"Set device '{device_id}' to {value} (MQTT published)"
                            )

        action_response = {
            "requestId": request_id,
            "payload": {
                "devices": [
                    {
                        "id": device_id,
                        "capabilities": [
                            {
                                "type": "devices.capabilities.on_off",
                                "state": {
                                    "instance": "on",
                                    "action_result": {"status": "DONE"},
                                },
                            }
                        ],
                        "action_result": {"status": "DONE"},
                    }
                ]
            },
        }
        return action_response

    try:
        # Connect to server and keep connection active
        # Pass controller_sn via custom header
        await sio.connect(
            server_url,
            socketio_path="/socket.io",
            headers={"X-Controller-SN": controller_sn},
        )
        await sio.wait()

    except socketio.exceptions.ConnectionError as e:
        logger.info(f"[ERR] Socket.IO connection error: {e}")
        logger.info(
            "[ERR] Unable to connect. The controller might have been unregistered."
        )
    except Exception as e:
        logger.info(f"[ERR] Exception while connecting to server: {e}")
    finally:
        if sio.connected:
            await sio.disconnect()
        # Stop MQTT client loop
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        logger.info("[INFO] MQTT disconnected")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[MAIN] Выполнение прерывано пользователем (Ctrl+C)")
