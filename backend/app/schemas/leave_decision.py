"""Pydantic schemas for Surface #1 — leave-decision write actions."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class LeaveApplyRequest(BaseModel):
    apply_token: str


class LeaveApplyResponse(BaseModel):
    log_id: str
    request_id: int
    status: Literal["approved", "denied"]
    decided_at: datetime


class LeaveDecisionLogEntry(BaseModel):
    id: str
    applied_at: datetime
    applied_by: str
    conversation_id: str | None = None
    request_id: int
    decision: Literal["approve", "deny"]
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    undo_window_ends_at: datetime
    undone_at: datetime | None = None


class LeaveUndoResponse(BaseModel):
    log_id: str
    request_id: int
    undone_at: datetime
