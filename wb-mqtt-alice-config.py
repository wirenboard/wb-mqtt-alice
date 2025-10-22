import asyncio
import hashlib
import json
import logging
import re
import subprocess
import uuid
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from constants import CAP_COLOR_SETTING, CLIENT_CONFIG_PATH
from fetch_url import fetch_url
from models import Capability, Config, Device, Property, Room, RoomID
from wb_mqtt_load_config import get_board_revision, get_key_id, load_client_config

# FastAPI initialization
app = FastAPI(
    title="Alice Integration API",
    version="1.0.0",
)

# Setting up the logger
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logging.captureWarnings(True)
logger = logging.getLogger(__name__)

# Constants
SHORT_SN_PATH = Path("/var/lib/wirenboard/short_sn.conf")
BOARD_REVISION_PATH = Path("/proc/device-tree/wirenboard/board-revision")
BOARD_MODEL_PATH = Path("/proc/device-tree/model")
DEVICES_CONFIG_PATH = Path("/etc/wb-mqtt-alice-devices.conf")
SETTING_PATH = Path("/usr/lib/wb-mqtt-alice/wb-mqtt-alice-webui.conf")
CLIENT_SERVICE_NAME = "wb-mqtt-alice-client"
DEFAULT_LANGUAGE = "en"
DEFAULT_CONFIG = {
    "rooms": {"without_rooms": {"name": "Без комнаты", "devices": []}},
    "devices": {},
    "link_url": None,
    "unlink_url": None,
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
        
        client_cfg = load_client_config()
        server_address = client_cfg.get("server_address")
        if not server_address:
            raise ValueError("Missing 'server_address' in client config")

        setting = load_setting()
        translations = setting.get("translations", {})
    except Exception as e:
        logger.critical("Failed to initialize global variables: %r", e)
        raise


def get_controller_sn():
    """Get controller SN from the configuration file"""

    logger.debug("Reading controller SN...")
    try:
        controller_sn = SHORT_SN_PATH.read_text().strip()
        logger.debug("Сontroller SN: %r", controller_sn)
        return controller_sn
    except FileNotFoundError:
        logger.error("Controller SN file not found! Check the path: %r", SHORT_SN_PATH)
        return None
    except Exception as e:
        logger.error("Error reading controller SN: %r", e)
        return None


def load_config() -> Config:
    """Load configurations from file"""

    logger.debug("Reading configuration file...")
    try:
        config = Config(**json.loads(DEVICES_CONFIG_PATH.read_text(encoding="utf-8")))
        return config
    except Exception as e:
        config = Config(**DEFAULT_CONFIG)
        save_devices_config(config)
        logger.error("Error reading configuration file: %r", e)
        return config


def save_devices_config(config: Config) -> None:
    """Save yandex devices configuration to file"""
    logger.debug("Saving yandex devices configuration file...")
    try:
        DEVICES_CONFIG_PATH.write_text(
            json.dumps(config.dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.error("Error saving yandex devices configuration file: %r", e)
        raise


def finalize_config_change(config: Config, *, force_client_reload: bool = False) -> None:
    """
    Compact helper to finalize config changes (Save → Sync → Optional Restart)

    force_client_reload:
      - True: restart client unconditionally (still syncing status)
            This needed when backend changed device config, but not needed
            if we load configurator main page or create new empty room
      - False: restart only if sync_client_enabled_status() has changed state
            This needed when not need restart client for load new dev-config
    """
    save_devices_config(config)

    # TODO(vg): Currently, the controller binding status (registration/linking)
    #           is only re-evaluated when configuration operations occur.
    #           This approach works but may skip updates if the controller
    #           state changes independently. A more robust event-based sync
    #           mechanism will be introduced in future revisions.
    try:
        status_changed = sync_client_enabled_status(config)
    except Exception:
        logger.exception("Failed to sync client enabled status")
        status_changed = False

    if force_client_reload or status_changed:
        force_client_reload_config()
    else:
        logger.debug(
            "No restart needed (force_client_reload=%s, status_changed=%s)",
            force_client_reload, status_changed
        )


def sync_client_enabled_status(config: Config) -> bool:
    """
    Synchronize client enabled status based on current config state

    Returns:
        bool: True if status was changed and client needs restart

    Example:
        - No devices, registered: False
        - Has devices, not registered: False  
        - Has devices, registered: True
    """
    client_config = load_client_config()

    old_status = client_config.get("client_enabled")
    new_status = should_enable_client(config)
    if old_status == new_status:
        return False  # No need to sync client "enabled" state

    client_config["client_enabled"] = new_status
    try:
        Path(CLIENT_CONFIG_PATH).write_text(
            json.dumps(client_config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Error saving yandex devices configuration file: %r", e)
        raise

    logger.info(
        "Client status changed: %s -> %s (devices: %d, registered: %s)",
        old_status, new_status, 
        len(config.devices), 
        bool(config.unlink_url)
    )
    return True


def force_client_reload_config() -> None:
    """
    Force client to reload configuration by restarting the service
    This function schedules an asynchronous service restart to apply
    configuration changes. The restart is non-blocking

    TODO: This is a temporary solution that requires service restart
          Need refactor to use a signal/message-based approach (Unix socket)
          to notify the running client about config changes without restart
    """
    logger.debug("Forcing client to reload configuration...")
    asyncio.create_task(async_restart_service(CLIENT_SERVICE_NAME))


def load_setting():
    """Load settings from file"""

    logger.debug("Reading settings file...")
    try:
        setting = json.loads(SETTING_PATH.read_text(encoding="utf-8"))
        return setting
    except Exception as e:
        logger.error("Error reading settings file: %r", e)
        raise


def get_language(request: Request) -> str:
    """Get language from request with fallback to default"""
    return getattr(request.state, "language", DEFAULT_LANGUAGE)


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
    result = subprocess.run(["systemctl", "is-active", CLIENT_SERVICE_NAME], capture_output=True, text=True)
    return result.stdout.strip() == "active"


async def async_restart_service(service_name: str):
    logger.debug("Attempting to restart service: %r", service_name)

    if not is_service_active(service_name):
        logger.info("%r service not active, starting...", service_name)
        action = "start"
    else:
        action = "restart"

    try:
        await asyncio.create_subprocess_exec("systemctl", action, service_name)
        logger.info("%r service %s...", service_name, action)
    except subprocess.CalledProcessError as e:
        logger.error("%r service %s error", service_name, action)
        return None


def generate_id(controller_sn):
    hashSN = hashlib.sha256(controller_sn.encode()).hexdigest()[:8]
    timestamp = datetime.now().strftime("%y%m%d%H%M%S")
    unique_id = uuid.uuid4()
    return f"{hashSN.lower()}-{timestamp}-{unique_id}"


def move_device_to_room(device_id, room_id, config):
    old_room_id = config.devices[device_id].room_id
    if room_id != old_room_id:
        config.rooms[old_room_id].devices.remove(device_id)
        config.rooms[room_id].devices.append(device_id)
    return None


def should_enable_client(config: Config) -> bool:
    """
    Check the minimum conditions for enabling the client

    Both conditions must be met for client to be enabled:
    - Has at least one device configured
    - Controller is registered (unlink_url is set)
    
    Returns:
        - True if client should be enabled
        - False otherwise
    """
    devices_qty = len(config.devices)
    has_devices = devices_qty > 0
    is_registered = bool(config.unlink_url)

    if not has_devices:
        logger.debug("Client should be disabled: no devices configured")
        return False
    
    if not is_registered:
        logger.debug("Client should be disabled: controller not registered (no unlink_url)")
        return False

    logger.debug(
        "Client should be enabled: %d device(s) configured, controller registered",
        devices_qty
    )
    return True


def sync_registration_status(config: Config) -> Config:
    """
    Synchronize controller registration status with remote server
    
    Updates config.link_url and config.unlink_url based on current registration state:
    - If not registered: sets link_url (registration URL)
    - If registered: sets unlink_url (server base URL)
    
    Args:
        config: Current configuration object
        
    Returns:
        Updated configuration object with actual registration URLs
    """
    logger.debug("Synchronizing registration status with server...")
    
    try:
        response = fetch_url(
            url=f"https://{server_address}/request-registration",
            data={"controller_version": f"{controller_version}"},
            key_id=key_id,
        )

        if response["data"] and "registration_url" in response["data"]:
            # Controller is not registered - provide registration link
            config.link_url = response["data"]["registration_url"]
            config.unlink_url = None
            logger.debug("Controller not registered, link_url updated")
        elif response["data"]["detail"]:
            # Controller is registered - provide unlink capability
            config.link_url = None
            config.unlink_url = f"https://{server_address.split(':')[0]}"
            logger.debug("Controller registered, unlink_url updated")

    except Exception as e:
        logger.error("Failed to fetch registration URL: %r", e)
        # On error, assume registered and provide unlink URL
        config.link_url = None
        config.unlink_url = f"https://{server_address.split(':')[0]}"

    return config


async def restore_client_status_if_needed(config: Config) -> None:
    """
    Restore client enabled status based on current configuration state
    
    Args:
        config: Configuration object with actual registration status
    """
    logger.info("Checking if client status needs restoration...")
    
    try:
        status_changed = sync_client_enabled_status(config)
        
        if status_changed:
            logger.info("Client status restored on startup")
            
            # Start client service if it should be enabled
            # Service may stop long time - need do it non-blocking
            if should_enable_client(config):
                asyncio.create_task(async_restart_service(CLIENT_SERVICE_NAME))
        else:
            logger.debug("Client status is already correct, no restoration needed")
            
    except Exception as e:
        logger.error("Failed to restore client status: %r", e)
        # Don't raise - this is a recovery mechanism, shouldn't break startup


# Data validation functions


def validate_room_name_unique(name: str, rooms: dict, language: str) -> None:
    """Validate that room name is unique"""
    if any(room.name == name for room in rooms.values()):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=get_translation("room_exists", language),
        )


def validate_room_exists(room_id: str, config: Config, language: str) -> None:
    """Validate that room with given ID exists"""
    if room_id not in config.rooms:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=get_translation("no_room_id", language),
        )


def validate_room_name(name: str, language: str) -> None:
    """Validate room name according to requirements"""

    if not re.fullmatch(r"^[а-яА-ЯёЁ0-9]+( [а-яА-ЯёЁ0-9]+)*$", name):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("room_name_invalid_chars", language),
        )

    if re.search(r"[а-яА-ЯёЁ][0-9]|[0-9][а-яА-ЯёЁ]", name):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("room_name_missing_spaces", language),
        )

    if len(name) > 20:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("room_name_too_long", language),
        )

    if len(re.sub(r"[^а-яА-ЯёЁ]", "", name)) < 2:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("room_name_too_few_letters", language),
        )


