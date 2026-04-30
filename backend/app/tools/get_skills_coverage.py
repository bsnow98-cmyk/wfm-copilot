"""
get_skills_coverage tool — Phase 8 Stage 2.

Answers "how is each skill covered today?" — the primary user question once
multi-skill staffing is real. Returns a `table` render with one row per
skill: required (with discount), available primaries, secondary credit FTE,
and shortfall.

Read-only: this tool computes against the latest completed per-skill
forecast for the queue. It does NOT staff write or trigger CP-SAT — those
are separate paths.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_skills_coverage",
    "description": (
        "For each skill in a queue, show required agents (with substitution "
        "discount applied), primary-skilled agents available, and the "
        "secondary-credit FTE we expect from cross-skilled agents. Use when "
        "the user asks 'how is each skill covered today?' or 'where are we "
        "short on skills?'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {
                "type": "string",
                "description": "Queue name. The tool finds per-skill forecasts under this queue.",
            },
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to today.",
            },
            "sl": {
                "type": "number",
                "description": "Service-level target as a fraction (0.8 = 80%). Defaults to 0.8.",
            },
            "asa": {
                "type": "number",
                "description": "Maximum acceptable ASA in seconds. Defaults to 20.",
            },
        },
        "required": ["queue"],
    },
}

_COLUMNS = [
    "skill",
    "required",
    "primaries_available",
    "secondary_credit_fte",
    "shortfall",
]


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    target_date = _parse_date(args.get("date"))
    sl: float = float(args.get("sl", 0.8))
    asa: float = float(args.get("asa", 20))

    # Find per-skill forecast runs (skill_id IS NOT NULL) for this queue.
    runs = (
        db.execute(
            text(
                """
                SELECT id, skill_id
                FROM forecast_runs
                WHERE queue = :queue
                  AND status = 'completed'
                  AND skill_id IS NOT NULL
                ORDER BY created_at DESC
                """
            ),
            {"queue": queue},
        )
        .mappings()
        .all()
    )

    if not runs:
        return {
            "render": "error",
            "message": (
                f"No per-skill forecasts for queue {queue!r}. "
                "Run a forecast with skill_id set first."
            ),
            "code": "NO_PER_SKILL_FORECAST",
        }

    # Pick the most recent run per skill_id.
    latest_by_skill: dict[int, int] = {}
    for r in runs:
        skill_id = int(r["skill_id"])
        if skill_id not in latest_by_skill:
            latest_by_skill[skill_id] = int(r["id"])

    from app.services.multi_skill_staffing import (
        required_with_substitution,
        secondary_credit_for_skill,
    )

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    rows: list[list[Any]] = []
    for skill_id, run_id in latest_by_skill.items():
        skill_name = (
            db.execute(
                text("SELECT name FROM skills WHERE id = :id"),
                {"id": skill_id},
            ).scalar_one_or_none()
            or f"skill_{skill_id}"
        )

        # Aggregate the day's required headcount: sum of per-interval
        # discounted required across all intervals on this date. We use
        # the max across the day as a more conservative "peak required"
        # (lots of WFM tools surface peak, not sum).
        intervals = (
            db.execute(
                text(
                    """
                    SELECT forecast_offered, forecast_aht_seconds
                    FROM forecast_intervals
                    WHERE forecast_run_id = :rid
                      AND interval_start >= :start
                      AND interval_start < :end
                    """
                ),
                {"rid": run_id, "start": day_start, "end": day_end},
            )
            .mappings()
            .all()
        )

        secondary_fte = secondary_credit_for_skill(db, skill_id)

        peak_required = 0
        for iv in intervals:
            req = required_with_substitution(
                forecast_offered=float(iv["forecast_offered"] or 0),
                aht_seconds=float(iv["forecast_aht_seconds"] or 0),
                secondary_credit_fte=secondary_fte,
                sl_target=sl,
                target_asa_sec=asa,
            )
            if req.discounted_required > peak_required:
                peak_required = req.discounted_required

        # Count primaries: agents whose top proficiency IS this skill.
        primaries = int(
            db.execute(
                text(
                    """
                    WITH max_prof AS (
                        SELECT agent_id, MAX(proficiency) AS top
                        FROM agent_skills GROUP BY agent_id
                    )
                    SELECT COUNT(*)
                    FROM agent_skills a_skill
                    JOIN max_prof mp ON mp.agent_id = a_skill.agent_id
                    JOIN agents a    ON a.id = a_skill.agent_id AND a.active = TRUE
                    WHERE a_skill.skill_id = :sid
                      AND a_skill.proficiency = mp.top
                    """
                ),
                {"sid": skill_id},
            ).scalar_one()
        )

        shortfall = max(0, peak_required - primaries)
        rows.append(
            [
                skill_name,
                peak_required,
                primaries,
                round(secondary_fte, 1),
                shortfall,
            ]
        )

    # Sort by shortfall desc — biggest gap first, where ops attention belongs.
    rows.sort(key=lambda r: r[4], reverse=True)

    return {
        "render": "table",
        "title": f"Skills coverage — {queue}, {target_date.isoformat()}",
        "columns": _COLUMNS,
        "rows": rows,
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
