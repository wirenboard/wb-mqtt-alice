import uuid
from typing import Dict, List

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Alice Integration API",
    version="1.0.0",
)


# Models
class AddRoom(BaseModel):
    name: str


class AddedRoom(BaseModel):
    id: str
    name: str


class Room(BaseModel):
    id: str
    name: str
    devices: List[str] = []


class AddDevice(BaseModel):
    name: str
    room: str  # The ID of the room to which the device is bound


class AddedDevice(BaseModel):
    id: str
    name: str
    type: str
    room: str


class Capability(BaseModel):
    type: str
    mqtt: str


class Property(BaseModel):
    type: str
    mqtt: str


class Device(BaseModel):
    id: str
    name: str
    type: str
    room: str  # The ID of the room to which the device is bound
    capabilities: List[Capability] = []
    properties: List[Property] = []


# On this momemt this vars are use local for debug
# TODO: In future must read from config file
rooms_repo: Dict[str, Room] = {}
devices_repo: Dict[str, Device] = {}


def generate_id():
    return str(uuid.uuid4())


# API Endpoints
@app.get("/integrations/alice", response_model=dict)
async def get_all_rooms_and_devices():
    """Get all the rooms and devices"""
    return {"rooms": list(rooms_repo.values()), "devices": list(devices_repo.values())}


@app.post("/integrations/alice/room", response_model=AddedRoom, status_code=201)
async def create_room(room_data: AddRoom):
    """Create a new room"""
    room_id = generate_id()
    new_room = Room(id=room_id, name=room_data.name)
    rooms_repo[room_id] = new_room
    return AddedRoom(id=room_id, name=room_data.name)


@app.put("/integrations/alice/room/{room_id}", response_model=Room)
async def update_room(room_id: str, room_data: Room):
    """Update room information"""
    if room_id not in rooms_repo:
        raise HTTPException(status_code=404, detail="Room not found")

    # Save the device list before updating
    devices = rooms_repo[room_id].devices

    # Renovating the room
    rooms_repo[room_id] = room_data
    rooms_repo[room_id].devices = devices  # Restore the device list

    return rooms_repo[room_id]


@app.delete("/integrations/alice/room/{room_id}")
async def delete_room(room_id: str):
    """Delete the room and all associated devices"""
    if room_id not in rooms_repo:
        raise HTTPException(status_code=404, detail="Room not found")

    # Remove all devices in this room
    for device_id in list(devices_repo.keys()):
        if devices_repo[device_id].room == room_id:
            del devices_repo[device_id]

    del rooms_repo[room_id]
    return {"message": "Room and its devices deleted successfully"}


@app.post("/integrations/alice/device", response_model=AddedDevice, status_code=201)
async def register_device(device_data: AddDevice):
    """Create a new device"""
    # Checking the existence of the room
    if device_data.room not in rooms_repo:
        raise HTTPException(status_code=400, detail="Room does not exist")

    device_id = generate_id()
    new_device = Device(
        id=device_id,
        name=device_data.name,
        type=device_data.type,
        room=device_data.room,
    )
    devices_repo[device_id] = new_device

    # Добавляем устройство в комнату
    rooms_repo[device_data.room].devices.append(device_id)

    return AddedDevice(
        id=device_id,
        name=device_data.name,
        type=device_data.type,
        room=device_data.room,
    )


@app.put("/integrations/alice/device/{device_id}", response_model=Device)
async def update_device(device_id: str, device_data: Device):
    """Update device information"""
    if device_id not in devices_repo:
        raise HTTPException(status_code=404, detail="Device not found")

    # Verifying the existence of the new room
    if device_data.room not in rooms_repo:
        raise HTTPException(status_code=400, detail="Room does not exist")

    old_room_id = devices_repo[device_id].room
    new_room_id = device_data.room

    # If the room has changed, update the links
    if old_room_id != new_room_id:
        # Удаляем из старой комнаты
        if old_room_id in rooms_repo:
            rooms_repo[old_room_id].devices = [
                d for d in rooms_repo[old_room_id].devices if d != device_id
            ]

        # Добавляем в новую комнату
        rooms_repo[new_room_id].devices.append(device_id)

    # Adding in a new room
    devices_repo[device_id] = device_data
    return device_data


@app.delete("/integrations/alice/device/{device_id}")
async def delete_device(device_id: str):
    """Remove the device"""
    if device_id not in devices_repo:
        raise HTTPException(status_code=404, detail="Device not found")

    # Removing the device from the room
    room_id = devices_repo[device_id].room
    if room_id in rooms_repo:
        rooms_repo[room_id].devices = [
            d for d in rooms_repo[room_id].devices if d != device_id
        ]

    del devices_repo[device_id]
    return {"message": "Device deleted successfully"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
