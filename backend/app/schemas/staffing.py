"""Pydantic models for /staffing-requirements."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class StaffingRequest(BaseModel):
    forecast_run_id: int = Field(..., description="A completed forecast_runs.id.")
    service_level_target: float | None = Field(
        0.80,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of contacts answered within target_answer_seconds. "
            "0.80 = 80%. Set to null to disable the SL constraint and staff "
            "purely to ASA."
        ),
    )
    target_answer_seconds: int = Field(
        20,
        ge=1,
        le=600,
        description='Target answer time for the SL constraint. Industry standard is 20s ("80/20").',
    )
    target_asa_seconds: int | None = Field(
        30,
        ge=1,
        le=600,
        description=(
            "Maximum acceptable Average Speed of Answer in seconds. "
            "Most operations manage to ASA — a typical target is 30s. "
            "Set to null to disable the ASA constraint."
        ),
    )
    shrinkage: float = Field(
        0.30,
        ge=0.0,
        lt=1.0,
        description=(
            "Fraction of paid time NOT productive (breaks, training, meetings, "
            "off-phone). 0.30 = 30%, a typical starting estimate. Adjust to your "
            "operation."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one_target(self) -> "StaffingRequest":
        if self.service_level_target is None and self.target_asa_seconds is None:
            raise ValueError(
                "At least one of service_level_target or target_asa_seconds "
                "must be set — otherwise there's no objective to staff to."
            )
        return self


class StaffingSummary(BaseModel):
    id: int
    forecast_run_id: int
    service_level_target: float | None = None
    target_answer_seconds: int
    target_asa_seconds: int | None = None
    shrinkage: float
    interval_minutes: int
    created_at: datetime
    # Derived fields populated when listing:
    intervals_count: int | None = None
    peak_required_agents: int | None = None


class StaffingIntervalRow(BaseModel):
    interval_start: datetime
    forecast_offered: float
    forecast_aht_seconds: float
    required_agents_raw: int
    required_agents: int
    expected_service_level: float | None = None
    expected_asa_seconds: float | None = None
    occupancy: float | None = None


class StaffingDetail(StaffingSummary):
    intervals: list[StaffingIntervalRow] = []
