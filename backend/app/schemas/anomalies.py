"""Pydantic schemas for the Phase 5 anomaly endpoints."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high"]
Detector = Literal["isolation_forest", "lof", "rolling_mean"]


class AnomalyDetectRequest(BaseModel):
    queue: str
    start_date: date
    end_date: date
    include_skill_drift: bool = Field(
        False,
        description=(
            "Phase 8 stage 5 — also run the skill_mix_drift detector for the "
            "end_date. Requires per-skill rows in interval_history."
        ),
    )


class AnomalyDetectResponse(BaseModel):
    inserted: int
    skipped_duplicates: int
    detectors_run: list[Detector]
    skill_drift_score: float | None = None


class AnomalyOut(BaseModel):
    id: str
    date: date
    interval_start: datetime
    queue: str
    category: str
    severity: Severity
    score: float
    observed: float | None = None
    expected: float | None = None
    residual: float | None = None
    detector: Detector
    note: str | None = None


class AnomaliesListResponse(BaseModel):
    items: list[AnomalyOut] = Field(default_factory=list)
