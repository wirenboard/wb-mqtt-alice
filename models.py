from typing import List, Dict, Optional
from pydantic import BaseModel
from uuid import UUID

class StatusInfo(BaseModel):
    reportable: bool = False

class Room(BaseModel):
    name: str
    devices: List[str] = []

    def post_response(self, room_id: UUID) -> Dict[UUID, Dict]:
        return {
            room_id: {
                "name": self.name,
                "devices": []
            }
        }

    def put_response(self) -> Dict:
        return {
            "name": self.name,
            "devices": self.devices
        }

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

    def post_response(self, device_id: UUID) -> Dict[UUID, Dict]:
        return {
            device_id: {
                "name": self.name,
                "status_info": self.status_info,
                "description": self.description,
                "room_id": self.room_id,
                "type": self.type,
                "capabilities": self.capabilities,
                "properties": self.properties
            }
        }

class RoomID(BaseModel):
    room_id: str

class Config(BaseModel):
    rooms: Dict[str, Room]
    devices: Dict[str, Device]
    link_url: Optional[str] = None
    unlink_url: Optional[str] = None

class IntegrationConfig(BaseModel):
    client_enabled: bool = False