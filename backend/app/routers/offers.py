"""
/offers — Surface #2 HTTP surface (EXECUTION_ROADMAP.md).

Endpoints:
  POST /offers/apply               — publish a previously-previewed OT/VTO offer
  GET  /offers                     — list published offers (audit feed)
  POST /offers/{offer_id}/retract  — retract one within the 24h window

The token (minted by preview_offer) is the integrity boundary: the offer spec
stored at preview time is authoritative, never the request body.
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
from app.schemas.offer import (
    OfferApplyRequest,
    OfferApplyResponse,
    OfferLogEntry,
    OfferRetractResponse,
)
from app.services.apply_tokens import consume_offer_token, mark_offer_consumed
from app.services.notifications import notify_offer_published, notify_offer_retracted
from app.services.offer import (
    AlreadyRetracted,
    OfferNotFound,
    RetractWindowExpired,
    publish_offer,
    retract_offer,
)
from app.services.write_actions import apply_via_token

log = logging.getLogger("wfm.offers")
router = APIRouter(prefix="/offers", tags=["offers"])


@router.post("/apply", response_model=OfferApplyResponse)
def post_apply(
    req: OfferApplyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("wfm_manager")),
) -> OfferApplyResponse:
    def _idempotent(db: Session, offer_id: int) -> OfferApplyResponse:
        row = (
            db.execute(
                text("SELECT id, kind, slots, targets, published_at FROM offers WHERE id = :id"),
                {"id": offer_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(500, "consumed token references missing offer")
        return OfferApplyResponse(
            offer_id=int(row["id"]),
            kind=row["kind"],
            slots=int(row["slots"]),
            n_targets=len(row["targets"] or []),
            published_at=row["published_at"],
        )

    def _write(db: Session, token: Any):
        result = publish_offer(
            db, spec=token.spec, conversation_id=token.conversation_id, actor=user.username
        )
        return result, result.offer_id

    def _notify(db: Session, token: Any, result: Any) -> None:
        notify_offer_published(
            db,
            summary=result.summary,
            offer_id=result.offer_id,
            kind=result.kind,
            conversation_id=token.conversation_id,
        )

    return apply_via_token(
        db,
        req.apply_token,
        consume=consume_offer_token,
        consumed_ref=lambda t: t.consumed_offer_id,
        idempotent_result=_idempotent,
        write=_write,
        mark_consumed=mark_offer_consumed,
        notify=_notify,
        response=lambda r: OfferApplyResponse(
            offer_id=r.offer_id,
            kind=r.kind,  # type: ignore[arg-type]
            slots=r.slots,
            n_targets=r.n_targets,
            published_at=r.published_at,
        ),
    )


@router.get("", response_model=list[OfferLogEntry])
def list_offers(
    since: date | None = None,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[OfferLogEntry]:
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date()
    where = "WHERE published_at >= :since"
    params: dict[str, Any] = {"since": since, "limit": limit}
    if status:
        where += " AND status = :status"
        params["status"] = status

    rows = (
        db.execute(
            text(
                f"""
                SELECT id, kind, target_date, window_start, window_end, slots,
                       targets, policy, message, status, published_at,
                       published_by, undo_window_ends_at, retracted_at
                FROM offers
                {where}
                ORDER BY published_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [
        OfferLogEntry(
            id=int(r["id"]),
            kind=r["kind"],
            target_date=r["target_date"],
            window_start=r["window_start"],
            window_end=r["window_end"],
            slots=int(r["slots"]),
            targets=r["targets"] or [],
            policy=r["policy"],
            message=r["message"],
            status=r["status"],
            published_at=r["published_at"],
            published_by=r["published_by"],
            undo_window_ends_at=r["undo_window_ends_at"],
            retracted_at=r["retracted_at"],
        )
        for r in rows
    ]


@router.post("/{offer_id}/retract", response_model=OfferRetractResponse)
def post_retract(
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("wfm_manager")),
) -> OfferRetractResponse:
    try:
        result = retract_offer(db, offer_id)
    except OfferNotFound:
        raise HTTPException(404, "offer not found")
    except AlreadyRetracted:
        raise HTTPException(409, "offer already retracted")
    except RetractWindowExpired:
        raise HTTPException(409, "retract window has expired (24h ceiling)")

    notify_offer_retracted(
        db, summary=result.summary, offer_id=result.offer_id, conversation_id=None
    )
    db.commit()
    return OfferRetractResponse(offer_id=result.offer_id, retracted_at=result.retracted_at)
