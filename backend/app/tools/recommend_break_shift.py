"""
recommend_break_shift — move a planned break to manage an intraday gap.

Strategy: find the next scheduled break of any agent currently scheduled
to be working, and propose moving it earlier (to free capacity now) or
later (to pull capacity into a forecast spike). Picks the most-junior
agent (by hire_date ASC) on the principle that they're typically the
first to flex. Names policy in title for auditability.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "recommend_break_shift",
    "description": (
        "Recommend shifting an upcoming planned break to manage a real-time "
        "gap — either pull a break forward (free capacity now) or push it "
        "later (cover an upcoming spike). Use when the user asks 'we're short "
        "right now, can we move breaks', 'I need to free 2 agents at 14:00'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["earlier", "later"],
                "description": "earlier = pull break before its scheduled time; later = push it after.",
            },
            "minutes": {
                "type": "integer",
                "minimum": 15,
                "maximum": 120,
                "description": "How many minutes to shift the break.",
            },
            "candidates": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "How many candidate agents to surface.",
            },
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    direction = args.get("direction") or "earlier"
    minutes = int(args.get("minutes") or 30)
    candidates = int(args.get("candidates") or 3)

    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    horizon_end = now + timedelta(hours=4)

    rows = (
        db.execute(
            text(
                """
                SELECT a.full_name, a.employee_id, a.hire_date,
                       seg.start_time, seg.end_time
                FROM shift_segments seg
                JOIN agents a ON a.id = seg.agent_id AND a.active = TRUE
                WHERE seg.segment_type = 'break'
                  AND seg.start_time > :now
                  AND seg.start_time < :horizon
                ORDER BY a.hire_date DESC NULLS LAST, seg.start_time
                LIMIT :limit
                """
            ),
            {"now": now, "horizon": horizon_end, "limit": candidates},
        )
        .mappings()
        .all()
    )

    delta = timedelta(minutes=minutes if direction == "later" else -minutes)
    table_rows = []
    for r in rows:
        new_start = r["start_time"] + delta
        new_end = r["end_time"] + delta
        table_rows.append(
            [
                r["full_name"],
                r["employee_id"],
                f"{r['start_time'].strftime('%H:%M')}–{r['end_time'].strftime('%H:%M')}",
                f"{new_start.strftime('%H:%M')}–{new_end.strftime('%H:%M')}",
                f"{'+' if direction == 'later' else '-'}{minutes}m",
            ]
        )

    return {
        "render": "table",
        "title": (
            f"Break-shift candidates ({direction}, {minutes}m, policy: junior-first) — "
            f"as of {now.strftime('%H:%M')} sim — {len(rows)} candidates"
        ),
        "columns": ["Agent", "ID", "Current break", "Proposed break", "Shift"],
        "rows": table_rows,
    }
