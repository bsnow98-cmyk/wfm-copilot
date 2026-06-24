"""Pydantic schemas for the vacation-bidding award surface."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class VacationAwardApplyRequest(BaseModel):
    apply_token: str


class VacationAwardApplyResponse(BaseModel):
    log_id: str
    round_id: int
    n_awarded: int
    n_zero_win: int
    applied_at: datetime


class VacationAwardLogEntry(BaseModel):
    id: str
    round_id: int
    applied_at: datetime
    applied_by: str
    awards: list[dict[str, Any]]
    denials: list[dict[str, Any]]
    summary: dict[str, Any]
    undo_window_ends_at: datetime
    undone_at: datetime | None = None


class VacationUndoResponse(BaseModel):
    log_id: str
    round_id: int
    reversed_count: int
    drifted: list[dict[str, Any]]
    undone_at: datetime


class VacationPublishResponse(BaseModel):
    round_id: int
    published_at: datetime
