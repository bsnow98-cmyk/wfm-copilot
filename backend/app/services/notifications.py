"""
NotificationSink + DBSink — cherry-pick D, decision D-3.

The sink interface lets v2 add `EmailSink` / `SlackSink` without touching
the call sites in the schedule-apply / undo paths. v1 ships only `DBSink`.

Helpers `notify_schedule_applied` / `notify_schedule_undone` are the call
sites that the apply/undo router invokes — they construct the right payload
shape (uses the existing render contract: `text` for v1) and dispatch
through whichever sink is configured.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.notifications")


@dataclass(frozen=True)
class Notification:
    category: str
    source: str
    payload: dict[str, Any]
    conversation_id: str | None = None
    recipient: str | None = None  # NULL = global feed in v1


class NotificationSink(Protocol):
    def send(self, db: Session, n: Notification) -> str | None:
        """Returns inserted row id, or None on failure (logged, not raised)."""


class DBSink:
    """Writes the notification to the `notifications` table.

    Failures are logged and swallowed — losing a notification must never
    break the apply path that produced it. Same pattern as
    `_persist_message` in the chat router.
    """

    def send(self, db: Session, n: Notification) -> str | None:
        try:
            row_id = db.execute(
                text(
                    """
                    INSERT INTO notifications
                        (recipient, category, source, conversation_id, payload)
                    VALUES (:r, :cat, :src, CAST(:conv AS uuid), CAST(:payload AS jsonb))
                    RETURNING id
                    """
                ),
                {
                    "r": n.recipient,
                    "cat": n.category,
                    "src": n.source,
                    "conv": n.conversation_id,
                    "payload": json.dumps(n.payload, default=str),
                },
            ).scalar_one()
            return str(row_id)
        except Exception:  # noqa: BLE001
            log.exception("DBSink.send failed for category=%s — continuing", n.category)
            return None


# Single global sink. v2 swaps this for a list of sinks.
_default_sink: NotificationSink = DBSink()


def get_default_sink() -> NotificationSink:
    return _default_sink


# --------------------------------------------------------------------------
# Convenience helpers used by the apply / undo paths
# --------------------------------------------------------------------------
def notify_schedule_applied(
    db: Session,
    *,
    summary: str,
    log_id: str,
    schedule_id: int,
    conversation_id: str | None,
) -> str | None:
    return _default_sink.send(
        db,
        Notification(
            category="schedule_applied",
            source="chat_apply",
            conversation_id=conversation_id,
            payload={
                "render": "text",
                "content": summary,
                "log_id": log_id,
                "schedule_id": schedule_id,
            },
        ),
    )


def notify_schedule_undone(
    db: Session,
    *,
    summary: str,
    undo_log_id: str,
    schedule_id: int,
    conversation_id: str | None,
) -> str | None:
    return _default_sink.send(
        db,
        Notification(
            category="schedule_undone",
            source="chat_undo",
            conversation_id=conversation_id,
            payload={
                "render": "text",
                "content": summary,
                "undo_log_id": undo_log_id,
                "schedule_id": schedule_id,
            },
        ),
    )


# --------------------------------------------------------------------------
# Read-side
# --------------------------------------------------------------------------
def list_notifications(
    db: Session, *, limit: int = 50
) -> tuple[list[dict[str, Any]], int]:
    """Returns (rows, unread_count). v1 doesn't filter by recipient — the
    NULL global feed is the only feed."""
    rows = (
        db.execute(
            text(
                """
                SELECT id, created_at, read_at, category, source,
                       conversation_id, payload
                FROM notifications
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    unread = db.execute(
        text("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL")
    ).scalar_one()
    return [dict(r) for r in rows], int(unread)


def mark_read(db: Session, notification_id: str) -> int:
    result = db.execute(
        text(
            """
            UPDATE notifications
            SET read_at = NOW()
            WHERE id = CAST(:id AS uuid) AND read_at IS NULL
            """
        ),
        {"id": notification_id},
    )
    db.commit()
    return int(result.rowcount)


def mark_all_read(db: Session) -> int:
    result = db.execute(
        text("UPDATE notifications SET read_at = NOW() WHERE read_at IS NULL")
    )
    db.commit()
    return int(result.rowcount)