def validate_device_name_unique(name: str, room_id: str, devices: dict, language: str) -> None:
    """Validate that device name is unique"""
    if any(device.name == name and device.room_id == room_id for device in devices.values()):
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=get_translation("device_exists", language),
        )


def validate_device_exists(device_id: str, config: Config, language: str) -> None:
    """Validate that device with given ID exists"""
    if device_id not in config.devices:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=get_translation("no_device_id", language),
        )


def validate_device_name(name: str, language: str) -> None:
    """Validate device name according to requirements"""

    if not re.fullmatch(r"^[а-яА-ЯёЁ0-9]+( [а-яА-ЯёЁ0-9]+)*$", name):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("device_name_invalid_chars", language),
        )

    if re.search(r"[а-яА-ЯёЁ][0-9]|[0-9][а-яА-ЯёЁ]", name):
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("device_name_missing_spaces", language),
        )

    if len(name) > 25:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("device_name_too_long", language),
        )

    if len(re.sub(r"[^а-яА-ЯёЁ]", "", name)) < 2:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=get_translation("device_name_too_few_letters", language),
        )


def validate_device_not_empty(device_data: Device, language: str) -> None:
    """Validate that device has at least one capability or property"""
    if not device_data.capabilities and not device_data.properties:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=get_translation("empty_device", language),
        )


