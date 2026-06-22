"""Pydantic schemas for Surface #2 — OT/VTO offer publishing."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel


class OfferApplyRequest(BaseModel):
    apply_token: str


class OfferApplyResponse(BaseModel):
    offer_id: int
    kind: Literal["ot", "vto"]
    slots: int
    n_targets: int
    published_at: datetime


class OfferLogEntry(BaseModel):
    id: int
    kind: Literal["ot", "vto"]
    target_date: date
    window_start: datetime
    window_end: datetime
    slots: int
    targets: list[dict[str, Any]]
    policy: str | None = None
    message: str | None = None
    status: Literal["open", "retracted"]
    published_at: datetime
    published_by: str
    undo_window_ends_at: datetime
    retracted_at: datetime | None = None


class OfferRetractResponse(BaseModel):
    offer_id: int
    retracted_at: datetime
