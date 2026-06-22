"""
offer — publish + retract OT/VTO offers (Surface #2 of EXECUTION_ROADMAP.md).

Publish-only v1: an offer is created (not an edit), so there's no optimistic
concurrency to enforce — the offers row is its own append-only audit record
(status open -> retracted) with a 24h retract window. Idempotency comes from
the single-use token. published_by literal 'demo' until RBAC.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

UNDO_WINDOW = timedelta(hours=24)


class OfferNotFound(Exception):
    pass


class AlreadyRetracted(Exception):
    pass


class RetractWindowExpired(Exception):
    pass


@dataclass(frozen=True)
class OfferResult:
    offer_id: int
    kind: str
    slots: int
    n_targets: int
    published_at: datetime
    summary: str


@dataclass(frozen=True)
class RetractResult:
    offer_id: int
    retracted_at: datetime
    summary: str


def summarize_offer(spec: dict[str, Any]) -> str:
    kind = spec.get("kind", "ot").upper()
    targets = spec.get("targets") or []
    win = ""
    try:
        ws = datetime.fromisoformat(spec["window_start"]).strftime("%H:%M")
        we = datetime.fromisoformat(spec["window_end"]).strftime("%H:%M")
        win = f", {spec['target_date']} {ws}–{we}"
    except (KeyError, ValueError):
        pass
    return (
        f"Published {kind} offer to {len(targets)} agent(s) "
        f"for {spec.get('slots', len(targets))} slot(s){win}"
    )


def publish_offer(
    db: Session,
    *,
    spec: dict[str, Any],
    conversation_id: str | None,
    actor: str = "demo",
) -> OfferResult:
    """Insert one offers row (status='open') from a previewed spec, inside the
    caller's transaction. The router commits."""
    published_at = datetime.now(timezone.utc)
    targets = spec.get("targets") or []
    offer_id = int(
        db.execute(
            text(
                """
                INSERT INTO offers
                    (kind, schedule_id, target_date, window_start, window_end,
                     targets, slots, policy, message, status,
                     published_at, published_by, conversation_id, undo_window_ends_at)
                VALUES
                    (:kind, :sched, :tdate, :wstart, :wend,
                     CAST(:targets AS jsonb), :slots, :policy, :message, 'open',
                     :at, :actor, CAST(:conv AS uuid), :undo_until)
                RETURNING id
                """
            ),
            {
                "actor": actor,
                "kind": spec["kind"],
                "sched": spec.get("schedule_id"),
                "tdate": spec["target_date"],
                "wstart": spec["window_start"],
                "wend": spec["window_end"],
                "targets": json.dumps(targets, default=str),
                "slots": int(spec.get("slots") or len(targets)),
                "policy": spec.get("policy"),
                "message": spec.get("message"),
                "at": published_at,
                "conv": conversation_id,
                "undo_until": published_at + UNDO_WINDOW,
            },
        ).scalar_one()
    )
    return OfferResult(
        offer_id=offer_id,
        kind=spec["kind"],
        slots=int(spec.get("slots") or len(targets)),
        n_targets=len(targets),
        published_at=published_at,
        summary=summarize_offer(spec),
    )


def retract_offer(db: Session, offer_id: int) -> RetractResult:
    """Retract a published offer within 24h. Raises OfferNotFound /
    AlreadyRetracted / RetractWindowExpired."""
    row = (
        db.execute(
            text(
                """
                SELECT id, kind, status, undo_window_ends_at, retracted_at, targets
                FROM offers
                WHERE id = :id
                FOR UPDATE
                """
            ),
            {"id": offer_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise OfferNotFound(f"offer {offer_id} not found")
    if row["status"] == "retracted" or row["retracted_at"] is not None:
        raise AlreadyRetracted(f"offer {offer_id} already retracted")
    if row["undo_window_ends_at"] < datetime.now(timezone.utc):
        raise RetractWindowExpired(f"offer {offer_id} is past the 24h retract window")

    retracted_at = datetime.now(timezone.utc)
    db.execute(
        text("UPDATE offers SET status='retracted', retracted_at=:at WHERE id=:id"),
        {"at": retracted_at, "id": offer_id},
    )
    n = len(row["targets"] or [])
    summary = f"Retracted {row['kind'].upper()} offer #{offer_id} ({n} agents)."
    return RetractResult(offer_id=offer_id, retracted_at=retracted_at, summary=summary)
