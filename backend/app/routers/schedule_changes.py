"""
/schedules/apply, /schedules/changes — cherry-pick D HTTP surface.

Endpoints:
  POST /schedules/apply                    — write a previously-previewed change
  GET  /schedules/changes                  — list audit log entries
  POST /schedules/changes/{log_id}/undo    — reverse one within the 24h window
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.schedule_change import (
    ApplyRequest,
    ApplyResponse,
    ScheduleChangeLogEntry,
    UndoResponse,
)
from app.services.apply_tokens import (
    TokenExpired,
    TokenNotFound,
    consume_token,
    mark_consumed,
)
from app.services.notifications import (
    notify_schedule_applied,
    notify_schedule_undone,
)
from app.services.schedule_change import (
    AlreadyUndone,
    ChangeNotFound,
    StaleVersionError,
    UndoWindowExpired,
    apply_change,
    snapshot_state,
    undo_change,
)
from app.services.summarize_change import summarize_change

log = logging.getLogger("wfm.schedule_changes")
router = APIRouter(prefix="/schedules", tags=["schedule_changes"])


@router.post("/apply", response_model=ApplyResponse)
def post_apply(req: ApplyRequest, db: Session = Depends(get_db)) -> ApplyResponse:
    # Look up + lock the apply_token.
    try:
        token = consume_token(db, req.apply_token)
    except TokenNotFound:
        raise HTTPException(404, "apply_token not found")
    except TokenExpired:
        raise HTTPException(410, "apply_token expired (5-minute TTL)")

    # D-6 — duplicate apply with already-consumed token returns the original
    # log_id with the original applied_at. The frontend treats both 200s the
    # same way; the user never knows their second click got idempotency-folded.
    if token.consumed_log_id is not None:
        log_row = (
            db.execute(
                text(
                    """
                    SELECT id, applied_at, schedule_id
                    FROM schedule_change_log WHERE id = :id::uuid
                    """
                ),
                {"id": token.consumed_log_id},
            )
            .mappings()
            .first()
        )
        if log_row is None:
            # Token says consumed, but the log row vanished — corrupted state.
            raise HTTPException(500, "consumed token references missing log entry")
        return ApplyResponse(
            log_id=str(log_row["id"]),
            applied_at=log_row["applied_at"],
            schedule_id=int(log_row["schedule_id"]),
        )

    # Concurrency check + write.
    try:
        log_id = apply_change(
            db,
            schedule_id=token.schedule_id,
            expected_version=req.schedule_version,
            change_set=[c.model_dump(mode="json") for c in req.changes],
            conversation_id=token.conversation_id,
            user_msg_id=token.user_msg_id,
        )
    except StaleVersionError as exc:
        # D-4: 409 with both versions side-by-side. The fresh preview is
        # rebuilt from the current state so the frontend can show 'Your
        # preview / Current state'.
        target_date = req.changes[0].start.date() if req.changes else date.today()
        affected = sorted({c.agent_id for c in req.changes})
        fresh_state = snapshot_state(db, token.schedule_id, affected, target_date)
        raise HTTPException(
            status_code=409,
            detail={
                "your_version": exc.your_version,
                "current_version": exc.current_version,
                "fresh_preview": {
                    "render": "gantt",
                    "date": target_date.isoformat(),
                    "agents": fresh_state,
                },
            },
        )

    # Mark token consumed inside the same transaction.
    mark_consumed(db, req.apply_token, log_id)

    # Re-read the log row to pull applied_at + before/after for the
    # notification summary.
    log_row = (
        db.execute(
            text(
                """
                SELECT applied_at, before_state, after_state
                FROM schedule_change_log WHERE id = :id::uuid
                """
            ),
            {"id": log_id},
        )
        .mappings()
        .one()
    )
    summary = summarize_change(log_row["before_state"], log_row["after_state"])

    notify_schedule_applied(
        db,
        summary=summary,
        log_id=log_id,
        schedule_id=token.schedule_id,
        conversation_id=token.conversation_id,
    )

    db.commit()
    return ApplyResponse(
        log_id=log_id,
        applied_at=log_row["applied_at"],
        schedule_id=token.schedule_id,
    )


@router.get("/changes", response_model=list[ScheduleChangeLogEntry])
def list_changes(
    since: date | None = None,
    conversation_id: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[ScheduleChangeLogEntry]:
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date()
    where = "WHERE applied_at >= :since"
    params: dict[str, Any] = {"since": since, "limit": limit}
    if conversation_id:
        where += " AND conversation_id = :conv::uuid"
        params["conv"] = conversation_id

    rows = (
        db.execute(
            text(
                f"""
                SELECT id, applied_at, applied_by, conversation_id, schedule_id,
                       change_set, undo_window_ends_at, undone_at
                FROM schedule_change_log
                {where}
                ORDER BY applied_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [
        ScheduleChangeLogEntry(
            id=str(r["id"]),
            applied_at=r["applied_at"],
            applied_by=r["applied_by"],
            conversation_id=str(r["conversation_id"]) if r["conversation_id"] else None,
            schedule_id=int(r["schedule_id"]),
            change_set=r["change_set"] or [],
            undo_window_ends_at=r["undo_window_ends_at"],
            undone_at=r["undone_at"],
        )
        for r in rows
    ]


@router.post(
    "/changes/{log_id}/undo",
    response_model=UndoResponse,
    status_code=status.HTTP_200_OK,
)
def post_undo(log_id: str, db: Session = Depends(get_db)) -> UndoResponse:
    try:
        undo_log_id, undone_at = undo_change(db, log_id)
    except ChangeNotFound:
        raise HTTPException(404, "change not found")
    except AlreadyUndone:
        raise HTTPException(409, "change already undone")
    except UndoWindowExpired:
        raise HTTPException(409, "undo window has expired (24h ceiling)")

    # Pull the new log row so the notification summary reflects the rollback.
    new_row = (
        db.execute(
            text(
                """
                SELECT before_state, after_state, schedule_id, conversation_id
                FROM schedule_change_log WHERE id = :id::uuid
                """
            ),
            {"id": undo_log_id},
        )
        .mappings()
        .one()
    )
    summary = "Undid: " + summarize_change(new_row["before_state"], new_row["after_state"])
    notify_schedule_undone(
        db,
        summary=summary,
        undo_log_id=undo_log_id,
        schedule_id=int(new_row["schedule_id"]),
        conversation_id=str(new_row["conversation_id"]) if new_row["conversation_id"] else None,
    )
    db.commit()
    return UndoResponse(undo_log_id=undo_log_id, undone_at=undone_at)