def validate_capabilities(capabilities: list[Capability], language: str) -> None:
    """Validate and prepare device capabilities"""
    for capability in capabilities:
        if capability.mqtt == "":
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=get_translation("empty_mqtt", language),
            )

        # Validate only specific "instance" whithh user can setup from frontend
        # Other structure frontend MUST send correctly
        if capability.type == CAP_COLOR_SETTING:
            params = capability.parameters or {}

            has_color_model = isinstance(params.get("color_model"), str)
            has_temp = isinstance(params.get("temperature_k"), dict)
            has_scene = isinstance(params.get("color_scene"), dict)

            # For current implementation we must get only one instance
            if (has_color_model + has_temp + has_scene) != 1:
                raise HTTPException(
                    status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                    detail=get_translation("invalid_color_setting", language),
                )


def validate_properties(properties: list[Property], language: str) -> None:
    """Validate and prepare device properties"""
    for property in properties:
        if property.mqtt == "":
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=get_translation("empty_mqtt", language),
            )


@app.middleware("http")
async def language_middleware(request: Request, call_next):
    accept_language = request.headers.get("accept-language", DEFAULT_LANGUAGE)
    primary_language = accept_language.split(",")[0].split("-")[0].lower()
    request.state.language = primary_language
    response = await call_next(request)
    return response


# API Endpoints


@app.get("/integrations/alice", response_model=Config, status_code=HTTPStatus.OK)
async def get_all_rooms_and_devices():
    """Get all the rooms and devices"""

    config = load_config()
    config = sync_registration_status(config)

    # Don't force client reload because this doesn't change devices
    finalize_config_change(config, force_client_reload=False)

    return config


@app.get("/integrations/alice/available", status_code=HTTPStatus.OK)
async def get_status():
    """Get status Alice intagrations"""

    return True


@app.post("/integrations/alice/room", status_code=HTTPStatus.CREATED)
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

    # Don't force client reload because new room is empty
    finalize_config_change(config, force_client_reload=False)

    return response


