"""
check_training_impact — would pulling N agents for training break SL?

Given a proposed training event (existing event_id, or ad-hoc start/end +
n_attendees), compute the intervals it spans and report:
  - max required_agents across intervals
  - currently scheduled (excluding the would-be attendees)
  - margin (scheduled - n_attendees - required)
Verdict: OK / WARN / FAIL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "check_training_impact",
    "description": (
        "Check the staffing/SL impact of pulling agents for a training "
        "event. Provide event_id OR (start_ts, end_ts, n_attendees). "
        "Returns per-interval required vs scheduled-minus-attendees with "
        "an OK/WARN/FAIL verdict. Use when the user asks 'can we pull 5 "
        "agents for training Wednesday at 14:00', 'training impact'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "event_id": {"type": "integer"},
            "start_ts": {"type": "string"},
            "end_ts": {"type": "string"},
            "n_attendees": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    if args.get("event_id"):
        ev = (
            db.execute(
                text(
                    """
                    SELECT te.start_ts, te.end_ts, te.title,
                           (SELECT COUNT(*) FROM training_attendees ta WHERE ta.training_event_id = te.id) AS n
                    FROM training_events te WHERE te.id = :id
                    """
                ),
                {"id": int(args["event_id"])},
            )
            .mappings()
            .one_or_none()
        )
        if not ev:
            return {"render": "error", "message": "Training event not found.", "code": "NO_EVENT"}
        start_ts, end_ts, title, n = (
            ev["start_ts"],
            ev["end_ts"],
            ev["title"],
            int(ev["n"] or 0),
        )
    else:
        if not (args.get("start_ts") and args.get("end_ts") and args.get("n_attendees")):
            return {
                "render": "error",
                "message": "Provide event_id OR (start_ts, end_ts, n_attendees).",
                "code": "BAD_ARGS",
            }
        start_ts = datetime.fromisoformat(args["start_ts"]).replace(tzinfo=timezone.utc)
        end_ts = datetime.fromisoformat(args["end_ts"]).replace(tzinfo=timezone.utc)
        n = int(args["n_attendees"])
        title = f"Ad-hoc event ({n} attendees)"

    intervals = (
        db.execute(
            text(
                """
                SELECT sr.interval_start,
                       sr.required_agents,
                       COALESCE(sc.scheduled_agents, 0) AS scheduled
                FROM staffing_requirement_intervals sr
                LEFT JOIN schedule_coverage sc ON sc.interval_start = sr.interval_start
                WHERE sr.interval_start >= :start AND sr.interval_start < :end
                ORDER BY sr.interval_start
                """
            ),
            {"start": start_ts, "end": end_ts},
        )
        .mappings()
        .all()
    )
    if not intervals:
        return {"render": "error", "message": "No staffing data for this window.", "code": "NO_STAFFING"}

    rows: list[list[Any]] = []
    worst = None
    for iv in intervals:
        req = int(iv["required_agents"] or 0)
        sched = int(iv["scheduled"] or 0)
        after = sched - n
        margin = after - req
        v = "OK" if margin >= 2 else ("WARN" if margin >= 0 else "FAIL")
        worst = v if worst is None else _worst_of(worst, v)
        rows.append([iv["interval_start"].strftime("%H:%M"), req, sched, after, margin, v])

    return {
        "render": "table",
        "title": (
            f"Training impact — {title}, "
            f"{start_ts.strftime('%Y-%m-%d %H:%M')}→{end_ts.strftime('%H:%M')}, "
            f"{n} attendees — verdict: {worst}"
        ),
        "columns": ["Interval", "Required", "Scheduled", "After", "Margin", "Verdict"],
        "rows": rows,
    }


def _worst_of(a: str, b: str) -> str:
    order = {"OK": 0, "WARN": 1, "FAIL": 2}
    return a if order[a] >= order[b] else b
