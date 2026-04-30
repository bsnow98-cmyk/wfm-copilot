"""Pydantic models for /schedules."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

SolverStatus = Literal["pending", "running", "optimal", "feasible", "infeasible", "failed"]


class ScheduleRequest(BaseModel):
    staffing_id: int = Field(..., description="A computed staffing_requirements.id.")
    name: str | None = Field(None, description="Human-readable name for this schedule.")
    agent_count: int | None = Field(
        None, ge=1, le=2000,
        description="Limit to first N active agents (default: all active agents).",
    )
    horizon_days: int = Field(
        7, ge=1, le=28,
        description="How many days to schedule. Solver runtime grows with horizon.",
    )
    target_shifts_per_week: int = Field(
        5, ge=1, le=7,
        description="Days per week each agent works. 5 days × 8 hrs = 40hr standard.",
    )
    min_rest_hours: int = Field(
        11, ge=0, le=24,
        description="Minimum hours between consecutive shifts (legal min in many jurisdictions).",
    )
    max_consecutive_days: int = Field(
        6, ge=1, le=14,
        description="Maximum consecutive working days.",
    )
    max_solve_time_seconds: int = Field(
        60, ge=5, le=600,
        description="Solver time budget. Returns best feasible if not optimal.",
    )


class ScheduleSummary(BaseModel):
    id: int
    name: str
    staffing_id: int | None
    start_date: date
    end_date: date
    status: str
    solver_status: SolverStatus | None = None
    solver_runtime_seconds: float | None = None
    objective_value: float | None = None
    total_understaffed_intervals: int | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ShiftSegment(BaseModel):
    agent_id: int
    employee_id: str | None = None
    full_name: str | None = None
    segment_type: str
    start_time: datetime
    end_time: datetime


class CoverageRow(BaseModel):
    interval_start: datetime
    required_agents: int
    scheduled_agents: int
    shortage: int


class ScheduleDetail(ScheduleSummary):
    shift_segments: list[ShiftSegment] = []
    coverage: list[CoverageRow] = []
