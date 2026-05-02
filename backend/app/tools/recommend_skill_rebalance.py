"""
recommend_skill_rebalance tool — Wave 2.

Answers "move agents from skill X to skill Y — who?" — when one skill is
short while another is long, surface cross-skilled agents who can be
re-pointed without dropping below proficiency floor.

Default policy: minimum_proficiency=3 on the target skill, primary on a
long skill. The tool computes per-skill required (peak interval today),
flags long/short, and pairs them. Read-only — does NOT mutate
shift_segments.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "recommend_skill_rebalance",
    "description": (
        "Suggest agents to move between skills when coverage is unbalanced "
        "— some skills short, others long. Returns rows of (agent, from_skill, "
        "to_skill, proficiency_in_target). Use when the user asks 'who can I "
        "move from X to Y', 'rebalance skills', or 'we're short on skill Y'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {
                "type": "string",
                "description": (
                    "Queue name. Used to find the queue's per-skill forecast "
                    "and current schedule."
                ),
            },
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to today.",
            },
            "min_proficiency": {
                "type": "integer",
                "description": (
                    "Minimum proficiency on the target skill (1-5). "
                    "Defaults to 3 (competent)."
                ),
            },
        },
        "required": ["queue"],
    },
}

_COLUMNS = [
    "agent",
    "employee_id",
    "from_skill",
    "to_skill",
    "prof_to",
    "prof_from",
]


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    target_date = _parse_date(args.get("date"))
    min_prof: int = int(args.get("min_proficiency", 3))

    # Per-skill forecast runs for this queue, latest per skill.
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
                f"No per-skill forecasts for queue {queue!r}. Need a forecast "
                "run with skill_id set first."
            ),
            "code": "NO_PER_SKILL_FORECAST",
        }
    latest_by_skill: dict[int, int] = {}
    for r in runs:
        sid = int(r["skill_id"])
        if sid not in latest_by_skill:
            latest_by_skill[sid] = int(r["id"])

    # Compute per-skill required (peak across day) using the same
    # multi-skill helper as get_skills_coverage. Avoids double-counting
    # secondary credit.
    from app.services.multi_skill_staffing import (
        required_with_substitution,
        secondary_credit_for_skill,
    )

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    skill_balance: list[dict[str, Any]] = []
    for skill_id, run_id in latest_by_skill.items():
        skill_name = (
            db.execute(
                text("SELECT name FROM skills WHERE id = :id"),
                {"id": skill_id},
            ).scalar_one_or_none()
            or f"skill_{skill_id}"
        )
        intervals = (
            db.execute(
                text(
                    """
                    SELECT forecast_offered, forecast_aht_seconds
                    FROM forecast_intervals
                    WHERE forecast_run_id = :rid
                      AND interval_start >= :start AND interval_start < :end
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
                sl_target=0.8,
                target_asa_sec=20,
            )
            if req.discounted_required > peak_required:
                peak_required = req.discounted_required

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
        skill_balance.append(
            {
                "skill_id": skill_id,
                "skill_name": skill_name,
                "required": peak_required,
                "primaries": primaries,
                "shortfall": max(0, peak_required - primaries),
                "surplus": max(0, primaries - peak_required),
            }
        )

    short = [s for s in skill_balance if s["shortfall"] > 0]
    long_ = [s for s in skill_balance if s["surplus"] > 0]

    if not short:
        return {
            "render": "table",
            "title": (
                f"Skill rebalance — {queue}, {target_date.isoformat()}: "
                "no shortfalls"
            ),
            "columns": _COLUMNS,
            "rows": [],
        }
    if not long_:
        return {
            "render": "table",
            "title": (
                f"Skill rebalance — {queue}, {target_date.isoformat()}: "
                f"{len(short)} skill(s) short, no surplus skills to draw from"
            ),
            "columns": _COLUMNS,
            "rows": [],
        }

    # For each short skill, find agents whose primary is a long skill AND
    # who have proficiency >= min_prof on the short skill.
    rows: list[list[Any]] = []
    for s in short:
        short_id = s["skill_id"]
        short_name = s["skill_name"]
        long_ids = [int(l_["skill_id"]) for l_ in long_]

        candidates = (
            db.execute(
                text(
                    """
                    WITH max_prof AS (
                        SELECT agent_id, MAX(proficiency) AS top
                        FROM agent_skills GROUP BY agent_id
                    ),
                    primaries AS (
                        SELECT a_skill.agent_id, a_skill.skill_id, a_skill.proficiency
                        FROM agent_skills a_skill
                        JOIN max_prof mp
                          ON mp.agent_id = a_skill.agent_id
                         AND mp.top = a_skill.proficiency
                    )
                    SELECT a.id, a.full_name, a.employee_id,
                           src.name AS from_skill,
                           p.proficiency AS prof_from,
                           target.proficiency AS prof_to
                    FROM agents a
                    JOIN primaries p           ON p.agent_id = a.id
                    JOIN skills src            ON src.id = p.skill_id
                    JOIN agent_skills target   ON target.agent_id = a.id
                                              AND target.skill_id = :short_id
                    WHERE a.active = TRUE
                      AND p.skill_id = ANY(:long_ids)
                      AND target.proficiency >= :min_prof
                    ORDER BY target.proficiency DESC, p.proficiency ASC
                    LIMIT :need
                    """
                ),
                {
                    "short_id": short_id,
                    "long_ids": long_ids,
                    "min_prof": min_prof,
                    "need": s["shortfall"],
                },
            )
            .mappings()
            .all()
        )

        for c in candidates:
            rows.append(
                [
                    c["full_name"],
                    c["employee_id"],
                    c["from_skill"],
                    short_name,
                    c["prof_to"],
                    c["prof_from"],
                ]
            )

    return {
        "render": "table",
        "title": (
            f"Skill rebalance — {queue}, {target_date.isoformat()} "
            f"(min proficiency {min_prof})"
        ),
        "columns": _COLUMNS,
        "rows": rows,
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
