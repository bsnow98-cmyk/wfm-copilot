"""Pydantic schemas for Surface #4 — forecast overrides."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ForecastOverrideApplyRequest(BaseModel):
    apply_token: str


class ForecastOverrideApplyResponse(BaseModel):
    log_id: str
    forecast_run_id: int
    interval_start: datetime
    before_value: float
    after_value: float
    applied_at: datetime


class ForecastOverrideLogEntry(BaseModel):
    id: str
    applied_at: datetime
    applied_by: str
    conversation_id: str | None = None
    forecast_run_id: int
    interval_start: datetime
    before_value: float
    after_value: float
    undo_window_ends_at: datetime
    undone_at: datetime | None = None


class ForecastOverrideUndoResponse(BaseModel):
    log_id: str
    forecast_run_id: int
    interval_start: datetime
    restored_value: float
    undone_at: datetime
