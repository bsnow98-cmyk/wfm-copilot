"""
/forecasts router.

POST   /forecasts                  — kick off a new forecast run (returns 202)
GET    /forecasts                  — list recent runs
GET    /forecasts/{id}             — get one run's metadata
GET    /forecasts/{id}/intervals   — get the run's forecasted interval points
GET    /forecasts/{id}/report.xlsx — download a multi-sheet Excel report

The actual model fit happens in a FastAPI BackgroundTask. Poll GET /forecasts/{id}
until status == 'completed' or 'failed'.

For Phase 2 this is fine. When forecasts get heavy (multiple queues at once,
deep models), swap BackgroundTasks for Celery + Redis (Redis is already in the
compose stack waiting).
"""
from __future__ import annotations

import logging
from io import BytesIO

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.schemas.forecasts import (
    ForecastDetail,
    ForecastInterval,
    ForecastRunRequest,
    ForecastRunSummary,
)
from app.services.export import build_forecast_report
from app.services.forecasting import ForecastService

log = logging.getLogger("wfm.forecast.router")
router = APIRouter(prefix="/forecasts", tags=["forecasts"])


# --------------------------------------------------------------------------
# Background worker — runs in a FastAPI BackgroundTask.
# Important: BackgroundTasks share the request's event loop but NOT its DB
# session, which gets closed when the request returns. So we open a fresh
# SessionLocal here.
# --------------------------------------------------------------------------
def _run_in_background(
    run_id: int,
    queue: str,
    channel: str,
    horizon_days: int,
    model: str,
    backtest_days: int,
    skill_id: int | None = None,
) -> None:
    with SessionLocal() as db:
        svc = ForecastService(db)
        svc.execute_run(
            run_id=run_id,
            queue=queue,
            channel=channel,
            horizon_days=horizon_days,
            model=model,
            backtest_days=backtest_days,
            skill_id=skill_id,
        )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post(
    "",
    response_model=ForecastRunSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Kick off a new forecast run.",
)
def create_forecast(
    body: ForecastRunRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ForecastRunSummary:
    # Sanity check: history exists for this queue/channel.
    n = db.execute(
        text("""
            SELECT COUNT(*) FROM interval_history
            WHERE queue=:queue AND channel=:channel
        """),
        {"queue": body.queue, "channel": body.channel},
    ).scalar_one()
    if n == 0:
        raise HTTPException(
            status_code=400,
            detail=f"No interval_history rows for queue={body.queue!r} channel={body.channel!r}. "
                   f"Ingest data first via POST /ingest/intervals or seed_db.",
        )

    svc = ForecastService(db)
    run_id = svc.create_run(
        queue=body.queue,
        channel=body.channel,
        horizon_days=body.horizon_days,
        model=body.model,
        backtest_days=body.backtest_days,
        skill_id=body.skill_id,
    )
    log.info(
        "Created forecast run %s for queue=%s model=%s skill_id=%s",
        run_id, body.queue, body.model, body.skill_id,
    )

    background.add_task(
        _run_in_background,
        run_id=run_id,
        queue=body.queue,
        channel=body.channel,
        horizon_days=body.horizon_days,
        model=body.model,
        backtest_days=body.backtest_days,
        skill_id=body.skill_id,
    )

    return _load_summary(db, run_id)


@router.get("", response_model=list[ForecastRunSummary])
def list_forecasts(
    queue: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
) -> list[ForecastRunSummary]:
    sql = """
        SELECT id, queue, channel, model_name, status,
               horizon_start, horizon_end, mape, wape, error_message,
               created_at, started_at, completed_at, skill_id
        FROM forecast_runs
        {where}
        ORDER BY created_at DESC
        LIMIT :limit
    """
    where = "WHERE queue = :queue" if queue else ""
    params: dict = {"limit": limit}
    if queue:
        params["queue"] = queue

    rows = db.execute(text(sql.format(where=where)), params).mappings().all()
    return [ForecastRunSummary(**dict(r)) for r in rows]


@router.get("/{run_id}", response_model=ForecastDetail)
def get_forecast(
    run_id: int,
    include_intervals: bool = True,
    db: Session = Depends(get_db),
) -> ForecastDetail:
    summary = _load_summary(db, run_id)
    if summary is None:
        raise HTTPException(404, f"Forecast run {run_id} not found")

    intervals: list[ForecastInterval] = []
    if include_intervals:
        rows = db.execute(
            text("""
                SELECT interval_start, forecast_offered, forecast_aht_seconds
                FROM forecast_intervals
                WHERE forecast_run_id = :id
                ORDER BY interval_start
            """),
            {"id": run_id},
        ).mappings().all()
        intervals = [ForecastInterval(**dict(r)) for r in rows]

    return ForecastDetail(**summary.model_dump(), intervals=intervals)


@router.get(
    "/{run_id}/report.xlsx",
    summary="Download a multi-sheet Excel report (forecast + linked staffing).",
    response_class=StreamingResponse,
)
def download_forecast_report(run_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    """Builds an .xlsx in-memory and streams it back as a download.

    The workbook contains:
      - Summary sheet (run metadata, MAPE/WAPE)
      - Forecast sheet (per-interval volume + AHT, with chart)
      - One sheet per staffing scenario computed against this forecast,
        each with a chart of required agents over time.
    """
    try:
        data = build_forecast_report(db, run_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    filename = f"forecast_{run_id}_report.xlsx"
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{run_id}/intervals", response_model=list[ForecastInterval])
def get_forecast_intervals(run_id: int, db: Session = Depends(get_db)) -> list[ForecastInterval]:
    rows = db.execute(
        text("""
            SELECT interval_start, forecast_offered, forecast_aht_seconds
            FROM forecast_intervals
            WHERE forecast_run_id = :id
            ORDER BY interval_start
        """),
        {"id": run_id},
    ).mappings().all()
    if not rows:
        # Distinguish "no run" from "run exists but no intervals yet (still running)"
        exists = db.execute(
            text("SELECT 1 FROM forecast_runs WHERE id = :id"),
            {"id": run_id},
        ).scalar_one_or_none()
        if not exists:
            raise HTTPException(404, f"Forecast run {run_id} not found")
    return [ForecastInterval(**dict(r)) for r in rows]


def _load_summary(db: Session, run_id: int) -> ForecastRunSummary | None:
    row = db.execute(
        text("""
            SELECT id, queue, channel, model_name, status,
                   horizon_start, horizon_end, mape, wape, error_message,
                   created_at, started_at, completed_at
            FROM forecast_runs WHERE id = :id
        """),
        {"id": run_id},
    ).mappings().first()
    return ForecastRunSummary(**dict(row)) if row else None
