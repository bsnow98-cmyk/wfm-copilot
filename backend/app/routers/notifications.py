"""
/notifications router — cherry-pick D, decision D-3.

Endpoints:
  GET  /notifications                 — feed + unread count for the badge
  POST /notifications/{id}/read       — mark one read
  POST /notifications/read-all        — zero out the badge
  POST /notifications/daily-briefing  — compose + post the morning briefing
                                        (cron-triggered; idempotent per day)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.notifications import (
    MarkReadResponse,
    NotificationOut,
    NotificationsListResponse,
)
from app.services.notifications import (
    list_notifications,
    mark_all_read,
    mark_read,
)

log = logging.getLogger("wfm.notifications.router")
router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.post("/daily-briefing")
def post_daily_briefing(
    force: bool = False, db: Session = Depends(get_db)
) -> dict:
    """Compose today's briefing and post it to the feed.

    Behind the global Basic-auth gate like everything else. `force=true`
    bypasses the once-per-day guard (manual re-runs, demos).
    """
    from app.services.daily_briefing import generate_daily_briefing

    try:
        return generate_daily_briefing(db, force=force)
    except Exception as exc:  # noqa: BLE001 — cron caller needs a clean signal
        log.exception("Daily briefing failed")
        raise HTTPException(502, f"briefing generation failed: {type(exc).__name__}")


@router.get("", response_model=NotificationsListResponse)
def get_notifications(
    limit: int = 50, db: Session = Depends(get_db)
) -> NotificationsListResponse:
    rows, unread = list_notifications(db, limit=limit)
    items = [
        NotificationOut(
            id=str(r["id"]),
            created_at=r["created_at"],
            read_at=r["read_at"],
            category=r["category"],
            source=r["source"],
            conversation_id=str(r["conversation_id"]) if r["conversation_id"] else None,
            payload=r["payload"],
        )
        for r in rows
    ]
    return NotificationsListResponse(items=items, unread_count=unread)


@router.post("/{notification_id}/read", response_model=MarkReadResponse)
def post_mark_read(
    notification_id: str, db: Session = Depends(get_db)
) -> MarkReadResponse:
    n = mark_read(db, notification_id)
    if n == 0:
        # Either unknown id or already read. The frontend can't tell from
        # the badge math, so 404 only if literally not found.
        # Cheap to skip the existence check; treat 0 as already-read.
        pass
    return MarkReadResponse(marked=n)


@router.post("/read-all", response_model=MarkReadResponse)
def post_mark_all_read(db: Session = Depends(get_db)) -> MarkReadResponse:
    return MarkReadResponse(marked=mark_all_read(db))
