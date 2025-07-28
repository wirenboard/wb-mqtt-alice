import re
import hashlib
import json
import uuid
import logging
import subprocess
import requests
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Request, HTTPException

from fetch_url import fetch_url
from models import Room, Capability, Property, Device, RoomID, Config

app = FastAPI(
    title="Alice Integration API",
    version="1.0.0",
)

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s',
    force=True)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)


SHORT_SN_PATH = "/var/lib/wirenboard/short_sn.conf"
BOARD_REVISION_PATH = "/proc/device-tree/wirenboard/board-revision"
CONFIG_PATH = "/etc/wb-alice-devices.conf"
SETTING_PATH = "/etc/wb-alice-setting.conf"
CLIENT_CONFIG_PATH = "/etc/wb-alice-client.conf"
CLIENT_SERVICE_NAME = "wb-alice-client"
DEFAULT_LANGUAGE = "en"
DEFAULT_CONFIG = {
    "rooms": {
        "without_rooms": {
            "name": "Без комнаты",
            "devices": []
        }
    },
    "devices": {},
    "link_url": None,
    "unlink_url": None
}

# Global variables (will be initialized in init_globals())
controller_sn = None
controller_version = None
key_id = None
config = None
server_address = None
translations = None


def init_globals():
    """Initialize global variables"""

    try:
        global controller_sn, controller_version, key_id, config, server_address, setting, translations
        
        controller_sn = get_controller_sn()
        controller_version = get_board_revision()
        key_id = get_key_id(controller_version)
        config = load_config()
        server_address = load_client_config()['server_address']
        setting = load_setting()
        translations = setting.get("translations", {})
    except Exception as e:
        logger.critical(f"Failed to initialize global variables: {e}")
        raise


def get_controller_sn():
    """Get controller SN from the configuration file"""

    logger.debug(f"Reading controller SN...")
    try:
        with open(SHORT_SN_PATH, "r") as file:
            controller_sn = file.read().strip()
            logger.debug(f"Сontroller SN: {controller_sn}")
            return controller_sn
    except FileNotFoundError:
        logger.error(f"Controller SN file not found! Check the path: {SHORT_SN_PATH}")
        return None
    except Exception as e:
        logger.error(f"Error reading controller SN: {e}")
        return None


def get_board_revision():
    """Read the controller hardware revision (board-revision) from Device Tree."""

    logger.debug(f"Reading controller hardware revision...")
    try:
        with open(BOARD_REVISION_PATH, "r") as file:
            board_revision = '.'.join(file.read().rstrip('\x00').split('.')[:2])
            logger.debug(f"Сontroller hardware revision: {board_revision}")
            return board_revision
    except FileNotFoundError:
        logger.error(f"Controller board revition file not found! Check the path: {BOARD_REVISION_PATH}")
        return None
    except Exception as e:
        logger.error(f"Error reading controller board revition: {e}")
        return None


def get_key_id(controller_version: str) -> str:
    """Determine the appropriate key ID based on controller version"""
    min_version = [7, 0]
    try:
        version_parts = list(map(int, controller_version.split('.')[:2]))
        return (
            "ATECCx08:00:02:C0:00" 
            if version_parts >= min_version 
            else "ATECCx08:00:04:C0:00"
        )
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid controller version format: {controller_version}") from e


def load_config() -> Config:
    """Load configurations from file"""
    
    logger.debug(f"Reading configuration file...")

    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = Config(**json.load(f))
        return config
    except Exception as e:
        config = Config(**DEFAULT_CONFIG)
        save_config(config)
        logger.error(f"Error reading configuration file: {e}")
        return config


def save_config(config: Config):
    """Save configuration file"""

    logger.debug(f"Saving configuration file...")
    
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config.dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saveing configuration file: {e}")
        raise

    restart_service(CLIENT_SERVICE_NAME)


def load_client_config():
    """Load client configuration file"""
    
    logger.debug(f"Reading client configuration file...")

    try:
        with open(CLIENT_CONFIG_PATH, 'r', encoding='utf-8') as f:
            client_config = json.load(f)
        return client_config
    except Exception as e:
        logger.error(f"Error reading client configuration file: {e}")
        raise


