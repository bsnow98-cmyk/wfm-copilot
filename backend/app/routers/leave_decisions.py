"""
/leave/decisions — Surface #1 HTTP surface (EXECUTION_ROADMAP.md).

Endpoints:
  POST /leave/decisions/apply              — commit a previously-previewed decision
  GET  /leave/decisions                    — list audit log entries
  POST /leave/decisions/{log_id}/undo      — reverse one within the 24h window

Mirrors app/routers/schedule_changes.py. The token (minted by
preview_leave_decision) is the integrity boundary: the decision + version
stored at preview time are authoritative, never the request body.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.leave_decision import (
    LeaveApplyRequest,
    LeaveApplyResponse,
    LeaveDecisionLogEntry,
    LeaveUndoResponse,
)
from app.services.apply_tokens import consume_leave_token, mark_leave_consumed
from app.services.leave_decision import (
    AlreadyUndone,
    ChangeNotFound,
    RequestNotFound,
    StaleVersionError,
    UndoWindowExpired,
    apply_decision,
    load_request,
    undo_decision,
)
from app.services.notifications import (
    notify_leave_decided,
    notify_leave_decision_undone,
)
from app.services.write_actions import apply_via_token

log = logging.getLogger("wfm.leave_decisions")
router = APIRouter(prefix="/leave/decisions", tags=["leave_decisions"])


@router.post("/apply", response_model=LeaveApplyResponse)
def post_apply(req: LeaveApplyRequest, db: Session = Depends(get_db)) -> LeaveApplyResponse:
    def _idempotent(db: Session, log_id: str) -> LeaveApplyResponse:
        row = (
            db.execute(
                text(
                    """
                    SELECT id, request_id, after_state, applied_at
                    FROM leave_decision_log WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": log_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(500, "consumed token references missing log entry")
        return LeaveApplyResponse(
            log_id=str(row["id"]),
            request_id=int(row["request_id"]),
            status=(row["after_state"] or {}).get("status", "approved"),
            decided_at=row["applied_at"],
        )

    def _write(db: Session, token: Any):
        result = apply_decision(
            db,
            request_id=token.request_id,
            expected_version=token.request_version,
            decision=token.decision,
            note=token.note,
            conversation_id=token.conversation_id,
        )
        return result, result.log_id

    def _notify(db: Session, token: Any, result: Any) -> None:
        notify_leave_decided(
            db,
            summary=result.summary,
            log_id=result.log_id,
            request_id=result.request_id,
            decision=token.decision,
            conversation_id=token.conversation_id,
        )

    # Domain concurrency errors raised inside _write propagate here for the
    # surface-specific 409 (the version check runs before any mutation, so the
    # session is clean for the fresh-preview read).
    try:
        return apply_via_token(
            db,
            req.apply_token,
            consume=consume_leave_token,
            consumed_ref=lambda t: t.consumed_log_id,
            idempotent_result=_idempotent,
            write=_write,
            mark_consumed=mark_leave_consumed,
            notify=_notify,
            response=lambda r: LeaveApplyResponse(
                log_id=r.log_id,
                request_id=r.request_id,
                status=r.status,  # type: ignore[arg-type]
                decided_at=r.decided_at,
            ),
        )
    except RequestNotFound:
        raise HTTPException(404, "leave request not found")
    except StaleVersionError as exc:
        # 409 with both versions + a fresh feasibility preview, mirroring D-4.
        info = load_request(db, exc.request_id) if exc.request_id is not None else None
        fresh: dict[str, Any] | None = None
        if info is not None:
            from app.tools.check_leave_feasibility import handler as feasibility_handler

            fresh = feasibility_handler({"request_id": info.request_id}, db)
        raise HTTPException(
            status_code=409,
            detail={
                "your_version": exc.your_version,
                "current_version": exc.current_version,
                "current_status": info.status if info else None,
                "fresh_preview": fresh,
            },
        )


@router.get("", response_model=list[LeaveDecisionLogEntry])
def list_decisions(
    since: date | None = None,
    conversation_id: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[LeaveDecisionLogEntry]:
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date()
    where = "WHERE applied_at >= :since"
    params: dict[str, Any] = {"since": since, "limit": limit}
    if conversation_id:
        where += " AND conversation_id = CAST(:conv AS uuid)"
        params["conv"] = conversation_id

    rows = (
        db.execute(
            text(
                f"""
                SELECT id, applied_at, applied_by, conversation_id, request_id,
                       decision, before_state, after_state,
                       undo_window_ends_at, undone_at
                FROM leave_decision_log
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
        LeaveDecisionLogEntry(
            id=str(r["id"]),
            applied_at=r["applied_at"],
            applied_by=r["applied_by"],
            conversation_id=str(r["conversation_id"]) if r["conversation_id"] else None,
            request_id=int(r["request_id"]),
            decision=r["decision"],
            before_state=r["before_state"] or {},
            after_state=r["after_state"] or {},
            undo_window_ends_at=r["undo_window_ends_at"],
            undone_at=r["undone_at"],
        )
        for r in rows
    ]


@router.post("/{log_id}/undo", response_model=LeaveUndoResponse)
def post_undo(log_id: str, db: Session = Depends(get_db)) -> LeaveUndoResponse:
    try:
        result = undo_decision(db, log_id)
    except ChangeNotFound:
        raise HTTPException(404, "decision not found")
    except AlreadyUndone:
        raise HTTPException(409, "decision already undone")
    except UndoWindowExpired:
        raise HTTPException(409, "undo window has expired (24h ceiling)")

    notify_leave_decision_undone(
        db,
        summary=result.summary,
        log_id=result.log_id,
        request_id=result.request_id,
        conversation_id=None,
    )
    db.commit()
    return LeaveUndoResponse(
        log_id=result.log_id,
        request_id=result.request_id,
        undone_at=result.undone_at,
    )
