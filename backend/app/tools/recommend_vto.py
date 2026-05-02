"""
recommend_vto tool — Wave 2.

Answers "should I offer VTO? Who?" — when a day is overstaffed, surface
candidates in seniority-desc order (senior-first; standard CBA-style
convention — senior agents value time off, junior want hours).

The tool finds the worst contiguous overstaffed window of the day from
schedule_coverage, takes the avg overage in that window as N, and
returns the top-N most-senior agents scheduled to work during it. The
policy is named in the title so it's auditable; swap by passing the
`policy` arg.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "recommend_vto",
    "description": (
        "Recommend voluntary time off (VTO) candidates when a day is "
        "overstaffed. Identifies the worst overstaffed window and returns the "
        "most-senior scheduled agents whose shift overlaps it. Use when the "
        "user asks 'should I offer VTO', 'who can take VTO today', or 'we're "
        "overstaffed — who do I send home'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to today.",
            },
            "policy": {
                "type": "string",
                "enum": ["seniority_desc", "seniority_asc"],
                "description": (
                    "Ranking policy. seniority_desc (default) offers VTO to "
                    "the most-senior eligible agent first; seniority_asc "
                    "offers to the most-junior."
                ),
            },
        },
    },
}

_COLUMNS = ["rank", "agent", "employee_id", "tenure_yrs", "shift", "skill"]


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    target_date = _parse_date(args.get("date"))
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
                SELECT interval_start, required_agents, scheduled_agents
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

    window = _worst_over_window(cov)
    if window is None:
        return {
            "render": "table",
            "title": (
                f"VTO candidates — {target_date.isoformat()}: "
                "no overstaffed intervals"
            ),
            "columns": _COLUMNS,
            "rows": [],
        }
    win_start, win_end, avg_over = window
    n_to_offer = max(1, round(avg_over))

    order = "ASC" if policy == "seniority_desc" else "DESC"
    rows = (
        db.execute(
            text(
                f"""
                SELECT DISTINCT
                    a.id, a.full_name, a.employee_id, a.hire_date,
                    seg.start_time, seg.end_time, sk.name AS skill_name
                FROM shift_segments seg
                JOIN agents a ON a.id = seg.agent_id AND a.active = TRUE
                LEFT JOIN skills sk ON sk.id = seg.skill_id
                WHERE seg.schedule_id = :sid
                  AND seg.segment_type = 'work'
                  AND seg.start_time < :win_end
                  AND seg.end_time   > :win_start
                ORDER BY a.hire_date {order} NULLS LAST, a.full_name
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
        shift = (
            f"{r['start_time'].strftime('%H:%M')}–"
            f"{r['end_time'].strftime('%H:%M')}"
        )
        table_rows.append(
            [
                i,
                r["full_name"],
                r["employee_id"],
                tenure,
                shift,
                r["skill_name"] or "-",
            ]
        )

    win_label = (
        f"{win_start.strftime('%H:%M')}–{win_end.strftime('%H:%M')}"
    )
    return {
        "render": "table",
        "title": (
            f"VTO candidates — {target_date.isoformat()}, window {win_label} "
            f"(avg overage {avg_over:.1f}, policy: {policy})"
        ),
        "columns": _COLUMNS,
        "rows": table_rows,
    }


def _worst_over_window(
    cov: list[dict[str, Any]],
) -> tuple[datetime, datetime, float] | None:
    """Find the longest contiguous run of overstaffed intervals; return its
    bounds and the avg overage. None if no overstaffed intervals exist."""
    best: tuple[datetime, datetime, float] | None = None
    run_start: datetime | None = None
    run_overs: list[float] = []
    last_ts: datetime | None = None
    for r in cov:
        ts: datetime = r["interval_start"]
        over = float((r["scheduled_agents"] or 0) - (r["required_agents"] or 0))
        if over > 0:
            if run_start is None:
                run_start = ts
                run_overs = [over]
            else:
                run_overs.append(over)
            last_ts = ts
        else:
            if run_start is not None and last_ts is not None and run_overs:
                end = last_ts + timedelta(minutes=30)
                avg = sum(run_overs) / len(run_overs)
                if best is None or (end - run_start) > (best[1] - best[0]):
                    best = (run_start, end, avg)
            run_start = None
            run_overs = []
    if run_start is not None and last_ts is not None and run_overs:
        end = last_ts + timedelta(minutes=30)
        avg = sum(run_overs) / len(run_overs)
        if best is None or (end - run_start) > (best[1] - best[0]):
            best = (run_start, end, avg)
    return best


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