def load_setting():
    """Load settings from file"""
    
    logger.debug(f"Reading settings file...")

    try:
        with open(SETTING_PATH, 'r', encoding='utf-8') as f:
            setting = json.load(f)
        return setting
    except Exception as e:
        logger.error(f"Error reading settings file: {e}")
        raise


def get_language(request: Request) -> str:
    """Get language from request with fallback to default"""
    if hasattr(request.state, "language"):
        return request.state.language
    return DEFAULT_LANGUAGE


def get_translation(key: str, language: str = None) -> str:
    """Get translation for a key with fallback logic"""
    if not language:
        language = DEFAULT_LANGUAGE  # Default language
    
    # Try exact match first (e.g. "ru-RU")
    if language in translations:
        return translations[language].get(key, key)
    
    # Try primary language (e.g. "ru" from "ru-RU")
    primary_lang = language.split("-")[0]
    if primary_lang in translations:
        return translations[primary_lang].get(key, key)
    
    # Fallback to English
    return translations.get(DEFAULT_LANGUAGE, {}).get(key, key)


def is_service_active(CLIENT_SERVICE_NAME):
    result = subprocess.run(["systemctl", "is-active", CLIENT_SERVICE_NAME],
        capture_output=True,
        text=True)
    return result.stdout.strip() == "active"


def restart_service(CLIENT_SERVICE_NAME: str):
    if not is_service_active(CLIENT_SERVICE_NAME):
        logger.info(f"'{CLIENT_SERVICE_NAME}' service not started")
        return
    
    try:
        subprocess.run(["systemctl", "restart", CLIENT_SERVICE_NAME],
            check=True)
        logger.info(f"'{CLIENT_SERVICE_NAME}' service restart...")
    except subprocess.CalledProcessError as e:
        logger.info(f"'{CLIENT_SERVICE_NAME}' service restart error")
        return


def generate_id(controller_sn):
    hashSN = hashlib.sha256(controller_sn.encode()).hexdigest()[:8]
    timestamp = datetime.now().strftime("%y%m%d%H%M%S")
    unique_id = str(uuid.uuid4())
    return f"{hashSN.lower()}-{timestamp}-{unique_id}"


def move_device_to_room(device_id, room_id, config):
    old_room_id = config.devices[device_id].room_id
    if room_id != old_room_id:
        config.rooms[old_room_id].devices.remove(device_id)
        config.rooms[room_id].devices.append(device_id)
    return 


# Data validation functions

def validate_room_name_unique(name: str, rooms: dict, language: str) -> None:
    """Validate that room name is unique"""
    if any(room.name == name for room in rooms.values()):
        raise HTTPException(
            status_code=409,
            detail=get_translation("room_exists", language))


def validate_room_exists(room_id: str, config: Config, language: str) -> None:
    """Validate that room with given ID exists"""
    if room_id not in config.rooms:
        raise HTTPException(
            status_code=404,
            detail=get_translation("no_room_id", language))


def validate_room_name(name: str, language: str) -> None:
    """Validate room name according to requirements"""

    if not re.fullmatch(r'^[а-яА-ЯёЁ0-9]+( [а-яА-ЯёЁ0-9]+)*$', name):
        raise HTTPException(
            status_code=422,
            detail=get_translation("room_name_invalid_chars", language))

    if re.search(r'[а-яА-ЯёЁ][0-9]|[0-9][а-яА-ЯёЁ]', name):
        raise HTTPException(
            status_code=422,
            detail=get_translation("room_name_missing_spaces", language))

    if len(name) > 20:
        raise HTTPException(
            status_code=422,
            detail=get_translation("room_name_too_long", language))

    if len(re.sub(r'[^а-яА-ЯёЁ]', '', name)) < 2:
        raise HTTPException(
            status_code=422,
            detail=get_translation("room_name_too_few_letters", language))


