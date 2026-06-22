"""
/forecast/overrides — Surface #4 HTTP surface (EXECUTION_ROADMAP.md).

Endpoints:
  POST /forecast/overrides/apply            — pin a previewed forecast interval
  GET  /forecast/overrides                  — audit feed
  POST /forecast/overrides/{log_id}/undo    — restore prior value within 24h

Built on the shared apply envelope (write_actions.apply_via_token). The token
(minted by preview_forecast_override) is the integrity boundary.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.forecast_override import (
    ForecastOverrideApplyRequest,
    ForecastOverrideApplyResponse,
    ForecastOverrideLogEntry,
    ForecastOverrideUndoResponse,
)
from app.services.apply_tokens import consume_forecast_token, mark_forecast_consumed
from app.services.forecast_override import (
    AlreadyUndone,
    ChangeNotFound,
    IntervalNotFound,
    StaleVersionError,
    UndoWindowExpired,
    apply_override,
    load_interval_value,
    undo_override,
)
from app.services.notifications import (
    notify_forecast_override_applied,
    notify_forecast_override_undone,
)
from app.services.write_actions import apply_via_token

log = logging.getLogger("wfm.forecast_overrides")
router = APIRouter(prefix="/forecast/overrides", tags=["forecast_overrides"])


@router.post("/apply", response_model=ForecastOverrideApplyResponse)
def post_apply(
    req: ForecastOverrideApplyRequest, db: Session = Depends(get_db)
) -> ForecastOverrideApplyResponse:
    def _idempotent(db: Session, log_id: str) -> ForecastOverrideApplyResponse:
        row = (
            db.execute(
                text(
                    """
                    SELECT id, forecast_run_id, interval_start, before_value,
                           after_value, applied_at
                    FROM forecast_override_log WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": log_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(500, "consumed token references missing log entry")
        return ForecastOverrideApplyResponse(
            log_id=str(row["id"]),
            forecast_run_id=int(row["forecast_run_id"]),
            interval_start=row["interval_start"],
            before_value=float(row["before_value"]),
            after_value=float(row["after_value"]),
            applied_at=row["applied_at"],
        )

    def _write(db: Session, token: Any):
        result = apply_override(
            db,
            forecast_run_id=token.forecast_run_id,
            interval_start=token.interval_start,
            new_value=token.new_value,
            expected_version=token.expected_version,
            conversation_id=token.conversation_id,
        )
        return result, result.log_id

    def _notify(db: Session, token: Any, result: Any) -> None:
        notify_forecast_override_applied(
            db,
            summary=result.summary,
            log_id=result.log_id,
            forecast_run_id=result.forecast_run_id,
            conversation_id=token.conversation_id,
        )

    try:
        return apply_via_token(
            db,
            req.apply_token,
            consume=consume_forecast_token,
            consumed_ref=lambda t: t.consumed_log_id,
            idempotent_result=_idempotent,
            write=_write,
            mark_consumed=mark_forecast_consumed,
            notify=_notify,
            response=lambda r: ForecastOverrideApplyResponse(
                log_id=r.log_id,
                forecast_run_id=r.forecast_run_id,
                interval_start=r.interval_start,
                before_value=r.before_value,
                after_value=r.after_value,
                applied_at=r.applied_at,
            ),
        )
    except IntervalNotFound:
        raise HTTPException(404, "forecast interval not found")
    except StaleVersionError as exc:
        # The forecast value changed since the preview (re-run or re-override).
        fresh: float | None = None
        if exc.forecast_run_id is not None and exc.interval_start is not None:
            fresh = load_interval_value(db, exc.forecast_run_id, exc.interval_start)
        raise HTTPException(
            status_code=409,
            detail={
                "your_version": exc.your_version,
                "current_version": exc.current_version,
                "current_value": fresh,
            },
        )


@router.get("", response_model=list[ForecastOverrideLogEntry])
def list_overrides(
    since: date | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[ForecastOverrideLogEntry]:
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date()
    rows = (
        db.execute(
            text(
                """
                SELECT id, applied_at, applied_by, conversation_id, forecast_run_id,
                       interval_start, before_value, after_value,
                       undo_window_ends_at, undone_at
                FROM forecast_override_log
                WHERE applied_at >= :since
                ORDER BY applied_at DESC
                LIMIT :limit
                """
            ),
            {"since": since, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [
        ForecastOverrideLogEntry(
            id=str(r["id"]),
            applied_at=r["applied_at"],
            applied_by=r["applied_by"],
            conversation_id=str(r["conversation_id"]) if r["conversation_id"] else None,
            forecast_run_id=int(r["forecast_run_id"]),
            interval_start=r["interval_start"],
            before_value=float(r["before_value"]),
            after_value=float(r["after_value"]),
            undo_window_ends_at=r["undo_window_ends_at"],
            undone_at=r["undone_at"],
        )
        for r in rows
    ]


@router.post("/{log_id}/undo", response_model=ForecastOverrideUndoResponse)
def post_undo(log_id: str, db: Session = Depends(get_db)) -> ForecastOverrideUndoResponse:
    try:
        result = undo_override(db, log_id)
    except ChangeNotFound:
        raise HTTPException(404, "override not found")
    except AlreadyUndone:
        raise HTTPException(409, "override already undone")
    except UndoWindowExpired:
        raise HTTPException(409, "undo window has expired (24h ceiling)")

    notify_forecast_override_undone(
        db,
        summary=result.summary,
        log_id=result.log_id,
        forecast_run_id=result.forecast_run_id,
        conversation_id=None,
    )
    db.commit()
    return ForecastOverrideUndoResponse(
        log_id=result.log_id,
        forecast_run_id=result.forecast_run_id,
        interval_start=result.interval_start,
        restored_value=result.restored_value,
        undone_at=result.undone_at,
    )
