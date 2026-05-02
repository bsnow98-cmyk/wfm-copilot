"""
recommend_ot tool — Wave 2.

Mirror of recommend_vto for the understaffed case. Finds the worst
contiguous shortfall window of the day, then ranks off-duty active
agents by [proficiency desc on best skill, seniority desc, name].

Default policy: seniority_desc — many shops give OT to senior agents
first (CBA convention). Pass policy=seniority_asc to flip to
junior-first (some non-union shops use this for "fairness/spread").
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "recommend_ot",
    "description": (
        "Recommend overtime (OT) candidates when a day is understaffed. "
        "Identifies the worst shortfall window and returns off-duty agents "
        "ranked by skill and seniority. Use when the user asks 'should I "
        "mandate OT', 'who's available for OT', or 'we're short — call "
        "someone in'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to today.",
            },
            "limit": {
                "type": "integer",
                "description": "Max candidates to return. Defaults to shortfall size.",
            },
            "policy": {
                "type": "string",
                "enum": ["seniority_desc", "seniority_asc"],
                "description": (
                    "seniority_desc (default) offers OT to most-senior first; "
                    "seniority_asc to most-junior."
                ),
            },
        },
    },
}

_COLUMNS = [
    "rank",
    "agent",
    "employee_id",
    "tenure_yrs",
    "top_skill",
    "proficiency",
]


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    target_date = _parse_date(args.get("date"))
    explicit_limit = args.get("limit")
    policy: str = args.get("policy") or "seniority_desc"

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

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    cov = (
        db.execute(
            text(
                """
                SELECT interval_start, required_agents, scheduled_agents,
                       shortage
                FROM schedule_coverage
                WHERE schedule_id = :sid
                  AND interval_start >= :start AND interval_start < :end
                ORDER BY interval_start
                """
            ),
            {"sid": schedule_id, "start": day_start, "end": day_end},
        )
        .mappings()
        .all()
    )
    if not cov:
        return {
            "render": "error",
            "message": "No coverage rows for this date.",
            "code": "NO_COVERAGE",
        }

    window = _worst_short_window(cov)
    if window is None:
        return {
            "render": "table",
            "title": (
                f"OT candidates — {target_date.isoformat()}: "
                "no shortfall intervals"
            ),
            "columns": _COLUMNS,
            "rows": [],
        }
    win_start, win_end, avg_short = window
    n_to_offer = (
        int(explicit_limit) if explicit_limit else max(1, round(avg_short))
    )

    order = "ASC" if policy == "seniority_desc" else "DESC"
    # Off-duty = no work segment overlapping the window. Top skill = max
    # proficiency across this agent's skills. Active agents only.
    rows = (
        db.execute(
            text(
                f"""
                WITH agent_top_skill AS (
                    SELECT DISTINCT ON (a_skill.agent_id)
                        a_skill.agent_id,
                        sk.name AS top_skill,
                        a_skill.proficiency AS top_prof
                    FROM agent_skills a_skill
                    JOIN skills sk ON sk.id = a_skill.skill_id
                    ORDER BY a_skill.agent_id, a_skill.proficiency DESC
                )
                SELECT a.id, a.full_name, a.employee_id, a.hire_date,
                       ats.top_skill, ats.top_prof
                FROM agents a
                LEFT JOIN agent_top_skill ats ON ats.agent_id = a.id
                WHERE a.active = TRUE
                  AND NOT EXISTS (
                      SELECT 1 FROM shift_segments seg
                      WHERE seg.agent_id = a.id
                        AND seg.schedule_id = :sid
                        AND seg.segment_type = 'work'
                        AND seg.start_time < :win_end
                        AND seg.end_time   > :win_start
                  )
                ORDER BY ats.top_prof DESC NULLS LAST,
                         a.hire_date {order} NULLS LAST,
                         a.full_name
                LIMIT :limit
                """
            ),
            {
                "sid": schedule_id,
                "win_start": win_start,
                "win_end": win_end,
                "limit": n_to_offer,
            },
        )
        .mappings()
        .all()
    )

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
                r["top_skill"] or "-",
                r["top_prof"] if r["top_prof"] is not None else "-",
            ]
        )

    win_label = f"{win_start.strftime('%H:%M')}–{win_end.strftime('%H:%M')}"
    return {
        "render": "table",
        "title": (
            f"OT candidates — {target_date.isoformat()}, window {win_label} "
            f"(avg short {avg_short:.1f}, policy: {policy})"
        ),
        "columns": _COLUMNS,
        "rows": table_rows,
    }


def _worst_short_window(
    cov: list[dict[str, Any]],
) -> tuple[datetime, datetime, float] | None:
    best: tuple[datetime, datetime, float] | None = None
    run_start: datetime | None = None
    run_shorts: list[float] = []
    last_ts: datetime | None = None
    for r in cov:
        ts: datetime = r["interval_start"]
        short = float(r["shortage"] or 0)
        if short > 0:
            if run_start is None:
                run_start = ts
                run_shorts = [short]
            else:
                run_shorts.append(short)
            last_ts = ts
        else:
            if run_start is not None and last_ts is not None and run_shorts:
                end = last_ts + timedelta(minutes=30)
                avg = sum(run_shorts) / len(run_shorts)
                if best is None or (end - run_start) > (best[1] - best[0]):
                    best = (run_start, end, avg)
            run_start = None
            run_shorts = []
    if run_start is not None and last_ts is not None and run_shorts:
        end = last_ts + timedelta(minutes=30)
        avg = sum(run_shorts) / len(run_shorts)
        if best is None or (end - run_start) > (best[1] - best[0]):
            best = (run_start, end, avg)
    return best


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
