"""
/vacation/rounds — vacation-bidding award surface.

  POST /vacation/rounds/{id}/award            — batch-award a closed round (manager+)
  POST /vacation/rounds/{id}/publish          — notify agents (separate from award)
  POST /vacation/rounds/awards/{log_id}/undo  — strict reverse within 24h (manager+)
  GET  /vacation/rounds                        — list rounds
  GET  /vacation/rounds/{id}/awards            — award audit (awards + denials)

The award rides write_actions.apply_via_token (single-use token, idempotent,
one transaction). It commits SILENTLY — notification is the separate publish step
so the manager can review/undo before dozens of agents are told.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.identity import User, require_role
from app.schemas.vacation import (
    VacationAwardApplyRequest,
    VacationAwardApplyResponse,
    VacationAwardLogEntry,
    VacationPublishResponse,
    VacationUndoResponse,
)
from app.services.apply_tokens import (
    consume_vacation_token,
    mark_vacation_consumed,
)
from app.services.notifications import (
    notify_vacation_award_undone,
    notify_vacation_published,
)
from app.services.vacation_bidding import (
    AlreadyPublished,
    AlreadyUndone,
    AwardNotFound,
    RoundNotClosed,
    RoundNotFound,
    StaleInputsError,
    UndoWindowExpired,
    apply_award,
    publish_round,
    undo_award,
)
from app.services.write_actions import apply_via_token

log = logging.getLogger("wfm.vacation")
router = APIRouter(prefix="/vacation/rounds", tags=["vacation_bidding"])


@router.post("/{round_id}/award", response_model=VacationAwardApplyResponse)
def post_award(
    round_id: int,
    req: VacationAwardApplyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("wfm_manager")),
) -> VacationAwardApplyResponse:
    def _idempotent(db: Session, log_id: str) -> VacationAwardApplyResponse:
        row = (
            db.execute(
                text(
                    "SELECT id, round_id, summary, applied_at "
                    "FROM vacation_award_log WHERE id = CAST(:id AS uuid)"
                ),
                {"id": log_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(500, "consumed token references missing award log")
        s = row["summary"] or {}
        return VacationAwardApplyResponse(
            log_id=str(row["id"]),
            round_id=int(row["round_id"]),
            n_awarded=int(s.get("n_awarded", 0)),
            n_zero_win=int(s.get("n_zero_win", 0)),
            applied_at=row["applied_at"],
        )

    def _write(db: Session, token: Any):
        if token.round_id != round_id:
            raise HTTPException(400, "token round_id does not match the URL")
        result = apply_award(
            db,
            round_id=token.round_id,
            expected_version=token.expected_version,
            conversation_id=token.conversation_id,
            actor=user.username,
        )
        return result, result.log_id

    def _notify(db: Session, token: Any, result: Any) -> None:
        return None  # decoupled — notification fires on publish

    try:
        return apply_via_token(
            db,
            req.apply_token,
            consume=consume_vacation_token,
            consumed_ref=lambda t: t.consumed_log_id,
            idempotent_result=_idempotent,
            write=_write,
            mark_consumed=mark_vacation_consumed,
            notify=_notify,
            response=lambda r: VacationAwardApplyResponse(
                log_id=r.log_id,
                round_id=r.round_id,
                n_awarded=r.n_awarded,
                n_zero_win=r.n_zero_win,
                applied_at=r.applied_at,
            ),
        )
    except RoundNotFound:
        raise HTTPException(404, "bid round not found")
    except RoundNotClosed as exc:
        raise HTTPException(409, str(exc))
    except StaleInputsError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "your_version": exc.your_version,
                "current_version": exc.current_version,
                "round_id": exc.round_id,
                "message": "Leave/capacity changed since the preview — re-preview before awarding.",
            },
        )


@router.post("/{round_id}/publish", response_model=VacationPublishResponse)
def post_publish(
    round_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("wfm_manager")),
) -> VacationPublishResponse:
    try:
        result = publish_round(db, round_id)
    except RoundNotFound:
        raise HTTPException(404, "bid round not found")
    except RoundNotClosed as exc:  # reused for wrong-status (must be 'awarded')
        raise HTTPException(409, str(exc))

    notify_vacation_published(
        db,
        summary=f"Vacation bid round {round_id} results published to agents.",
        round_id=round_id,
        conversation_id=None,
    )
    db.commit()
    return VacationPublishResponse(round_id=result["round_id"], published_at=result["published_at"])


@router.post("/awards/{log_id}/undo", response_model=VacationUndoResponse)
def post_undo(
    log_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("wfm_manager")),
) -> VacationUndoResponse:
    try:
        result = undo_award(db, log_id)
    except AwardNotFound:
        raise HTTPException(404, "award not found")
    except AlreadyUndone:
        raise HTTPException(409, "award already undone")
    except UndoWindowExpired:
        raise HTTPException(409, "undo window has expired (24h ceiling)")
    except AlreadyPublished:
        raise HTTPException(409, "round already published — undo blocked")

    notify_vacation_award_undone(
        db,
        summary=result.summary,
        log_id=result.log_id,
        round_id=result.round_id,
        conversation_id=None,
    )
    db.commit()
    return VacationUndoResponse(
        log_id=result.log_id,
        round_id=result.round_id,
        reversed_count=result.reversed_count,
        drifted=result.drifted,
        undone_at=result.undone_at,
    )


@router.get("", response_model=list[dict])
def list_rounds(limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.execute(
            text(
                """
                SELECT id, name, status, season_start, season_end, max_weeks_per_agent,
                       awarded_at, published_at, created_at
                FROM bid_rounds ORDER BY created_at DESC LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/{round_id}/awards", response_model=list[VacationAwardLogEntry])
def list_awards(
    round_id: int,
    since: date | None = None,
    db: Session = Depends(get_db),
) -> list[VacationAwardLogEntry]:
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).date()
    rows = (
        db.execute(
            text(
                """
                SELECT id, round_id, applied_at, applied_by, awards, denials, summary,
                       undo_window_ends_at, undone_at
                FROM vacation_award_log
                WHERE round_id = :rid AND applied_at >= :since
                ORDER BY applied_at DESC
                """
            ),
            {"rid": round_id, "since": since},
        )
        .mappings()
        .all()
    )
    return [
        VacationAwardLogEntry(
            id=str(r["id"]),
            round_id=int(r["round_id"]),
            applied_at=r["applied_at"],
            applied_by=r["applied_by"],
            awards=r["awards"] or [],
            denials=r["denials"] or [],
            summary=r["summary"] or {},
            undo_window_ends_at=r["undo_window_ends_at"],
            undone_at=r["undone_at"],
        )
        for r in rows
    ]
