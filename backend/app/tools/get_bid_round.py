"""
get_bid_round — status + per-week capacity + bid counts for a vacation bid round.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_bid_round",
    "description": (
        "Show a vacation bid round: status, bidding window, and per-week capacity "
        "with how many agents bid each week. Use when the user asks 'show the "
        "vacation bid round', 'how's the bid looking', 'which weeks are "
        "oversubscribed'. Defaults to the most recent round."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"round_id": {"type": "integer"}},
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    round_id = args.get("round_id")
    if round_id is None:
        round_id = db.execute(
            text("SELECT id FROM bid_rounds ORDER BY created_at DESC LIMIT 1")
        ).scalar_one_or_none()
        if round_id is None:
            return {"render": "error", "message": "No bid rounds exist yet.", "code": "NO_ROUND"}

    rnd = (
        db.execute(
            text(
                """
                SELECT id, name, status, season_start, season_end, max_weeks_per_agent,
                       awarded_at, published_at
                FROM bid_rounds WHERE id = :id
                """
            ),
            {"id": int(round_id)},
        )
        .mappings()
        .first()
    )
    if rnd is None:
        return {"render": "error", "message": f"Bid round {round_id} not found.", "code": "NO_ROUND"}

    rows = (
        db.execute(
            text(
                """
                SELECT c.week_start, c.slots,
                       COUNT(b.id) AS bids,
                       COUNT(*) FILTER (WHERE b.rank = 1) AS first_choice
                FROM bid_week_capacity c
                LEFT JOIN vacation_bids b
                  ON b.round_id = c.round_id AND b.week_start = c.week_start
                WHERE c.round_id = :id
                GROUP BY c.week_start, c.slots
                ORDER BY c.week_start
                """
            ),
            {"id": int(round_id)},
        )
        .mappings()
        .all()
    )

    table_rows = [
        [
            r["week_start"].isoformat(),
            int(r["slots"]),
            int(r["bids"]),
            int(r["first_choice"]),
            "OVERSUBSCRIBED" if int(r["first_choice"]) > int(r["slots"]) else "ok",
        ]
        for r in rows
    ]
    return {
        "render": "table",
        "title": (
            f"{rnd['name']} — status: {rnd['status']} "
            f"({rnd['season_start']}→{rnd['season_end']}, max {rnd['max_weeks_per_agent']} wk/agent)"
        ),
        "columns": ["Week", "Slots", "Bids", "1st-choice", "Demand"],
        "rows": table_rows,
    }
