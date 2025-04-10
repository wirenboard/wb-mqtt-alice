# Информация для разработчиков

Данный файл содержит информацию, полезную при разработке/сопровождении кода.

Далее описаны сборка, ручная установка и приведены другие специфические
сведения, которые могут быть полезны в разработке.

Любые манипуляции нужно начинать с клонирования репозитория:

```terminal
$ git clone git@github.com:wirenboard/wb-alice-client.git
```


## Сборка пакета локально из исходного кода

Устанавливаем зависимости

```terminal
$ sudo apt install dpkg-dev debhelper dh-python
```

Начинаем сборку

```terminal
dpkg-buildpackage -us -uc
```

Получаем актуальный deb пакет.

## Ручная установка из исходного кода

1. Скопировать конфигурационный файл по пути `/etc/wb-alice-client.conf`

   ```terminal
   $ cd /путь/к/репозиторию
   $ cp wb-alice-client.conf /etc/wb-alice-client.conf
   ```

2. Отредактировать файл конфигурации:

   - Вместо "example.com:8000" указать адрес и порт сервера.
   - (Пока не используется) "is_registered": true - оставляем как есть.
   - (Пока не используется) "target_topic": "value" - изменяемый топик
     с полным путем от корня.
     Например, для реле в WB-MR6C v.3: `/devices/wb-mr6cv3_127/controls/K1`

## Ручной запуск на контроллере

1. Установить Python и pip, если их нет:

   ```terminal
   $ apt update && apt install -y python3 python3-pip
   ```

2. Установить зависимости:

   ```terminal
   $ pip install -r requirements.txt
   ```

3. Предварительно перед запуском клиента нужно (пока не обязательно):

   - Если контроллер ещё не зарегистрирован на сервере, зарегистрировать
     его через POST запрос к /users
   - Проверить существование контроллера через REST API запрос к /users

4. Запустим клиент:

   ```terminal
   $ cd /путь/к/репозиторию
   $ python3 wb-alice-client.py
   ```

   В случае успеха мы получим следующий текст в консоли:

   ```terminal
   $ python3 wb-alice-client.py
   [INFO] Readed controller ID: 00000000
   [INFO] Connecting to server: https://example.com:8000
   [SUCCESS] Connected to Socket.IO server!
   [INCOME] Server response: {'data': 'Message received'}
   ```

Приложение само прочитает серийный номер контроллера и отправит его серверу
в первом сообщении после соединения.
