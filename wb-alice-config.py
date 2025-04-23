import os
import json
import uuid

import uvicorn
from fastapi import FastAPI, HTTPException

from models import AddRoom, Room, Capability, Property, AddDevice, Device, Config

app = FastAPI(
    title="Alice Integration API",
    version="1.0.0",
)

CONFIG_PATH = "/etc/wb-alice-devices.conf"


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
            json.dump(config.model_dump(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise


def generate_id():
    return str(uuid.uuid4())


def room_name_exist(name: str, rooms) -> bool:
    return any(room.name == name for room in rooms)


def room_id_exist(room_id: str, rooms) -> bool:
    return any(room.id == room_id for room in rooms)


def room_index(room_id: str, rooms) -> int:
    index = next(i for i, room in enumerate(rooms) if room.id == room_id)
    return index


def device_name_exist(name: str, room_id: str, devices) -> bool:
    return any((device.name == name)&(device.room_id == room_id) for device in devices)


def device_id_exist(device_id: str, devices) -> bool:
    return any(device.id == device_id for device in devices)


def device_index(device_id: str, devices) -> int:
    index = next(i for i, device in enumerate(devices) if device.id == device_id)
    return index


# API Endpoints

@app.get("/integrations/alice", response_model=Config, status_code=200)
async def get_all_rooms_and_devices():
    """Get all the rooms and devices"""
    
    return load_config()


@app.post("/integrations/alice/room", response_model=Room, status_code=201)
async def create_room(room_data: AddRoom):
    """Create new room"""

    # Check if room with given name exists
    if room_name_exist(room_data.name, config.rooms):
        raise HTTPException(
                status_code=409,
                detail="Room with this name already exists")
    # Create room
    room_id = generate_id()
    response = Room(id=room_id, name=room_data.name, devices=list())
    config.rooms.insert(-1, response)
    
    save_config(config)
    return response


@app.put("/integrations/alice/room/{room_id}", response_model=Room, status_code=200)
async def update_room(room_id: str, room_data: Room):
    """Update room"""
    
    # Check for the presence of room with given id
    if not room_id_exist(room_id, config.rooms):
        raise HTTPException(
            status_code=404,
            detail="There is no room with this ID.")
    # Update room
    response = Room(id=room_id, name=room_data.name, devices=room_data.devices)
    index = room_index(room_id, config.rooms)
    config.rooms[index] = response
    
    save_config(config)
    return response


@app.delete("/integrations/alice/room/{room_id}", status_code=200)
async def delete_room(room_id: str, room_data: Room):
    """Delete room"""
    
    # Check for the presence of room with given id
    if not room_id_exist(room_id, config.rooms):
        raise HTTPException(
            status_code=404,
            detail="There is no room with this ID.")
    # Delete room
    index = room_index(room_id, config.rooms)
    devices_roomless = config.rooms[index].devices
    del config.rooms[index]
    # Transfer device ids from room being deleted to room "without_rooms"
    index = room_index("without_rooms", config.rooms)
    config.rooms[index].devices.extend(devices_roomless)
    # Change device id of room being deleted to "without_rooms"
    for device_id in devices_roomless:
        index = device_index(device_id, config.devices)
        config.devices[index].room_id = "without_rooms"
        
    save_config(config)
    return {"message": "Room deleted successfully"}


@app.post("/integrations/alice/device", response_model=Device, status_code=201)
async def register_device(device_data: AddDevice):
    """Create new device"""
    
    # Check for device with given name
    if device_name_exist(device_data.name, device_data.room_id, config.devices):
        raise HTTPException(
            status_code=409,
            detail="Device with this name already exists")
    # Create device
    device_id = generate_id()
    response = Device(id=device_id,
                      name=device_data.name,
                      #status_info=device_data.status_info,
                      #description=device_data.description,
                      room_id=device_data.room_id,
                      type=device_data.type,
                      capabilities=device_data.capabilities,
                      properties=device_data.properties)
    index = room_index(device_data.room_id, config.rooms)
    config.rooms[index].devices.append(device_id)
    config.devices.append(response)
    
    save_config(config)
    return response


@app.put("/integrations/alice/device/{device_id}", response_model=Device, status_code=200)
async def update_device(device_id: str, device_data: Device):
    """Update device"""
    
    # Check for the presence of device with given id
    if not device_id_exist(device_id, config.devices):
        raise HTTPException(
            status_code=404,
            detail="There is no device with this ID.")
    # Check for the presence of room with given id
    if not room_id_exist(device_data.room_id, config.rooms):
        raise HTTPException(
            status_code=404,
            detail="There is no room with this ID.")
    # Update device
    response = Device(id=device_id,
                      name=device_data.name,
                      #status_info=device_data.status_info,
                      #description=device_data.description,
                      room_id=device_data.room_id,
                      type=device_data.type,
                      capabilities=device_data.capabilities,
                      properties=device_data.properties)
    index = device_index(device_id, config.devices)
    del_room_id = config.devices[index].room_id
    config.devices[index] = response
    # Update room
    if device_data.room_id != del_room_id:
        index = room_index(del_room_id, config.rooms)
        config.rooms[index].devices.remove(device_id)
        index = room_index(device_data.room_id, config.rooms)
        config.rooms[index].devices.append(device_id)
    
    save_config(config)
    return response


@app.delete("/integrations/alice/device/{device_id}", status_code=200)
async def delete_device(device_id: str):
    """Delete device"""
    
    # Check for the presence of device with given id
    if not device_id_exist(device_id, config.devices):
        raise HTTPException(
            status_code=404,
            detail="There is no device with this ID.")
    # Delete device
    index = device_index(device_id, config.devices)
    del_room_id = config.devices[index].room_id
    del config.devices[index]
    # Update room
    index = room_index(del_room_id, config.rooms)
    config.rooms[index].devices.remove(device_id)
        
    save_config(config)
    return {"message": "Device deleted successfully"}

config = load_config()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
