"""
schedule_change — apply + undo, with optimistic concurrency and audit logging.

Cherry-pick D core. Decision-by-decision:
- D-4: stale schedule_version → 409 with both versions side-by-side.
- D-1: undo window 24h, enforced at undo time (not via a sweeper).
- D-2: applied_by stored as literal "demo" until RBAC.
- D-5: write goes through chat_apply_tokens consumption (single-use).
- D-6: duplicate apply with consumed token returns the original log_id (200 OK).

The activity → segment_type mapping mirrors the inverse map in get_schedule.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.schedule_change")

UNDO_WINDOW = timedelta(hours=24)

# Frontend gantt activity → DB segment_type. Inverse of get_schedule's map,
# kept here to avoid a circular import.
_ACTIVITY_TO_SEGMENT_TYPE = {
    "available": "work",
    "break": "break",
    "lunch": "lunch",
    "training": "training",
    "meeting": "meeting",
    "shrinkage": "shrinkage",
    "off": "off",
}


@dataclass
class ChangeSetItem:
    agent_employee_id: str
    start: datetime
    end: datetime
    activity: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChangeSetItem":
        return cls(
            agent_employee_id=d["agent_id"],
            start=_parse_dt(d["start"]),
            end=_parse_dt(d["end"]),
            activity=d["activity"],
        )


class StaleVersionError(Exception):
    """Raised when the supplied schedule_version doesn't match the live one."""

    def __init__(self, your_version: int, current_version: int) -> None:
        super().__init__(
            f"schedule_version mismatch (yours={your_version}, current={current_version})"
        )
        self.your_version = your_version
        self.current_version = current_version


class UndoWindowExpired(Exception):
    pass


class AlreadyUndone(Exception):
    pass


class ChangeNotFound(Exception):
    pass


