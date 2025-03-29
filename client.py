import asyncio
import json
import os

import socketio

SHORT_SN_PATH = "/var/lib/wirenboard/short_sn.conf"
CONFIG_PATH = "/etc/wb-alice-client.conf"


def get_controller_sn():
    """Get controller SN from the configuration file"""
    try:
        with open(SHORT_SN_PATH, "r") as file:
            controller_sn = file.read().strip()
            print(f"[INFO] Read controller SN: {controller_sn}")
            return controller_sn
    except FileNotFoundError:
        print(f"[ERR] Controller SN file not found! Check the path: {SHORT_SN_PATH}")
        return None
    except Exception as e:
        print(f"[ERR] Reading controller SN exception: {e}")
        return None


def read_config():
    """Read configuration file"""
    try:
        if not os.path.exists(CONFIG_PATH):
            print(f"[ERR] Configuration file not found at {CONFIG_PATH}")
            return None

        with open(CONFIG_PATH, "r") as file:
            config = json.load(file)
            return config
    except json.JSONDecodeError:
        print("[ERR] Parsing configuration file: Invalid JSON format")
        return None
    except Exception as e:
        print(f"[ERR] Reading configuration exception: {e}")
        return None


async def connect_controller():
    config = read_config()
    if not config:
        print("[ERR] Cannot proceed without configuration")
        return

    # TODO(vg): On this moment this parameter hardcoded - must set after
    #           register controller on web server automatically
    if not config.get("is_registered", False):
        print(
            "[ERR] Controller is not registered. Please register the controller first."
        )
        return

    controller_sn = get_controller_sn()
    if not controller_sn:
        print("[ERR] Cannot proceed without controller SN")
        return

    server_domain = config.get("server_domain")
    if not server_domain:
        print("[ERR] Server domain not specified in configuration")
        return

    server_url = f"https://{server_domain}"
    print(f"[INFO] Connecting to server: {server_url}")

    sio = socketio.AsyncClient()

    @sio.event
    async def connect():
        print("[SUCCESS] Connected to Socket.IO server!")
        # Send message to server about owr controller serial number
        await sio.emit("message", {"controller_sn": controller_sn, "status": "online"})

    @sio.event
    async def disconnect():
        print("[ERR] Disconnected from server")

    @sio.event
    async def response(data):
        print(f"[INCOME] Server response: {data}")

    @sio.event
    async def error(data):
        print(f"[ERR] Server error: {data}")
        print("[ERR] Terminating connection due to server error")
        await sio.disconnect()

    try:
        # Connect to server and keep connection active
        await sio.connect(server_url, socketio_path="/socket.io")
        await sio.wait()
    except socketio.exceptions.ConnectionError as e:
        print(f"[ERR] Connection error: {e}")
        print("[ERR] Unable to connect. The controller might have been unregistered.")
    except Exception as e:
        print(f"[ERR] Get exception when try connect: {e}")
    finally:
        if sio.connected:
            await sio.disconnect()


if __name__ == "__main__":
    asyncio.run(connect_controller())
