from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum

class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"

class MessageType(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    MEDIA = "media"
    DOCUMENT = "document"

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