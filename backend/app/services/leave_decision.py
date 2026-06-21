"""
leave_decision — apply + undo for leave approve/deny, mirroring schedule_change.

Surface #1 of EXECUTION_ROADMAP.md. Decision-by-decision (parallels cherry-pick D):
- Optimistic concurrency: request_version = hash(status, decided_at). A request
  decided out-of-band between preview and apply → 409 (StaleVersionError).
- Idempotency: single-use token (chat_apply_tokens, target_kind='leave').
- Audit: append-only leave_decision_log with before/after JSONB snapshots.
- Undo: 24h window, enforced at undo time. Restores status + reverses the
  PTO ledger row with a compensating 'adjust' entry (ledger stays append-only).
- applied_by/decided_by literal 'demo' until RBAC.

The math the human sees (worst-day margin) is recomputed server-side, never
authored by the LLM — same defense as summarize_change.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.realtime_clock import sim_now

UNDO_WINDOW = timedelta(hours=24)
SHIFT_HOURS = 8.0  # PTO charged per leave day


class StaleVersionError(Exception):
    """request_version no longer matches the live request (decided out-of-band)."""

    def __init__(
        self, your_version: int, current_version: int, request_id: int | None = None
    ) -> None:
        super().__init__(
            f"request_version mismatch (yours={your_version}, current={current_version})"
        )
        self.your_version = your_version
        self.current_version = current_version
        self.request_id = request_id


class RequestNotFound(Exception):
    pass


class ChangeNotFound(Exception):
    pass


class AlreadyUndone(Exception):
    pass


class UndoWindowExpired(Exception):
    pass


@dataclass(frozen=True)
class LeaveRequestInfo:
    request_id: int
    agent_id: int
    full_name: str
    employee_id: str
    leave_type: str
    start_ts: datetime
    end_ts: datetime
    status: str
    decided_at: datetime | None
    decided_by: str | None
    decision_note: str | None


@dataclass(frozen=True)
class DecisionResult:
    log_id: str
    request_id: int
    status: str  # 'approved' | 'denied'
    decided_at: datetime
    ledger_event_id: int | None
    summary: str


# --------------------------------------------------------------------------
# Discovery / versioning
# --------------------------------------------------------------------------
def load_request(db: Session, request_id: int, *, lock: bool = False) -> LeaveRequestInfo | None:
    row = (
        db.execute(
            text(
                f"""
                SELECT lr.id, lr.agent_id, a.full_name, a.employee_id,
                       lr.leave_type, lr.start_ts, lr.end_ts, lr.status,
                       lr.decided_at, lr.decided_by, lr.decision_note
                FROM leave_requests lr
                JOIN agents a ON a.id = lr.agent_id
                WHERE lr.id = :id
                {"FOR UPDATE OF lr" if lock else ""}
                """
            ),
            {"id": request_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return LeaveRequestInfo(
        request_id=int(row["id"]),
        agent_id=int(row["agent_id"]),
        full_name=row["full_name"],
        employee_id=row["employee_id"],
        leave_type=row["leave_type"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        status=row["status"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decision_note=row["decision_note"],
    )


def compute_request_version(status: str, decided_at: datetime | None) -> int:
    """Stable 31-bit hash of the request's decision state. Changes the moment
    someone decides (or re-decides) the request — the optimistic-concurrency
    fingerprint, same construction as compute_schedule_version."""
    payload = f"{status}|{decided_at.isoformat() if decided_at else ''}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def leave_pto_hours(start_ts: datetime, end_ts: datetime) -> float:
    """PTO hours a leave consumes = (calendar days touched) * 8h. The seed
    builds requests as start@09:00 → start + N days + 8h, so the inclusive
    day count is (end.date - start.date) + 1."""
    days = (end_ts.date() - start_ts.date()).days + 1
    return max(days, 1) * SHIFT_HOURS


def worst_day_margin(db: Session, start_ts: datetime, end_ts: datetime) -> int | None:
    """Worst per-day staffing margin (scheduled-1-required) across the window.
    Mirrors recommend_leave_approval's math. None when no staffing data."""
    days = (
        db.execute(
            text(
                """
                SELECT MAX(sr.required_agents) AS required,
                       MAX(sc.scheduled_agents) AS scheduled
                FROM staffing_requirement_intervals sr
                LEFT JOIN schedule_coverage sc
                  ON sc.interval_start = sr.interval_start
                WHERE sr.interval_start >= :start AND sr.interval_start < :end
                GROUP BY (sr.interval_start::date)
                """
            ),
            {"start": start_ts, "end": end_ts},
        )
        .mappings()
        .all()
    )
    worst: int | None = None
    for d in days:
        margin = int((d["scheduled"] or 0) - 1 - (d["required"] or 0))
        worst = margin if worst is None else min(worst, margin)
    return worst