def validate_device_name_unique(name: str, room_id: str, devices: dict, language: str) -> None:
    """Validate that device name is unique"""
    if any((device.name == name)&(device.room_id == room_id) for device in devices.values()):
        raise HTTPException(
            status_code=409,
            detail=get_translation("device_exists", language))


def validate_device_exists(device_id: str, config: Config, language: str) -> None:
    """Validate that device with given ID exists"""
    if device_id not in config.devices:
        raise HTTPException(
            status_code=404,
            detail=get_translation("no_device_id", language))


def validate_device_name(name: str, language: str) -> None:
    """Validate device name according to requirements"""

    if not re.fullmatch(r'^[а-яА-ЯёЁ0-9]+( [а-яА-ЯёЁ0-9]+)*$', name):
        raise HTTPException(
            status_code=422,
            detail=get_translation("device_name_invalid_chars", language))


    if re.search(r'[а-яА-ЯёЁ][0-9]|[0-9][а-яА-ЯёЁ]', name):
        raise HTTPException(
            status_code=422,
            detail=get_translation("device_name_missing_spaces", language))

    if len(name) > 25:
        raise HTTPException(
            status_code=422,
            detail=get_translation("device_name_too_long", language))

    if len(re.sub(r'[^а-яА-ЯёЁ]', '', name)) < 2:
        raise HTTPException(
            status_code=422,
            detail=get_translation("device_name_too_few_letters", language))


def validate_device_not_empty(device_data: Device, language: str) -> None:
    """Validate that device has at least one capability or property"""
    if not device_data.capabilities and not device_data.properties:
        raise HTTPException(
            status_code=400,
            detail=get_translation("empty_device", language))


def validate_capabilities(capabilities: list[Capability], language: str) -> None:
    """Validate and prepare device capabilities"""
    for capability in capabilities:
        if capability.mqtt == "":
            raise HTTPException(
                status_code=422,
                detail=get_translation("empty_mqtt", language))
        
        if capability.type == "devices.capabilities.on_off":
            capability.parameters["instance"] = "on"
        elif capability.type == "devices.capabilities.color_setting":
            capability.parameters["instance"] = capability.parameters["color_model"]


def validate_properties(properties: list[Property], language: str) -> None:
    """Validate and prepare device properties"""
    for property in properties:
        if property.mqtt == "":
            raise HTTPException(
                status_code=422,
                detail=get_translation("empty_mqtt", language))


@app.middleware("http")
async def language_middleware(request: Request, call_next):
    accept_language = request.headers.get("accept-language", DEFAULT_LANGUAGE)
    primary_language = accept_language.split(",")[0].split("-")[0].lower()
    request.state.language = primary_language
    response = await call_next(request)
    return response


# API Endpoints

@app.get("/integrations/alice", response_model=Config, status_code=200)
async def get_all_rooms_and_devices():
    """Get all the rooms and devices"""
    
    config = load_config()
    
    try:
        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
            )
        if response["data"] and "registration_url" in response["data"]:
            config.link_url = response["data"]["registration_url"]
            config.unlink_url = None
        elif response["data"]["detail"]:
            config.link_url = None
            config.unlink_url = f"https://{server_address.split(':')[0]}"
    except Exception as e:
        logger.error(f"Failed to fetch registration URL: {str(e)}")
        config.link_url = None
        config.unlink_url = f"https://{server_address.split(':')[0]}"
    
    save_config(config)
    return config


@app.get("/integrations/alice/available", status_code=200)
async def get_status():
    """Get status Alice intagrations"""
    
    return True


@app.post("/integrations/alice/room", status_code=201)
async def create_room(request: Request, room_data: Room):
    """Create new room"""

    language = get_language(request)
    config = load_config()
    # Validate room name
    validate_room_name(room_data.name, language)
    # Validate room name is unique
    validate_room_name_unique(room_data.name, config.rooms, language)
    # Create room
    room_id = generate_id(controller_sn)
    config.rooms[room_id] = room_data
    response = room_data.post_response(room_id)
    
    save_config(config)
    return response


