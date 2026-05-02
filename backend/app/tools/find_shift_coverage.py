"""
find_shift_coverage tool — Wave 2.

Answers "find me coverage for this open shift" — agent calls out, manager
needs to fill the gap. Takes a date + window (start/end) + optional
required skill, returns ranked candidates who are off during the window.

Default policy: skill_match_then_junior — match the required skill first
(higher proficiency wins), then offer to the most-junior eligible agent
(let junior reps pick up the hours). Pass policy=skill_match_then_senior
to invert the seniority leg.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "find_shift_coverage",
    "description": (
        "Find agents who can cover an open shift window on a given date. "
        "Returns candidates who are off during the window, ranked by skill "
        "match then seniority. Use when the user asks 'find coverage', 'who "
        "can cover this shift', or 'agent X called out — who fills in'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD of the open shift.",
            },
            "start_time": {
                "type": "string",
                "description": "Window start, HH:MM (24h). Local to schedule timezone.",
            },
            "end_time": {
                "type": "string",
                "description": "Window end, HH:MM (24h).",
            },
            "skill": {
                "type": "string",
                "description": "Required skill name. Optional — omit to allow any.",
            },
            "limit": {
                "type": "integer",
                "description": "Max candidates to return (default 10).",
            },
            "policy": {
                "type": "string",
                "enum": ["skill_match_then_junior", "skill_match_then_senior"],
                "description": (
                    "Default skill_match_then_junior offers to the most-junior "
                    "eligible; flip to skill_match_then_senior for senior-first."
                ),
            },
        },
        "required": ["date", "start_time", "end_time"],
    },
}

_COLUMNS = [
    "rank",
    "agent",
    "employee_id",
    "tenure_yrs",
    "skill_match",
    "proficiency",
]


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    target_date = _parse_date(args["date"])
    start_h, start_m = _parse_hhmm(args["start_time"])
    end_h, end_m = _parse_hhmm(args["end_time"])
    skill_name: str | None = args.get("skill")
    limit: int = int(args.get("limit", 10))
    policy: str = args.get("policy") or "skill_match_then_junior"

    win_start = datetime(
        target_date.year, target_date.month, target_date.day,
        start_h, start_m, tzinfo=timezone.utc,
    )
    win_end = datetime(
        target_date.year, target_date.month, target_date.day,
        end_h, end_m, tzinfo=timezone.utc,
    )
    if win_end <= win_start:
        return {
            "render": "error",
            "message": "end_time must be after start_time.",
            "code": "BAD_WINDOW",
        }

    schedule_id = db.execute(
        text(
            """
            SELECT id FROM schedules
            WHERE start_date <= :d AND end_date >= :d
            ORDER BY (status = 'published') DESC, created_at DESC
            LIMIT 1
            """
        ),
        {"d": target_date},
    ).scalar_one_or_none()
    if schedule_id is None:
        return {
            "render": "error",
            "message": f"No schedule covers {target_date.isoformat()}.",
            "code": "NO_SCHEDULE",
        }

    skill_id = None
    if skill_name:
        skill_id = db.execute(
            text("SELECT id FROM skills WHERE name = :n"),
            {"n": skill_name},
        ).scalar_one_or_none()
        if skill_id is None:
            return {
                "render": "error",
                "message": f"Unknown skill {skill_name!r}.",
                "code": "UNKNOWN_SKILL",
            }

    # Pull candidates: active agents with no overlapping work shift.
    # Join agent_skills filtered to the requested skill (if any) — agents
    # without that skill drop out for skill-required searches.
    seniority_dir = "DESC" if policy == "skill_match_then_junior" else "ASC"
    if skill_id is not None:
        sql = f"""
            SELECT a.id, a.full_name, a.employee_id, a.hire_date,
                   :skill_name AS skill_match, a_skill.proficiency
            FROM agents a
            JOIN agent_skills a_skill
              ON a_skill.agent_id = a.id AND a_skill.skill_id = :skill_id
            WHERE a.active = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM shift_segments seg
                  WHERE seg.agent_id = a.id
                    AND seg.schedule_id = :sid
                    AND seg.segment_type = 'work'
                    AND seg.start_time < :win_end
                    AND seg.end_time   > :win_start
              )
            ORDER BY a_skill.proficiency DESC,
                     a.hire_date {seniority_dir} NULLS LAST,
                     a.full_name
            LIMIT :limit
        """
        params: dict[str, Any] = {
            "skill_id": skill_id,
            "skill_name": skill_name,
            "sid": schedule_id,
            "win_start": win_start,
            "win_end": win_end,
            "limit": limit,
        }
    else:
        # No skill required — rank by top proficiency overall, then seniority.
        sql = f"""
            WITH agent_top AS (
                SELECT DISTINCT ON (agent_id)
                    agent_id, sk.name AS top_skill, a_skill.proficiency AS top_prof
                FROM agent_skills a_skill
                JOIN skills sk ON sk.id = a_skill.skill_id
                ORDER BY agent_id, a_skill.proficiency DESC
            )
            SELECT a.id, a.full_name, a.employee_id, a.hire_date,
                   COALESCE(at.top_skill, '-') AS skill_match,
                   at.top_prof AS proficiency
            FROM agents a
            LEFT JOIN agent_top at ON at.agent_id = a.id
            WHERE a.active = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM shift_segments seg
                  WHERE seg.agent_id = a.id
                    AND seg.schedule_id = :sid
                    AND seg.segment_type = 'work'
                    AND seg.start_time < :win_end
                    AND seg.end_time   > :win_start
              )
            ORDER BY at.top_prof DESC NULLS LAST,
                     a.hire_date {seniority_dir} NULLS LAST,
                     a.full_name
            LIMIT :limit
        """
        params = {
            "sid": schedule_id,
            "win_start": win_start,
            "win_end": win_end,
            "limit": limit,
        }

    rows = db.execute(text(sql), params).mappings().all()

    table_rows: list[list[Any]] = []
    today = datetime.now(timezone.utc).date()
    for i, r in enumerate(rows, start=1):
        tenure = (
            round((today - r["hire_date"]).days / 365.25, 1)
            if r["hire_date"]
            else "-"
        )
        table_rows.append(
            [
                i,
                r["full_name"],
                r["employee_id"],
                tenure,
                r["skill_match"],
                r["proficiency"] if r["proficiency"] is not None else "-",
            ]
        )

    title_skill = f", skill {skill_name}" if skill_name else ""
    win_label = (
        f"{win_start.strftime('%H:%M')}–{win_end.strftime('%H:%M')}"
    )
    return {
        "render": "table",
        "title": (
            f"Coverage candidates — {target_date.isoformat()} "
            f"{win_label}{title_skill} (policy: {policy})"
        ),
        "columns": _COLUMNS,
        "rows": table_rows,
    }


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)
