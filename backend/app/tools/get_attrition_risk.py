"""
get_attrition_risk — flag agents at high risk of leaving.

Heuristic features (each scored 0..1, summed → risk score 0..5):
  short_tenure        hire_date < 6 months ago
  low_adherence       window adherence < 85%
  low_qa              window QA avg < 75
  high_exceptions     > 60 exception-minutes in window
  schedule_swings     > 5 unique distinct shift-start hours in window
                      (proxy for irregular schedule)

Surfaces score AND the contributing features so the "AI shows its math".
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.tools.get_adherence import ADHERENT_MATCH

definition: dict[str, Any] = {
    "name": "get_attrition_risk",
    "description": (
        "Rank agents by attrition risk based on heuristic features (short "
        "tenure, low adherence, low QA, high exception minutes, irregular "
        "schedule). Returns score 0–5 with the contributing factors named. "
        "Use when the user asks 'who is at risk of leaving', 'attrition "
        "risk', 'who should I retain'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "window_days": {"type": "integer", "minimum": 7, "maximum": 90},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    limit = int(args.get("limit") or 10)
    window_days = int(args.get("window_days") or 30)
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    start_ts = now - timedelta(days=window_days)

    rows = (
        db.execute(
            text(
                f"""
                WITH adh AS (
                    SELECT seg.agent_id,
                           SUM(EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                       - GREATEST(seg.start_time, ax.start_ts))))
                                AS sched,
                           SUM(CASE WHEN {ADHERENT_MATCH} THEN
                               EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                       - GREATEST(seg.start_time, ax.start_ts)))
                           ELSE 0 END) AS adh
                    FROM shift_segments seg
                    JOIN agent_aux_events ax ON ax.agent_id = seg.agent_id
                     AND ax.start_ts < seg.end_time
                     AND COALESCE(ax.end_ts, sim_now()) > seg.start_time
                    WHERE seg.start_time >= :start AND seg.start_time < :end
                      AND seg.segment_type <> 'off'
                    GROUP BY seg.agent_id
                ),
                qa AS (
                    SELECT agent_id, AVG(score) AS avg_score
                    FROM agent_qa_scores WHERE evaluated_at >= :start
                    GROUP BY agent_id
                ),
                exc AS (
                    SELECT agent_id, SUM(duration_seconds) AS total_sec
                    FROM adherence_exceptions WHERE start_ts >= :start
                    GROUP BY agent_id
                ),
                swings AS (
                    SELECT agent_id,
                           COUNT(DISTINCT EXTRACT(HOUR FROM start_time)) AS hour_variants
                    FROM shift_segments
                    WHERE start_time >= :start AND start_time < :end
                      AND segment_type = 'work'
                    GROUP BY agent_id
                )
                SELECT a.full_name, a.employee_id, a.hire_date,
                       COALESCE(adh.adh / NULLIF(adh.sched, 0), 1) AS adh_ratio,
                       COALESCE(qa.avg_score, 80) AS qa_avg,
                       COALESCE(exc.total_sec, 0) AS exc_sec,
                       COALESCE(swings.hour_variants, 1) AS hour_variants
                FROM agents a
                LEFT JOIN adh    ON adh.agent_id    = a.id
                LEFT JOIN qa     ON qa.agent_id     = a.id
                LEFT JOIN exc    ON exc.agent_id    = a.id
                LEFT JOIN swings ON swings.agent_id = a.id
                WHERE a.active = TRUE
                """
            ),
            {"start": start_ts, "end": now},
        )
        .mappings()
        .all()
    )

    scored: list[dict[str, Any]] = []
    six_mo = now - timedelta(days=180)
    for r in rows:
        features: list[str] = []
        score = 0.0
        if r["hire_date"] and r["hire_date"] > six_mo.date():
            score += 1
            features.append("short_tenure")
        adh_ratio = float(r["adh_ratio"] or 1)
        if adh_ratio < 0.85:
            score += 1
            features.append(f"low_adherence({adh_ratio*100:.0f}%)")
        qa_avg = float(r["qa_avg"] or 80)
        if qa_avg < 75:
            score += 1
            features.append(f"low_qa({qa_avg:.0f})")
        exc_min = int(r["exc_sec"] or 0) // 60
        if exc_min > 60:
            score += 1
            features.append(f"high_exceptions({exc_min}m)")
        hour_variants = int(r["hour_variants"] or 1)
        if hour_variants > 5:
            score += 1
            features.append(f"schedule_swings({hour_variants}h)")
        if score > 0:
            scored.append(
                {
                    "name": r["full_name"],
                    "eid": r["employee_id"],
                    "score": score,
                    "features": ", ".join(features),
                }
            )

    scored.sort(key=lambda x: -x["score"])
    scored = scored[:limit]
    table_rows = [
        [i, s["name"], s["eid"], f"{s['score']:.0f}/5", s["features"]]
        for i, s in enumerate(scored, start=1)
    ]
    return {
        "render": "table",
        "title": f"Attrition risk — top {len(scored)} (last {window_days}d, heuristic 0-5)",
        "columns": ["Rank", "Agent", "ID", "Score", "Risk factors"],
        "rows": table_rows,
    }
