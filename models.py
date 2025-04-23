from typing import List, Dict, Optional
from pydantic import BaseModel, RootModel
from uuid import UUID

class StatusInfo(BaseModel):
    reportable: bool = False

class AddRoom(BaseModel):
    name: str   

class Room(AddRoom):
    devices: List[str] = []
    
class RoomResponse(RootModel[Dict[UUID, Room]]):
    pass

class Capability(BaseModel):
    type: str
    mqtt: str

class Property(BaseModel):
    type: str
    mqtt: str

class AddDevice(BaseModel):
    name: str
    status_info: Optional[dict] = None
    description: Optional[str] = None
    room_id: str
    type: str
    capabilities: List[Capability] = []
    properties: List[Property] = []

class Device(BaseModel):
    name: str
    status_info: Optional[dict] = None
    description: Optional[str] = None
    room_id: str
    type: str
    capabilities: List[Capability] = []
    properties: List[Property] = []
    
class DeviceResponse(RootModel[Dict[UUID, Device]]):
    pass

class RoomChange(BaseModel):
    room_id: str

class Config(BaseModel):
    rooms: Dict[str, Room] 
    devices: Dict[str, Device] 
