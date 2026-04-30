"""Pydantic models for the /forecasts API surface."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ModelName = Literal["seasonal_naive", "auto_arima", "mstl"]
ForecastStatus = Literal["pending", "running", "completed", "failed"]


class ForecastRunRequest(BaseModel):
    queue: str = Field(..., examples=["sales"])
    channel: str = Field("voice", examples=["voice", "chat", "email"])
    horizon_days: int = Field(
        14,
        ge=1,
        le=90,
        description="How many days into the future to forecast.",
    )
    model: ModelName = Field(
        "mstl",
        description=(
            "seasonal_naive = baseline (last week pattern). "
            "auto_arima = single-seasonality SARIMA. "
            "mstl = multi-seasonal (daily + weekly), recommended for 30-min interval data."
        ),
    )
    backtest_days: int = Field(
        14,
        ge=0,
        le=60,
        description="How many days of recent history to hold out for backtesting. 0 = skip backtest.",
    )
    skill_id: int | None = Field(
        None,
        description=(
            "Phase 8 — when set, the run trains on interval_history filtered "
            "to this skill. Omit (or null) for an aggregate forecast across "
            "all skills in the queue."
        ),
    )


class ForecastRunSummary(BaseModel):
    id: int
    queue: str
    channel: str
    model_name: str
    status: ForecastStatus
    horizon_start: datetime | None
    horizon_end: datetime | None
    mape: float | None = Field(None, description="Mean absolute percent error (lower is better).")
    wape: float | None = Field(None, description="Weighted absolute percent error.")
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    skill_id: int | None = None


class ForecastInterval(BaseModel):
    interval_start: datetime
    forecast_offered: float
    forecast_aht_seconds: float | None = None


class ForecastDetail(ForecastRunSummary):
    intervals: list[ForecastInterval] = []