@app.put("/integrations/alice/room/{room_id}", status_code=200)
async def update_room(request: Request, room_id: str, room_data: Room):
    """Update room"""

    language = get_language(request)
    config = load_config()
    # Validate room exists
    validate_room_exists(room_id, config, language)
    # Exclude current room
    other_rooms = {
        k: v for k, v in config.rooms.items() 
        if k != room_id
    }
    # Validate room name is unique
    validate_room_name_unique(room_data.name, config.rooms, language)
    # Update room
    response = room_data.put_response()
    config.rooms[room_id] = response
    
    save_config(config)
    return response


@app.delete("/integrations/alice/room/{room_id}", status_code=200)
async def delete_room(request: Request, room_id: str):
    """Delete room"""

    language = get_language(request)
    config = load_config()
    # Don't allow deleting "without_rooms" special room
    if room_id == "without_rooms":
        raise HTTPException(
            status_code=409,
            detail=get_translation("special_room", language))

    # Validate room exists
    validate_room_exists(room_id, config, language)
    # Delete room
    devices_to_move = config.rooms[room_id].devices.copy()
    for device_id in devices_to_move:
        if device_id in config.devices:
            config.devices[device_id].room_id = "without_rooms"
    config.rooms["without_rooms"].devices.extend(devices_to_move)
    del config.rooms[room_id]
    
    save_config(config)
    return {"message": get_translation("room_deleted", language)}


@app.post("/integrations/alice/device", status_code=201)
async def create_device(request: Request, device_data: Device):
    """Create new device"""

    language = get_language(request)
    config = load_config()
    # Validate device name
    validate_device_name(device_data.name, language)
    # Validate device name is unique
    validate_device_name_unique(
        device_data.name,
        device_data.room_id,
        config.devices,
        language)
    # Check if the device has a capability or property
    validate_device_not_empty(device_data, language)
    # Validate and prepare capabilities
    validate_capabilities(device_data.capabilities, language)
    # Validate and prepare properties
    validate_properties(device_data.properties, language)
    # Create device
    device_id = generate_id(controller_sn)
    response = device_data.post_response(device_id)
    config.devices[device_id] = device_data
    config.rooms[device_data.room_id].devices.append(device_id)
    
    save_config(config)
    return response


@app.put("/integrations/alice/device/{device_id}", status_code=200)
async def update_device(request: Request, device_id: str, device_data: Device):
    """Update device"""

    language = get_language(request)
    config = load_config()
    # Validate device name
    validate_device_name(device_data.name, language)
    # Validate device exists
    validate_device_exists(device_id, config, language)
    # Validate room exists
    validate_room_exists(device_data.room_id, config, language)
    # Check if the device has a capability or property
    validate_device_not_empty(device_data, language)
    # Validate and prepare capabilities
    validate_capabilities(device_data.capabilities, language)
    # Validate and prepare properties
    validate_properties(device_data.properties, language)
    # Update device
    response = device_data
    move_device_to_room(device_id, device_data.room_id, config)
    config.devices[device_id] = response
    
    save_config(config)
    return response


@app.delete("/integrations/alice/device/{device_id}", status_code=200)
async def delete_device(request: Request, device_id: str):
    """Delete device"""

    language = get_language(request)
    config = load_config()
    # Validate device exists
    validate_device_exists(device_id, config, language)
    # Delete device
    del_room_id = config.devices[device_id].room_id
    del config.devices[device_id]
    # Update room
    config.rooms[del_room_id].devices.remove(device_id)
        
    save_config(config)
    return {"message": get_translation("device_deleted", language)}


@app.put("/integrations/alice/device/{device_id}/room", response_model=RoomID, status_code=200)
async def change_device_room(request: Request, device_id: str, device_data: RoomID):
    """Changes the room for the device"""
    
    language = get_language(request)
    config = load_config()
    # Validate device exists
    validate_device_exists(device_id, config, language)
    # Validate room exists
    validate_room_exists(device_data.room_id, config, language)
    # Change device room
    move_device_to_room(device_id, device_data.room_id, config)
    config.devices[device_id].room_id = device_data.room_id
    response = RoomID(room_id=device_data.room_id)
    
    save_config(config)
    return response


# Initialize global variables at startup
init_globals()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)
