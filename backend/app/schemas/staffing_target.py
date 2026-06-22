"""Pydantic schemas for Surface #5 — staffing-target changes (async recompute)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class StaffingTargetApplyRequest(BaseModel):
    apply_token: str


class StaffingTargetApplyResponse(BaseModel):
    """202 — the recompute runs in the background; poll the status endpoint."""

    log_id: str
    staffing_id: int
    recompute_status: Literal["pending", "running", "completed", "failed"]
    peak_required_before: int
    before_targets: dict[str, Any]
    after_targets: dict[str, Any]
    applied_at: datetime


class StaffingTargetStatus(BaseModel):
    log_id: str
    staffing_id: int
    recompute_status: Literal["pending", "running", "completed", "failed"]
    recompute_error: str | None = None
    peak_required_before: int | None = None
    peak_required_after: int | None = None
    before_targets: dict[str, Any]
    after_targets: dict[str, Any]
    applied_at: datetime
    completed_at: datetime | None = None
    undone_at: datetime | None = None


class StaffingTargetUndoResponse(BaseModel):
    log_id: str
    staffing_id: int
    peak_required_after: int
    undone_at: datetime
