from typing import List, Dict, Optional
from pydantic import BaseModel
from uuid import UUID

class StatusInfo(BaseModel):
    reportable: bool = False

class Room(BaseModel):
    name: str
    devices: List[str] = []
    
class RoomResponse(BaseModel):
    __root__: Dict[UUID, Room]

class Capability(BaseModel):
    type: str
    mqtt: str
    parameters: Optional[dict] = None

class Property(BaseModel):
    type: str
    mqtt: str
    parameters: Optional[dict] = None

class Device(BaseModel):
    name: str
    status_info: Optional[dict] = None
    description: Optional[str] = None
    room_id: str
    type: str
    capabilities: List[Capability] = []
    properties: List[Property] = []
    
class DeviceResponse(BaseModel):
    __root__: Dict[UUID, Device]

class RoomChange(BaseModel):
    room_id: str

class Config(BaseModel):
    rooms: Dict[str, Room]
    devices: Dict[str, Device]
