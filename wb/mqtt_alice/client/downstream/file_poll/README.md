# file_poll

Tiny polling adapter that reads values from a JSON file.

Intended for:
  - quick local testing without MQTT
  - unit/integration tests with deterministic input

The file is expected to contain a JSON object (dict).
Each configured key is treated as an "address".

Example file:

```json
{ "t": 23.5, "relay": 1 }
```
