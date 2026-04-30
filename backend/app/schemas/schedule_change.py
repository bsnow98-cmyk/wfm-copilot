"""Pydantic schemas for cherry-pick D — schedule write actions."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

GanttActivity = Literal[
    "available", "break", "lunch", "training", "meeting", "shrinkage", "off"
]


class ScheduleChange(BaseModel):
    """A single proposed segment edit. Same shape as preview_schedule_change input."""

    agent_id: str = Field(..., description="External employee_id from the agents table.")
    start: datetime
    end: datetime
    activity: GanttActivity


class ApplyRequest(BaseModel):
    apply_token: str
    schedule_version: int
    changes: list[ScheduleChange]


class ApplyResponse(BaseModel):
    log_id: str
    applied_at: datetime
    schedule_id: int


class FreshPreview(BaseModel):
    """409 body — both versions side by side so the frontend can render the
    'Your preview / Current state' UI without re-fetching."""

    current_version: int
    your_version: int
    fresh_preview: dict[str, Any]


class ScheduleChangeLogEntry(BaseModel):
    id: str
    applied_at: datetime
    applied_by: str
    schedule_id: int
    conversation_id: str | None = None
    change_set: list[dict[str, Any]]
    undo_window_ends_at: datetime
    undone_at: datetime | None = None


class UndoResponse(BaseModel):
    undo_log_id: str
    undone_at: datetime
