"""
preview_offer — read-only preview of publishing an OT or VTO offer.

Surface #2 of EXECUTION_ROADMAP.md. Reuses recommend_ot / recommend_vto's
worst-window math to scope the offer, ranks the same candidate group, and mints
an apply_token pinning the full offer spec. Publishing happens in
app/routers/offers.py; the LLM previews but never publishes.

v1 offers to the *recommended* target group (the shortfall/overage candidates).
Custom recipient lists are a v2 concern.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "preview_offer",
    "description": (
        "Preview (read-only) publishing an overtime (OT) or voluntary-time-off "
        "(VTO) offer to the recommended candidate group for a day. Shows who "
        "would receive it and surfaces a Publish button the human clicks to "
        "send. Use after recommend_ot / recommend_vto when the user says e.g. "
        "'publish that OT offer', 'send a VTO offer for today'. Does NOT publish."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["ot", "vto"]},
            "date": {"type": "string", "description": "ISO date YYYY-MM-DD. Defaults to today."},
            "slots": {"type": "integer", "description": "Override how many to offer."},
            "policy": {
                "type": "string",
                "enum": ["seniority_desc", "seniority_asc"],
            },
            "message": {"type": "string", "description": "Optional note shown with the offer."},
        },
        "required": ["kind"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    from app.services.apply_tokens import issue_offer_token
    from app.services.realtime_clock import sim_today
    from app.tools.recommend_ot import _worst_short_window
    from app.tools.recommend_vto import _worst_over_window

    kind = args["kind"]
    if kind not in ("ot", "vto"):
        return {"render": "error", "message": "kind must be 'ot' or 'vto'.", "code": "BAD_ARGS"}
    target_date = date.fromisoformat(args["date"]) if args.get("date") else sim_today(db)
    policy = args.get("policy") or "seniority_desc"
    _policy_dirs = {"seniority_desc": "ASC", "seniority_asc": "DESC"}
    if policy not in _policy_dirs:
        policy = "seniority_desc"
    order = _policy_dirs[policy]

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
                SELECT interval_start, required_agents, scheduled_agents, shortage
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
        return {"render": "error", "message": "No coverage rows for this date.", "code": "NO_COVERAGE"}

    window = _worst_short_window(cov) if kind == "ot" else _worst_over_window(cov)
    if window is None:
        what = "shortfall" if kind == "ot" else "overstaffed"
        return {
            "render": "error",
            "message": f"No {what} window on {target_date.isoformat()} — nothing to offer.",
            "code": "NO_WINDOW",
        }
    win_start, win_end, avg = window
    slots = int(args["slots"]) if args.get("slots") else max(1, round(avg))

    if kind == "ot":
        # Off-duty active agents ranked by top proficiency then seniority.
        rows = (
            db.execute(
                text(
                    f"""
                    WITH agent_top_skill AS (
                        SELECT DISTINCT ON (a_skill.agent_id)
                            a_skill.agent_id, sk.name AS top_skill, a_skill.proficiency AS top_prof
                        FROM agent_skills a_skill
                        JOIN skills sk ON sk.id = a_skill.skill_id
                        ORDER BY a_skill.agent_id, a_skill.proficiency DESC
                    )
                    SELECT a.full_name, a.employee_id, ats.top_skill, ats.top_prof
                    FROM agents a
                    LEFT JOIN agent_top_skill ats ON ats.agent_id = a.id
                    WHERE a.active = TRUE
                      AND NOT EXISTS (
                          SELECT 1 FROM shift_segments seg
                          WHERE seg.agent_id = a.id AND seg.schedule_id = :sid
                            AND seg.segment_type = 'work'
                            AND seg.start_time < :win_end AND seg.end_time > :win_start
                      )
                    ORDER BY ats.top_prof DESC NULLS LAST, a.hire_date {order} NULLS LAST, a.full_name
                    LIMIT :limit
                    """
                ),
                {"sid": schedule_id, "win_start": win_start, "win_end": win_end, "limit": slots},
            )
            .mappings()
            .all()
        )
        columns = ["rank", "agent", "employee_id", "top_skill", "proficiency"]
        table_rows = [
            [i, r["full_name"], r["employee_id"], r["top_skill"] or "-",
             r["top_prof"] if r["top_prof"] is not None else "-"]
            for i, r in enumerate(rows, start=1)
        ]
    else:
        # Scheduled agents in the overstaffed window, most-senior first.
        rows = (
            db.execute(
                text(
                    f"""
                    SELECT DISTINCT a.full_name, a.employee_id, a.hire_date,
                           seg.start_time, seg.end_time
                    FROM shift_segments seg
                    JOIN agents a ON a.id = seg.agent_id AND a.active = TRUE
                    WHERE seg.schedule_id = :sid AND seg.segment_type = 'work'
                      AND seg.start_time < :win_end AND seg.end_time > :win_start
                    ORDER BY a.hire_date {order} NULLS LAST, a.full_name
                    LIMIT :limit
                    """
                ),
                {"sid": schedule_id, "win_start": win_start, "win_end": win_end, "limit": slots},
            )
            .mappings()
            .all()
        )
        columns = ["rank", "agent", "employee_id", "shift"]
        table_rows = [
            [i, r["full_name"], r["employee_id"],
             f"{r['start_time'].strftime('%H:%M')}–{r['end_time'].strftime('%H:%M')}"]
            for i, r in enumerate(rows, start=1)
        ]

    if not table_rows:
        return {
            "render": "error",
            "message": f"No eligible agents for an {kind.upper()} offer in that window.",
            "code": "NO_CANDIDATES",
        }

    targets = [{"employee_id": r[2], "full_name": r[1]} for r in table_rows]
    spec = {
        "kind": kind,
        "schedule_id": schedule_id,
        "target_date": target_date.isoformat(),
        "window_start": win_start.isoformat(),
        "window_end": win_end.isoformat(),
        "targets": targets,
        "slots": slots,
        "policy": policy,
        "message": args.get("message"),
    }
    token = issue_offer_token(db, spec=spec)
    db.commit()

    win_label = f"{win_start.strftime('%H:%M')}–{win_end.strftime('%H:%M')}"
    metric = "avg short" if kind == "ot" else "avg overage"
    return {
        "render": "table",
        "title": (
            f"Publish {kind.upper()} offer — {target_date.isoformat()}, window {win_label} "
            f"({metric} {avg:.1f}, {len(targets)} candidate(s), policy: {policy})"
        ),
        "columns": columns,
        "rows": table_rows,
        "apply_token": token.token,
        "offer": {
            "kind": kind,
            "slots": slots,
            "n_targets": len(targets),
            "window_label": win_label,
            "target_date": target_date.isoformat(),
            "message": args.get("message"),
        },
    }
