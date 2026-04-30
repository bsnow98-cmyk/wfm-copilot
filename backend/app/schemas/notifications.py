"""Pydantic schemas for the notification feed (cherry-pick D)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: str
    created_at: datetime
    read_at: datetime | None = None
    category: str
    source: str
    conversation_id: str | None = None
    payload: dict[str, Any]


class NotificationsListResponse(BaseModel):
    items: list[NotificationOut]
    unread_count: int


class MarkReadResponse(BaseModel):
    marked: int