# --------------------------------------------------------------------------
# Discovery — figure out which schedule a change targets, and its version
# --------------------------------------------------------------------------
def find_schedule_for_date(db: Session, target_date: date) -> int | None:
    """Most recent schedule whose date range covers `target_date`.

    Returns None if no schedule covers the date — the preview tool surfaces
    this as a render:'error' upstream so the user sees a clean message.
    """
    return db.execute(
        text(
            """
            SELECT id FROM schedules
            WHERE :d BETWEEN start_date AND end_date
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"d": target_date},
    ).scalar_one_or_none()


def compute_schedule_version(
    db: Session,
    schedule_id: int,
    affected_employee_ids: list[str],
    target_date: date,
) -> int:
    """Stable int hash of the affected segments' current state.

    Changes whenever the underlying segments do — two previews against
    the same data produce the same int; any external edit changes it.
    Uses CRC32 (8-byte fingerprint truncated to 31 bits) — collision odds
    are fine for an in-process concurrency token, and we don't store it.
    """
    if not affected_employee_ids:
        return 0
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    rows = db.execute(
        text(
            """
            SELECT a.employee_id, s.segment_type, s.start_time, s.end_time
            FROM shift_segments s
            JOIN agents a ON a.id = s.agent_id
            WHERE s.schedule_id = :sched
              AND a.employee_id = ANY(:emps)
              AND s.start_time < :day_end AND s.end_time > :day_start
            ORDER BY a.employee_id, s.start_time
            """
        ),
        {
            "sched": schedule_id,
            "emps": affected_employee_ids,
            "day_start": day_start,
            "day_end": day_end,
        },
    ).all()
    payload = "|".join(
        f"{r[0]},{r[1]},{r[2].isoformat()},{r[3].isoformat()}" for r in rows
    )
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    # Take first 4 bytes as a signed 31-bit int (Postgres INT range).
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def snapshot_state(
    db: Session,
    schedule_id: int,
    affected_employee_ids: list[str],
    target_date: date,
) -> list[dict[str, Any]]:
    """Read current segments for the affected agents. Used for before/after
    snapshots in the audit log."""
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    rows = (
        db.execute(
            text(
                """
                SELECT a.employee_id, a.full_name,
                       s.segment_type, s.start_time, s.end_time
                FROM shift_segments s
                JOIN agents a ON a.id = s.agent_id
                WHERE s.schedule_id = :sched
                  AND a.employee_id = ANY(:emps)
                  AND s.start_time < :day_end AND s.end_time > :day_start
                ORDER BY a.full_name, s.start_time
                """
            ),
            {
                "sched": schedule_id,
                "emps": affected_employee_ids,
                "day_start": day_start,
                "day_end": day_end,
            },
        )
        .mappings()
        .all()
    )
    by_agent: dict[str, dict[str, Any]] = {}
    for r in rows:
        eid = r["employee_id"]
        if eid not in by_agent:
            by_agent[eid] = {"id": eid, "name": r["full_name"], "segments": []}
        by_agent[eid]["segments"].append(
            {
                "start": r["start_time"].isoformat(),
                "end": r["end_time"].isoformat(),
                "activity": _segment_type_to_activity(r["segment_type"]),
            }
        )
    return list(by_agent.values())


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------
def apply_change(
    db: Session,
    *,
    schedule_id: int,
    expected_version: int,
    change_set: list[dict[str, Any]],
    conversation_id: str | None,
    user_msg_id: str | None,
) -> str:
    """Write the change set + insert the audit log row + return log_id.

    Raises StaleVersionError if the live schedule_version differs from
    expected_version (caller maps to 409).

    All inserts/updates run inside the caller's transaction. The router is
    responsible for db.commit() so the chat_apply_tokens consume + this
    write + the notification dispatch all land atomically.
    """
    items = [ChangeSetItem.from_dict(c) for c in change_set]
    target_date = items[0].start.date() if items else date.today()
    affected = sorted({i.agent_employee_id for i in items})

    # Concurrency check.
    current_version = compute_schedule_version(db, schedule_id, affected, target_date)
    if current_version != expected_version:
        raise StaleVersionError(expected_version, current_version)

    before = snapshot_state(db, schedule_id, affected, target_date)

    # Resolve external employee_ids → internal agent.id.
    agent_id_by_emp = _resolve_agent_ids(db, affected)

    # Apply each change: drop overlapping segments for that agent, insert
    # the new one. Same overlap rule as the read-side preview.
    for item in items:
        agent_id = agent_id_by_emp.get(item.agent_employee_id)
        if agent_id is None:
            log.warning("apply_change: unknown employee_id=%s — creating without FK row", item.agent_employee_id)
            continue
        seg_type = _ACTIVITY_TO_SEGMENT_TYPE.get(item.activity, item.activity)
        db.execute(
            text(
                """
                DELETE FROM shift_segments
                WHERE schedule_id = :sched
                  AND agent_id = :aid
                  AND start_time < :end
                  AND end_time   > :start
                """
            ),
            {"sched": schedule_id, "aid": agent_id, "start": item.start, "end": item.end},
        )
        db.execute(
            text(
                """
                INSERT INTO shift_segments
                    (schedule_id, agent_id, segment_type, start_time, end_time)
                VALUES (:sched, :aid, :stype, :start, :end)
                """
            ),
            {
                "sched": schedule_id,
                "aid": agent_id,
                "stype": seg_type,
                "start": item.start,
                "end": item.end,
            },
        )

    after = snapshot_state(db, schedule_id, affected, target_date)
    applied_at = datetime.now(timezone.utc)
    log_id = db.execute(
        text(
            """
            INSERT INTO schedule_change_log
                (applied_at, applied_by, conversation_id, user_msg_id,
                 schedule_id, change_set, before_state, after_state,
                 undo_window_ends_at)
            VALUES
                (:at, 'demo', CAST(:conv AS uuid), CAST(:umid AS uuid),
                 :sched, CAST(:cs AS jsonb), CAST(:before AS jsonb), CAST(:after AS jsonb),
                 :undo_until)
            RETURNING id
            """
        ),
        {
            "at": applied_at,
            "conv": conversation_id,
            "umid": user_msg_id,
            "sched": schedule_id,
            "cs": json.dumps(change_set, default=str),
            "before": json.dumps(before, default=str),
            "after": json.dumps(after, default=str),
            "undo_until": applied_at + UNDO_WINDOW,
        },
    ).scalar_one()
    return str(log_id)


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------
def undo_change(
    db: Session,
    log_id: str,
    *,
    conversation_id: str | None = None,
) -> tuple[str, datetime]:
    """Reverse an applied change. Writes a new log row whose change_set is
    the inverse and updates the original row's undone_at + undone_by_log_id.

    Raises:
      ChangeNotFound — log_id doesn't exist.
      UndoWindowExpired — past the 24h ceiling.
      AlreadyUndone — already reversed.
    """
    row = (
        db.execute(
            text(
                """
                SELECT id, schedule_id, change_set, before_state, after_state,
                       undo_window_ends_at, undone_at
                FROM schedule_change_log
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
        raise ChangeNotFound(f"log_id {log_id} not found")
    if row["undone_at"] is not None:
        raise AlreadyUndone(f"log_id {log_id} already undone")
    if row["undo_window_ends_at"] < datetime.now(timezone.utc):
        raise UndoWindowExpired(f"log_id {log_id} is past the 24h undo window")

    schedule_id = int(row["schedule_id"])
    before_state = row["before_state"] or []
    after_state = row["after_state"] or []
    target_date = _date_from_state(before_state) or date.today()
    affected = sorted({a["id"] for a in before_state})

    # Restore the before_state: for each agent, drop the day's segments and
    # re-insert what was there originally.
    agent_id_by_emp = _resolve_agent_ids(db, affected)
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    for emp_id in affected:
        agent_id = agent_id_by_emp.get(emp_id)
        if agent_id is None:
            continue
        db.execute(
            text(
                """
                DELETE FROM shift_segments
                WHERE schedule_id = :sched AND agent_id = :aid
                  AND start_time < :day_end AND end_time > :day_start
                """
            ),
            {"sched": schedule_id, "aid": agent_id, "day_start": day_start, "day_end": day_end},
        )
    for agent in before_state:
        agent_id = agent_id_by_emp.get(agent["id"])
        if agent_id is None:
            continue
        for seg in agent.get("segments", []):
            db.execute(
                text(
                    """
                    INSERT INTO shift_segments
                        (schedule_id, agent_id, segment_type, start_time, end_time)
                    VALUES (:sched, :aid, :stype, :start, :end)
                    """
                ),
                {
                    "sched": schedule_id,
                    "aid": agent_id,
                    "stype": _ACTIVITY_TO_SEGMENT_TYPE.get(seg["activity"], seg["activity"]),
                    "start": _parse_dt(seg["start"]),
                    "end": _parse_dt(seg["end"]),
                },
            )

    # Insert undo log row.
    undone_at = datetime.now(timezone.utc)
    undo_log_id = db.execute(
        text(
            """
            INSERT INTO schedule_change_log
                (applied_at, applied_by, conversation_id, schedule_id,
                 change_set, before_state, after_state,
                 undo_window_ends_at)
            VALUES
                (:at, 'demo', CAST(:conv AS uuid), :sched,
                 CAST(:cs AS jsonb), CAST(:before AS jsonb), CAST(:after AS jsonb),
                 :undo_until)
            RETURNING id
            """
        ),
        {
            "at": undone_at,
            "conv": conversation_id,
            "sched": schedule_id,
            "cs": json.dumps([{"undo_of": str(row["id"])}], default=str),
            "before": json.dumps(after_state, default=str),
            "after": json.dumps(before_state, default=str),
            "undo_until": undone_at + UNDO_WINDOW,
        },
    ).scalar_one()
    db.execute(
        text(
            """
            UPDATE schedule_change_log
            SET undone_at = :at, undone_by_log_id = CAST(:undo_id AS uuid)
            WHERE id = CAST(:id AS uuid)
            """
        ),
        {"at": undone_at, "undo_id": str(undo_log_id), "id": log_id},
    )
    return str(undo_log_id), undone_at


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------
def _resolve_agent_ids(db: Session, employee_ids: list[str]) -> dict[str, int]:
    rows = db.execute(
        text("SELECT id, employee_id FROM agents WHERE employee_id = ANY(:emps)"),
        {"emps": employee_ids},
    ).all()
    return {r[1]: int(r[0]) for r in rows}


def _segment_type_to_activity(seg_type: str) -> str:
    inverse = {v: k for k, v in _ACTIVITY_TO_SEGMENT_TYPE.items()}
    return inverse.get(seg_type, "shrinkage")


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _date_from_state(state: list[dict[str, Any]]) -> date | None:
    for agent in state:
        for seg in agent.get("segments", []):
            return _parse_dt(seg["start"]).date()
    return None
