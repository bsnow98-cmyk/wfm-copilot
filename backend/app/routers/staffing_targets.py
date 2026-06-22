"""
/staffing/targets — Surface #5 HTTP surface (EXECUTION_ROADMAP.md).

Endpoints:
  POST /staffing/targets/apply           — 202; writes audit (pending) + kicks
                                           off the async recompute job
  GET  /staffing/targets                 — audit feed
  GET  /staffing/targets/{log_id}        — poll recompute status
  POST /staffing/targets/{log_id}/undo   — restore prior targets + recompute (sync)

The sync part (token consume → idempotency → audit write → commit) runs on the
shared apply envelope; the recompute itself is a FastAPI BackgroundTask with its
own session, mirroring the schedule solver. The completion notification fires
from the job, not the request.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.schemas.staffing_target import (
    StaffingTargetApplyRequest,
    StaffingTargetApplyResponse,
    StaffingTargetStatus,
    StaffingTargetUndoResponse,
)
from app.services.apply_tokens import consume_staffing_token, mark_staffing_consumed
from app.services.notifications import notify_staffing_target_undone
from app.services.staffing_target import (
    AlreadyUndone,
    ChangeNotFound,
    StaffingNotFound,
    StaleVersionError,
    UndoWindowExpired,
    apply_target_change,
    run_recompute,
    undo_target_change,
)
from app.services.write_actions import apply_via_token

log = logging.getLogger("wfm.staffing_targets")
router = APIRouter(prefix="/staffing/targets", tags=["staffing_targets"])


@router.post("/apply", response_model=StaffingTargetApplyResponse, status_code=status.HTTP_202_ACCEPTED)
def post_apply(
    req: StaffingTargetApplyRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> StaffingTargetApplyResponse:
    holder: dict[str, Any] = {}

    def _idempotent(db: Session, log_id: str) -> StaffingTargetApplyResponse:
        row = (
            db.execute(
                text(
                    """
                    SELECT id, staffing_id, recompute_status, peak_required_before,
                           before_targets, after_targets, applied_at
                    FROM staffing_target_change_log WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": log_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(500, "consumed token references missing log entry")
        return StaffingTargetApplyResponse(
            log_id=str(row["id"]),
            staffing_id=int(row["staffing_id"]),
            recompute_status=row["recompute_status"],
            peak_required_before=int(row["peak_required_before"] or 0),
            before_targets=row["before_targets"] or {},
            after_targets=row["after_targets"] or {},
            applied_at=row["applied_at"],
        )

    def _write(db: Session, token: Any):
        result = apply_target_change(
            db,
            staffing_id=token.staffing_id,
            new_targets=token.new_targets,
            expected_version=token.expected_version,
            conversation_id=token.conversation_id,
        )
        holder["result"] = result
        return result, result.log_id

    def _notify(db: Session, token: Any, result: Any) -> None:
        # Completion notification fires from the background recompute job.
        return None

    try:
        resp = apply_via_token(
            db,
            req.apply_token,
            consume=consume_staffing_token,
            consumed_ref=lambda t: t.consumed_log_id,
            idempotent_result=_idempotent,
            write=_write,
            mark_consumed=mark_staffing_consumed,
            notify=_notify,
            response=lambda r: StaffingTargetApplyResponse(
                log_id=r.log_id,
                staffing_id=r.staffing_id,
                recompute_status="pending",
                peak_required_before=r.peak_required_before,
                before_targets=r.before_targets,
                after_targets=r.after_targets,
                applied_at=r.applied_at,
            ),
        )
    except StaffingNotFound:
        raise HTTPException(404, "staffing scenario not found")
    except StaleVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "your_version": exc.your_version,
                "current_version": exc.current_version,
                "staffing_id": exc.staffing_id,
            },
        )

    # Schedule the recompute ONLY for a fresh apply — an idempotent re-apply
    # (holder unset) already ran its job.
    if "result" in holder:
        r = holder["result"]
        background.add_task(
            run_recompute,
            SessionLocal,
            log_id=r.log_id,
            staffing_id=r.staffing_id,
            after_targets=r.after_targets,
        )
    return resp


@router.get("", response_model=list[StaffingTargetStatus])
def list_changes(
    since: date | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[StaffingTargetStatus]:
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date()
    rows = (
        db.execute(
            text(
                """
                SELECT id, staffing_id, recompute_status, recompute_error,
                       peak_required_before, peak_required_after,
                       before_targets, after_targets, applied_at, completed_at, undone_at
                FROM staffing_target_change_log
                WHERE applied_at >= :since
                ORDER BY applied_at DESC LIMIT :limit
                """
            ),
            {"since": since, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [_status_from_row(r) for r in rows]


@router.get("/{log_id}", response_model=StaffingTargetStatus)
def get_status(log_id: str, db: Session = Depends(get_db)) -> StaffingTargetStatus:
    row = (
        db.execute(
            text(
                """
                SELECT id, staffing_id, recompute_status, recompute_error,
                       peak_required_before, peak_required_after,
                       before_targets, after_targets, applied_at, completed_at, undone_at
                FROM staffing_target_change_log WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": log_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(404, "change not found")
    return _status_from_row(row)


@router.post("/{log_id}/undo", response_model=StaffingTargetUndoResponse)
def post_undo(log_id: str, db: Session = Depends(get_db)) -> StaffingTargetUndoResponse:
    try:
        result = undo_target_change(db, log_id)
    except ChangeNotFound:
        raise HTTPException(404, "change not found")
    except AlreadyUndone:
        raise HTTPException(409, "change already undone")
    except UndoWindowExpired:
        raise HTTPException(409, "undo window has expired (24h ceiling)")

    notify_staffing_target_undone(
        db,
        summary=result.summary,
        log_id=result.log_id,
        staffing_id=result.staffing_id,
        conversation_id=None,
    )
    db.commit()
    return StaffingTargetUndoResponse(
        log_id=result.log_id,
        staffing_id=result.staffing_id,
        peak_required_after=result.peak_required_after,
        undone_at=result.undone_at,
    )


def _status_from_row(r: Any) -> StaffingTargetStatus:
    return StaffingTargetStatus(
        log_id=str(r["id"]),
        staffing_id=int(r["staffing_id"]),
        recompute_status=r["recompute_status"],
        recompute_error=r["recompute_error"],
        peak_required_before=int(r["peak_required_before"]) if r["peak_required_before"] is not None else None,
        peak_required_after=int(r["peak_required_after"]) if r["peak_required_after"] is not None else None,
        before_targets=r["before_targets"] or {},
        after_targets=r["after_targets"] or {},
        applied_at=r["applied_at"],
        completed_at=r["completed_at"],
        undone_at=r["undone_at"],
    )
