from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum
from pydantic import EmailStr

class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"

class MessageType(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    MEDIA = "media"
    DOCUMENT = "document"

class ParticipantRole(str, Enum):
    TEAM = "team"
    CLIENT = "client"
    LEADER = "leader"

class GroupConfig(BaseModel):
    """Configuration for a WhatsApp group"""
    group_sid: str
    name: str
    description: Optional[str] = None
    is_active: bool = True
    keywords: List[str] = Field(default_factory=list)
    notification_settings: dict = Field(default_factory=dict)

class GroupCreate(BaseModel):
    """Model for creating a new group"""
    name: str
    description: Optional[str] = None
    keywords: Optional[List[str]] = None
    notification_settings: Optional[dict] = None

class GroupUpdate(BaseModel):
    """Model for updating an existing group"""
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    keywords: Optional[List[str]] = None
    notification_settings: Optional[dict] = None

class FilterParams(BaseModel):
    """Model for filtering messages"""
    group_sid: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    message_type: Optional[MessageType] = None
    keyword: Optional[str] = None

class PaginationParams(BaseModel):
    """Model for pagination"""
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    sort_by: Optional[str] = None
    sort_order: Optional[str] = "desc"  # "asc" or "desc"

class GroupStats(BaseModel):
    """Model for group statistics"""
    total_messages: int
    message_types: dict
    keywords: List[str]
    active_users: int
    last_activity: datetime

class ParticipantCreate(BaseModel):
    phone_number: str
    name: str
    role: ParticipantRole

class Participant(ParticipantCreate):
    group_id: str
    added_at: datetime = datetime.now()
    is_active: bool = True

class MessageStatus(str, Enum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"

class Message(BaseModel):
    """Model for storing WhatsApp messages"""
    group_id: str
    sender: str
    timestamp: datetime
    body: str
    status: MessageStatus = MessageStatus.RECEIVED
    message_type: MessageType = MessageType.TEXT
    media_url: Optional[str] = None
    raw_payload: Optional[Dict] = None
    escalation: Optional[Dict] = Field(default_factory=lambda: {
        "ignored": False,
        "notified_at": None,
        "auto_replied": False
    }) 