# --------------------------------------------------------------------------
# Summary (server-side, never LLM-authored)
# --------------------------------------------------------------------------
def summarize_decision(
    *,
    decision: str,
    info: LeaveRequestInfo,
    margin: int | None,
    pto_hours: float | None,
) -> str:
    verb = "Approved" if decision == "approve" else "Denied"
    span = f"{info.start_ts.date().isoformat()}→{info.end_ts.date().isoformat()}"
    parts = [f"{verb} {info.leave_type} for {info.full_name} ({info.employee_id}), {span}"]
    if decision == "approve" and pto_hours is not None:
        parts.append(f"−{pto_hours:.0f}h PTO")
    if margin is not None:
        parts.append(f"worst-day margin {margin:+d}")
    return " — ".join(parts)


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------
def apply_decision(
    db: Session,
    *,
    request_id: int,
    expected_version: int,
    decision: str,
    note: str | None,
    conversation_id: str | None,
) -> DecisionResult:
    """Flip a pending leave request to approved/denied inside the caller's txn.

    On approve, writes a PTO 'use' ledger row. Always writes one
    leave_decision_log row. Raises StaleVersionError (→409) if the live
    request_version differs from expected_version, RequestNotFound (→404) if
    the request is gone. The router commits.
    """
    info = load_request(db, request_id, lock=True)
    if info is None:
        raise RequestNotFound(f"leave request {request_id} not found")

    current_version = compute_request_version(info.status, info.decided_at)
    if current_version != expected_version:
        raise StaleVersionError(expected_version, current_version, request_id=request_id)

    before_state = {
        "status": info.status,
        "decided_at": info.decided_at.isoformat() if info.decided_at else None,
        "decided_by": info.decided_by,
        "decision_note": info.decision_note,
    }

    new_status = "approved" if decision == "approve" else "denied"
    decided_at = sim_now(db)
    db.execute(
        text(
            """
            UPDATE leave_requests
            SET status = :status, decided_at = :at,
                decided_by = 'demo', decision_note = :note
            WHERE id = :id
            """
        ),
        {"status": new_status, "at": decided_at, "note": note, "id": request_id},
    )

    ledger_event_id: int | None = None
    pto_hours: float | None = None
    if decision == "approve":
        pto_hours = leave_pto_hours(info.start_ts, info.end_ts)
        prior_balance = float(
            db.execute(
                text(
                    """
                    SELECT balance_after FROM pto_ledger
                    WHERE agent_id = :aid ORDER BY event_ts DESC, id DESC LIMIT 1
                    """
                ),
                {"aid": info.agent_id},
            ).scalar()
            or 0.0
        )
        new_balance = prior_balance - pto_hours
        ledger_event_id = int(
            db.execute(
                text(
                    """
                    INSERT INTO pto_ledger
                        (agent_id, event_ts, event_type, hours, balance_after, note)
                    VALUES
                        (:aid, :at, 'use', :hours, :bal, :note)
                    RETURNING id
                    """
                ),
                {
                    "aid": info.agent_id,
                    "at": decided_at,
                    "hours": -pto_hours,
                    "bal": new_balance,
                    "note": f"Leave approved (request {request_id})",
                },
            ).scalar_one()
        )

    after_state = {
        "status": new_status,
        "decided_at": decided_at.isoformat(),
        "decided_by": "demo",
        "decision_note": note,
    }

    margin = worst_day_margin(db, info.start_ts, info.end_ts)
    summary = summarize_decision(
        decision=decision, info=info, margin=margin, pto_hours=pto_hours
    )

    applied_at = datetime.now(timezone.utc)
    log_id = db.execute(
        text(
            """
            INSERT INTO leave_decision_log
                (applied_at, applied_by, conversation_id, request_id, decision,
                 before_state, after_state, ledger_event_id, undo_window_ends_at)
            VALUES
                (:at, 'demo', CAST(:conv AS uuid), :rid, :decision,
                 CAST(:before AS jsonb), CAST(:after AS jsonb), :ledger, :undo_until)
            RETURNING id
            """
        ),
        {
            "at": applied_at,
            "conv": conversation_id,
            "rid": request_id,
            "decision": decision,
            "before": json.dumps(before_state, default=str),
            "after": json.dumps(after_state, default=str),
            "ledger": ledger_event_id,
            "undo_until": applied_at + UNDO_WINDOW,
        },
    ).scalar_one()

    return DecisionResult(
        log_id=str(log_id),
        request_id=request_id,
        status=new_status,
        decided_at=decided_at,
        ledger_event_id=ledger_event_id,
        summary=summary,
    )


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class UndoResult:
    log_id: str
    request_id: int
    undone_at: datetime
    summary: str


