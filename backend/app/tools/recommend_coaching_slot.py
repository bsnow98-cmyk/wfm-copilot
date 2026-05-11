"""
recommend_coaching_slot — find a 45-min window in the next 7 days where
a 1:1 coaching session won't break SL.

Picks the interval with the largest (scheduled_agents - required_agents)
margin during business hours, then returns 3 candidate slots.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "recommend_coaching_slot",
    "description": (
        "Recommend 3 candidate 45-min coaching slots in the next 7 days "
        "based on staffing margin. Use when the user asks 'when can I "
        "coach <agent>', 'best time to pull <agent> aside', 'find a "
        "coaching slot'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string"},
            "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 120},
        },
        "required": ["employee_id"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    eid = args["employee_id"]
    duration_minutes = int(args.get("duration_minutes") or 45)

    agent = (
        db.execute(
            text("SELECT id, full_name FROM agents WHERE employee_id = :eid"),
            {"eid": eid},
        )
        .mappings()
        .one_or_none()
    )
    if not agent:
        return {"render": "error", "message": "Agent not found.", "code": "NO_AGENT"}

    # Candidate intervals where agent is scheduled to be working and there's
    # >= 2 staffing margin.
    rows = (
        db.execute(
            text(
                """
                SELECT seg.start_time AS slot_start,
                       LEAST(seg.end_time, seg.start_time + INTERVAL '45 minutes') AS slot_end,
                       sc.scheduled_agents - sr.required_agents AS margin
                FROM shift_segments seg
                JOIN staffing_requirement_intervals sr
                  ON sr.interval_start >= seg.start_time
                 AND sr.interval_start < seg.end_time
                LEFT JOIN schedule_coverage sc ON sc.interval_start = sr.interval_start
                WHERE seg.agent_id = :aid
                  AND seg.segment_type = 'work'
                  AND seg.start_time >= sim_now()
                  AND seg.start_time < sim_now() + INTERVAL '7 days'
                  AND sc.scheduled_agents - sr.required_agents >= 2
                ORDER BY (sc.scheduled_agents - sr.required_agents) DESC, seg.start_time
                LIMIT 3
                """
            ),
            {"aid": agent["id"]},
        )
        .mappings()
        .all()
    )
    if not rows:
        return {
            "render": "text",
            "content": (
                f"No comfortable coaching slot found for {agent['full_name']} in "
                f"the next 7 days (staffing margin < 2 on every working interval)."
            ),
        }

    table_rows = [
        [
            i,
            r["slot_start"].strftime("%a %Y-%m-%d %H:%M"),
            f"{duration_minutes}m",
            int(r["margin"] or 0),
        ]
        for i, r in enumerate(rows, start=1)
    ]
    return {
        "render": "table",
        "title": f"Coaching slot candidates — {agent['full_name']} ({eid}) — {duration_minutes}m",
        "columns": ["Rank", "When", "Duration", "Staffing margin"],
        "rows": table_rows,
    }
