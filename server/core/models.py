from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ClodiaStatus(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    CANCELLING = "cancelling"
    ERROR = "error"
    STOPPED = "stopped"


class MessageRequest(BaseModel):
    content: str


class CreateChatRequest(BaseModel):
    """Body opzionale per POST /clodia/chats. Se assente o senza 'kind',
    default = 'clodia' (mantiene backcompat con il vecchio client)."""
    kind: Optional[str] = None


class ChatMessage(BaseModel):
    id: str
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime
    meta: dict = Field(default_factory=dict)


class Event(BaseModel):
    type: str   # "status" | "message" | "message_chunk" | "usage" | "error" | "interrupted"
    payload: dict
    timestamp: datetime
