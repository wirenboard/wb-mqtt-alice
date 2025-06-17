import os
import json
import uuid
import subprocess

import uvicorn
from fastapi import FastAPI, HTTPException

from models import Room, Capability, Property, Device, RoomChange, Config

app = FastAPI(
    title="Alice Integration API",
    version="1.0.0",
)

CONFIG_PATH = "/etc/wb-alice-devices.conf"
CLIENT_SERVICE_NAME = "wb-alice-client"


def load_config() -> Config:
    """Load configurations from file"""
    
    try:
        if not os.path.exists(CONFIG_PATH):
            config_default = dict({"rooms":
                                   {"without_rooms": {"name": "Без комнаты","devices": []}},
                                   "devices": {}
                                   })
            config = Config(**config_default)
            save_config(config)
            return config
        
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = Config(**json.load(f))
        return config
    except Exception as e:
        raise


def save_config(config: Config):
    """Save configurations to file"""
    
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config.dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise


def is_service_active(CLIENT_SERVICE_NAME: str):
    result = subprocess.run(["systemctl", "is-active", CLIENT_SERVICE_NAME],
        capture_output=True,
        text=True)
    return result.stdout.strip() == "active"


def restart_service(CLIENT_SERVICE_NAME: str):
    if not is_service_active(CLIENT_SERVICE_NAME):
        return
    
    try:
        subprocess.run(["systemctl", "restart", CLIENT_SERVICE_NAME],
            check=True)
    except subprocess.CalledProcessError as e:
        return


def generate_id():
    return str(uuid.uuid4())


def room_name_exist(name: str, rooms) -> bool:
    return any(room.name == name for room in rooms.values())


def room_change(device_id, room_id, config):
    del_room_id = config.devices[device_id].room_id
    if room_id != del_room_id:
        config.rooms[del_room_id].devices.remove(device_id)
        config.rooms[room_id].devices.append(device_id)
    return 


def device_name_exist(name: str, room_id: str, devices) -> bool:
    return any((device.name == name)&(device.room_id == room_id) for device in devices.values())


# API Endpoints

@app.get("/integrations/alice", response_model=Config, status_code=200)
async def get_all_rooms_and_devices():
    """Get all the rooms and devices"""

    config = load_config()
    return config


@app.post("/integrations/alice/room", status_code=201)
async def create_room(room_data: Room):
    """Create new room"""

    config = load_config()
    # Check if room with given name exists
    if room_name_exist(room_data.name, config.rooms):
        raise HTTPException(
                status_code=409,
                detail="Room with this name already exists")
    # Create room
    room_id = generate_id()
    rooms_dict = config.rooms.copy()
    without_rooms = rooms_dict.pop("without_rooms")
    rooms_dict[room_id] = room_data
    rooms_dict["without_rooms"] = without_rooms
    config.rooms = rooms_dict
    response = room_data.post_response(room_id)
    
    save_config(config)
    return response


@app.put("/integrations/alice/room/{room_id}", status_code=200)
async def update_room(room_id: str, room_data: Room):
    """Update room"""

    config = load_config()
    # Check for the presence of room with given id
    if not room_id in config.rooms:
        raise HTTPException(
            status_code=404,
            detail="There is no room with this ID")
    # Check if room with given name exists
    other_rooms = config.rooms.copy()
    other_rooms.pop(room_id)
    if room_name_exist(room_data.name, other_rooms):
        raise HTTPException(
                status_code=409,
                detail="Room with this name already exists")
    # Update room
    response = room_data.put_response()
    config.rooms[room_id] = response
    
    save_config(config)
    return response


@app.delete("/integrations/alice/room/{room_id}", status_code=200)
async def delete_room(room_id: str):
    """Delete room"""

    config = load_config()
    # Don't allow deleting "without_rooms" special room
    if room_id == "without_rooms":
        raise HTTPException(
            status_code=409,
            detail="Cannot delete special room")
    # Check for the presence of room with given id
    if not room_id in config.rooms:
        raise HTTPException(
            status_code=404,
            detail="There is no room with this ID")
    # Delete room
    devices_to_move = config.rooms[room_id].devices.copy()
    for device_id in devices_to_move:
        if device_id in config.devices:
            config.devices[device_id].room_id = "without_rooms"
    config.rooms["without_rooms"].devices.extend(devices_to_move)
    del config.rooms[room_id]
    
    save_config(config)
    return {"message": "Room deleted successfully"}


@app.post("/integrations/alice/device", status_code=201)
async def create_device(device_data: Device):
    """Create new device"""

    config = load_config()
    # Check for device with given name
    if device_name_exist(device_data.name, device_data.room_id, config.devices):
        raise HTTPException(
            status_code=409,
            detail="Device with this name already exists")
    # Create device
    device_id = generate_id()
    response = device_data.post_response(device_id)
    config.devices[device_id] = device_data
    config.rooms[device_data.room_id].devices.append(device_id)
    
    save_config(config)
    return response


@app.put("/integrations/alice/device/{device_id}", status_code=200)
async def update_device(device_id: str, device_data: Device):
    """Update device"""

    config = load_config()
    # Check for the presence of device with given id
    if not device_id in config.devices:
        raise HTTPException(
            status_code=404,
            detail="There is no device with this ID")
    # Check for the presence of room with given id
    if not device_data.room_id in config.rooms:
        raise HTTPException(
            status_code=404,
            detail="There is no room with this ID")
    # Update device
    response = device_data
    room_change(device_id, device_data.room_id, config)
    config.devices[device_id] = response
    
    save_config(config)
    return response


@app.delete("/integrations/alice/device/{device_id}", status_code=200)
async def delete_device(device_id: str):
    """Delete device"""

    config = load_config()
    # Check for the presence of device with given id
    if not device_id in config.devices:
        raise HTTPException(
            status_code=404,
            detail="There is no device with this ID")
    # Delete device
    del_room_id = config.devices[device_id].room_id
    del config.devices[device_id]
    # Update room
    config.rooms[del_room_id].devices.remove(device_id)
        
    save_config(config)
    return {"message": "Device deleted successfully"}


@app.put("/integrations/alice/device/{device_id}/room", response_model=RoomChange, status_code=200)
async def change_room(device_id: str, device_data: RoomChange):
    """Change room"""
    
    config = load_config()
    # Check for the presence of device with given id
    if not device_id in config.devices:
        raise HTTPException(
            status_code=404,
            detail="There is no device with this ID")
    # Check for the presence of room with given id
    if not device_data.room_id in config.rooms:
        raise HTTPException(
            status_code=404,
            detail="There is no room with this ID")
    # Change room   
    room_change(device_id, device_data.room_id, config)
    config.devices[device_id].rooms_id = device_data.room_id
    response = RoomChange(room_id=device_data.room_id)
    
    save_config(config)
    return response


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
