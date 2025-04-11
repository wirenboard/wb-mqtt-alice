from typing import List
from pydantic import BaseModel

class StatusInfo(BaseModel):
    reportable: bool = False

class AddRoom(BaseModel):
    name: str

class Room(BaseModel):
    id: str
    name: str
    devices: List[str] = []

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
    id: str
    name: str
    status_info: Optional[dict] = None
    description: Optional[str] = None
    room_id: str
    type: str
    capabilities: List[Capability] = []
    properties: List[Property] = []

class Config(BaseModel):
    rooms: List[Room]
    devices: List[Device]