def undo_decision(db: Session, log_id: str) -> UndoResult:
    """Reverse a leave decision within 24h: restore the request's prior state
    and append a compensating PTO ledger row. Marks the log row undone_at.

    Raises ChangeNotFound / AlreadyUndone / UndoWindowExpired.
    """
    row = (
        db.execute(
            text(
                """
                SELECT id, request_id, decision, before_state, after_state,
                       ledger_event_id, undo_window_ends_at, undone_at
                FROM leave_decision_log
                WHERE id = CAST(:id AS uuid)
                FOR UPDATE
                """
            ),
            {"id": log_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ChangeNotFound(f"leave_decision_log {log_id} not found")
    if row["undone_at"] is not None:
        raise AlreadyUndone(f"leave_decision_log {log_id} already undone")
    if row["undo_window_ends_at"] < datetime.now(timezone.utc):
        raise UndoWindowExpired(f"leave_decision_log {log_id} is past the 24h undo window")

    request_id = int(row["request_id"])
    before = row["before_state"] or {}

    # Restore the request to its pre-decision state.
    db.execute(
        text(
            """
            UPDATE leave_requests
            SET status = :status, decided_at = :at,
                decided_by = :by, decision_note = :note
            WHERE id = :id
            """
        ),
        {
            "status": before.get("status", "pending"),
            "at": before.get("decided_at"),
            "by": before.get("decided_by"),
            "note": before.get("decision_note"),
            "id": request_id,
        },
    )

    # Reverse the PTO consumption with a compensating ledger row (append-only).
    if row["ledger_event_id"] is not None:
        used = db.execute(
            text("SELECT agent_id, hours FROM pto_ledger WHERE id = :id"),
            {"id": int(row["ledger_event_id"])},
        ).mappings().first()
        if used is not None:
            give_back = -float(used["hours"])  # original was negative; restore it
            at = sim_now(db)
            prior_balance = float(
                db.execute(
                    text(
                        """
                        SELECT balance_after FROM pto_ledger
                        WHERE agent_id = :aid ORDER BY event_ts DESC, id DESC LIMIT 1
                        """
                    ),
                    {"aid": int(used["agent_id"])},
                ).scalar()
                or 0.0
            )
            db.execute(
                text(
                    """
                    INSERT INTO pto_ledger
                        (agent_id, event_ts, event_type, hours, balance_after, note)
                    VALUES
                        (:aid, :at, 'adjust', :hours, :bal, :note)
                    """
                ),
                {
                    "aid": int(used["agent_id"]),
                    "at": at,
                    "hours": give_back,
                    "bal": prior_balance + give_back,
                    "note": f"Reversed leave approval (undo of {log_id})",
                },
            )

    undone_at = datetime.now(timezone.utc)
    db.execute(
        text(
            """
            UPDATE leave_decision_log
            SET undone_at = :at WHERE id = CAST(:id AS uuid)
            """
        ),
        {"at": undone_at, "id": log_id},
    )

    summary = f"Undid leave {row['decision']} for request {request_id}."
    return UndoResult(
        log_id=str(row["id"]),
        request_id=request_id,
        undone_at=undone_at,
        summary=summary,
    )