@app.put("/integrations/alice/room/{room_id}", status_code=HTTPStatus.OK)
async def update_room(request: Request, room_id: str, room_data: Room):
    """Update room"""

    language = get_language(request)
    config = load_config()
    # Validate room exists
    validate_room_exists(room_id, config, language)
    # Exclude current room
    other_rooms = {k: v for k, v in config.rooms.items() if k != room_id}
    # Validate room name is unique
    validate_room_name_unique(room_data.name, config.rooms, language)
    # Update room
    response = room_data.put_response()
    config.rooms[room_id] = response

    # Always sync status and restart client when device config changes
    finalize_config_change(config, force_client_reload=True)

    return response


@app.delete("/integrations/alice/room/{room_id}", status_code=HTTPStatus.OK)
async def delete_room(request: Request, room_id: str):
    """Delete room"""

    language = get_language(request)
    config = load_config()
    # Don't allow deleting "without_rooms" special room
    if room_id == "without_rooms":
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=get_translation("special_room", language),
        )

    # Validate room exists
    validate_room_exists(room_id, config, language)
    # Delete room
    devices_to_move = config.rooms[room_id].devices.copy()
    for device_id in devices_to_move:
        if device_id in config.devices:
            config.devices[device_id].room_id = "without_rooms"
    config.rooms["without_rooms"].devices.extend(devices_to_move)
    del config.rooms[room_id]

    # Always sync status and restart client when device config changes
    finalize_config_change(config, force_client_reload=True)

    return {"message": get_translation("room_deleted", language)}


@app.post("/integrations/alice/device", status_code=HTTPStatus.CREATED)
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
        language,
    )
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

    # Always sync status and restart client when device config changes
    finalize_config_change(config, force_client_reload=True)

    return response


@app.put("/integrations/alice/device/{device_id}", status_code=HTTPStatus.OK)
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

    # Always sync status and restart client when device config changes
    finalize_config_change(config, force_client_reload=True)

    return response


@app.delete("/integrations/alice/device/{device_id}", status_code=HTTPStatus.OK)
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

    # Always sync status and restart client when device config changes
    finalize_config_change(config, force_client_reload=True)

    return {"message": get_translation("device_deleted", language)}


@app.put(
    "/integrations/alice/device/{device_id}/room",
    response_model=RoomID,
    status_code=HTTPStatus.OK,
)
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

    # Always sync status and restart client when device config changes
    finalize_config_change(config, force_client_reload=True)

    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Single, compact JSON-only error handler.
    - Logs the error with a correlation error_id
    - Puts a human-friendly text into detail - this frontend show like error msg:
        hint (if matched) -> short cause -> generic fallback,
      and appends (error_id=...) for fast diagnostics

    How to add new cases:
      - Edit the HINTS list below: append tuples (compiled_regex, "human hint")
      - Keep it short and actionable — это попадёт в detail
    """
    # Known patterns -> human hints (add new tuples here)
    HINTS = [
        # This error when frontend send not correct config with
        (
            re.compile(r"\b(color_model|temperature_k|color_scene|instance)\b", re.IGNORECASE),
            "Frontend must send instance explicitly: "
            "{color_model:'rgb'|'hsv', instance:'rgb'|'hsv'} OR "
            "{temperature_k:{min,max}, instance:'temperature_k'} OR "
            "{color_scene:{scenes:[...]}, instance:'scene'}.",
        ),
        # This error when frontend send not correct config with mqtt field empty in capability root
        (
            re.compile(r"\bempty_mqtt\b|\bmqtt\b.*\b(empty|missing)\b", re.IGNORECASE),
            "Each capability must have a non-empty root 'mqtt' field.",
        ),
    ]

    error_id = str(uuid.uuid4())
    logger.exception(
        "[%r] Unhandled error on %r %r: %r",
        error_id,
        request.method,
        request.url.path,
        exc,
    )

    # Try to find a hint, otherwise show a short cause
    exc_text = f"{type(exc).__name__}: {exc}"
    hint = None
    for pattern, msg in HINTS:
        if pattern.search(exc_text):
            hint = msg
            break

    core = hint or exc_text or "Internal server error"
    detail = f"{core} (error_id={error_id})"

    # Always return JSON for frontend
    return JSONResponse(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        content={
            "detail": detail,
            "message": "Internal server error. See controller logs.",
            "path": str(request.url.path),
            "error_id": error_id,
        },
    )


@app.on_event("startup")
async def startup_event():
    """Application startup tasks"""
    init_globals()
    config = load_config()

    config = sync_registration_status(config)
    finalize_config_change(config, force_client_reload=False)
    
    await restore_client_status_if_needed(config)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8011, log_config=None)
