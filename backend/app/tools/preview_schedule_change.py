"""
preview_schedule_change tool — read-only diff preview.

Loads the existing schedule for a date and returns the gantt with one or more
proposed segment changes overlaid. PURE PREVIEW — does not write to the DB.
The mutating apply path is deferred (TODOS — D).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Same map as get_schedule. Kept local rather than imported to avoid a coupling
# you'd have to remember.
_ACTIVITY_MAP = {
    "work": "available",
    "break": "break",
    "lunch": "lunch",
    "training": "training",
    "meeting": "meeting",
    "off": "off",
}

definition: dict[str, Any] = {
    "name": "preview_schedule_change",
    "description": (
        "Preview (read-only) what the schedule would look like for a date with "
        "one or more proposed segment changes overlaid. Use when the user asks "
        "'what if Adams goes to lunch at 13:00 instead of 12:30'. Does NOT "
        "modify the schedule."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD.",
            },
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "External employee_id from the agents table.",
                        },
                        "start": {"type": "string", "description": "ISO datetime."},
                        "end": {"type": "string", "description": "ISO datetime."},
                        "activity": {
                            "type": "string",
                            "enum": [
                                "available",
                                "break",
                                "lunch",
                                "training",
                                "meeting",
                                "shrinkage",
                                "off",
                            ],
                        },
                    },
                    "required": ["agent_id", "start", "end", "activity"],
                },
                "minItems": 1,
            },
        },
        "required": ["date", "changes"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    target = date.fromisoformat(args["date"])
    changes: list[dict[str, Any]] = args["changes"]

    # Discover the schedule the change targets (cherry-pick D — needed to
    # mint an apply_token that pins the write to a specific schedule_id).
    from app.services.apply_tokens import issue_token
    from app.services.schedule_change import (
        compute_schedule_version,
        find_schedule_for_date,
    )

    schedule_id = find_schedule_for_date(db, target)

    day_start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    rows = (
        db.execute(
            text(
                """
                SELECT a.employee_id, a.full_name,
                       s.segment_type, s.start_time, s.end_time
                FROM shift_segments s
                JOIN agents a ON a.id = s.agent_id
                WHERE s.start_time < :end AND s.end_time > :start
                ORDER BY a.full_name, s.start_time
                """
            ),
            {"start": day_start, "end": day_end},
        )
        .mappings()
        .all()
    )

    by_agent: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["employee_id"]
        if key not in by_agent:
            by_agent[key] = {"id": key, "name": r["full_name"], "segments": []}
        by_agent[key]["segments"].append(
            {
                "start": r["start_time"].isoformat(),
                "end": r["end_time"].isoformat(),
                "activity": _ACTIVITY_MAP.get(r["segment_type"], "shrinkage"),
            }
        )

    # Apply proposed changes. Each change replaces overlapping segments for the
    # named agent. Agents not in the existing roster get an entry with just the
    # proposed segment so the user can still see the preview.
    for ch in changes:
        agent_id = ch["agent_id"]
        proposed_start = datetime.fromisoformat(ch["start"])
        proposed_end = datetime.fromisoformat(ch["end"])
        if agent_id not in by_agent:
            by_agent[agent_id] = {
                "id": agent_id,
                "name": agent_id,
                "segments": [],
            }
        # Drop any existing segment that overlaps the proposed window.
        kept = [
            s
            for s in by_agent[agent_id]["segments"]
            if not _overlaps(s["start"], s["end"], proposed_start, proposed_end)
        ]
        kept.append(
            {
                "start": ch["start"],
                "end": ch["end"],
                "activity": ch["activity"],
            }
        )
        kept.sort(key=lambda s: s["start"])
        by_agent[agent_id]["segments"] = kept

    response: dict[str, Any] = {
        "render": "gantt",
        "date": target.isoformat(),
        "agents": list(by_agent.values()),
    }

    # Cherry-pick D — embed an apply_token + schedule_version when there's a
    # real schedule to write into. Frontend renders the Apply button only
    # when both fields are present. If no schedule covers this date, return
    # the gantt without the apply affordance — the user gets a preview but
    # can't write anything that wouldn't have a target.
    if schedule_id is not None:
        affected = sorted({c["agent_id"] for c in changes})
        version = compute_schedule_version(db, schedule_id, affected, target)
        token = issue_token(
            db,
            schedule_id=schedule_id,
            schedule_version=version,
            change_set=changes,
        )
        db.commit()  # tokens persist immediately so a subsequent apply can find them
        response["apply_token"] = token.token
        response["schedule_version"] = version

    return response


def _overlaps(a_start: str, a_end: str, b_start: datetime, b_end: datetime) -> bool:
    a0 = datetime.fromisoformat(a_start)
    a1 = datetime.fromisoformat(a_end)
    return a0 < b_end and a1 > b_start
