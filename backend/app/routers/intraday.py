"""
/intraday router.

GET /intraday/today — today's per-interval forecast + actual offered volume,
relative to sim_now(). Dashboard "Intraday" view consumes this directly.

Read-only. Returns the half-hour rows from interval_history (actuals) plus the
matching forecast points from the most recent completed forecast run for the
target queue. Forecast goes the whole day; actuals are clamped to <= sim_now.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.realtime_clock import maybe_ensure_sim_anchor, sim_now

router = APIRouter(prefix="/intraday", tags=["intraday"])


class IntradayPoint(BaseModel):
    interval_start: datetime
    forecast: float | None
    actual: float | None


class IntradayToday(BaseModel):
    queue: str
    sim_now: datetime
    points: list[IntradayPoint]


@router.get("/today", response_model=IntradayToday)
def get_today(
    queue: str = "auto",
    db: Session = Depends(get_db),
) -> IntradayToday:
    # This view is exactly what breaks when the sim clock drifts past the
    # seeded data, and a long-lived process never hits the startup heal —
    # so check (throttled, never raises) on the way in.
    maybe_ensure_sim_anchor(db)
    now = sim_now(db)
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    # "auto" picks the queue with the most recent forecast run — works for
    # both the prod skeleton (single 'all' queue) and local synth (per-skill
    # queues like 'sales' / 'support').
    if queue == "auto":
        picked = db.execute(
            text(
                """
                SELECT queue FROM forecast_runs
                WHERE status = 'completed'
                ORDER BY created_at DESC LIMIT 1
                """
            )
        ).scalar_one_or_none()
        if picked:
            queue = picked

    # Forecast: most recent completed run for this queue.
    forecast_run_id = db.execute(
        text(
            """
            SELECT id FROM forecast_runs
            WHERE queue = :q AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"q": queue},
    ).scalar_one_or_none()

    forecast_rows: list[tuple[datetime, float]] = []
    if forecast_run_id is not None:
        forecast_rows = list(
            db.execute(
                text(
                    """
                    SELECT interval_start, forecast_offered
                    FROM forecast_intervals
                    WHERE forecast_run_id = :id
                      AND interval_start >= :start AND interval_start < :end
                    ORDER BY interval_start
                    """
                ),
                {"id": forecast_run_id, "start": day_start, "end": day_end},
            ).all()
        )

    actual_rows = list(
        db.execute(
            text(
                """
                SELECT interval_start, offered
                FROM interval_history
                WHERE queue = :q
                  AND interval_start >= :start AND interval_start < :end
                  AND interval_start <= :cap
                ORDER BY interval_start
                """
            ),
            {"q": queue, "start": day_start, "end": day_end, "cap": now},
        ).all()
    )

    forecast_by_ts = {r[0]: float(r[1] or 0) for r in forecast_rows}
    actual_by_ts = {r[0]: float(r[1] or 0) for r in actual_rows}
    all_ts = sorted(set(forecast_by_ts) | set(actual_by_ts))

    points = [
        IntradayPoint(
            interval_start=ts,
            forecast=forecast_by_ts.get(ts),
            actual=actual_by_ts.get(ts),
        )
        for ts in all_ts
    ]
    return IntradayToday(queue=queue, sim_now=now, points=points)
