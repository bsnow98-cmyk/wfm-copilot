"""
explain_substitution tool — Phase 8 Stage 3.

Returns a `text` render explaining the multi-skill substitution math used
for a (queue, skill) pair: the discount factor, the proficiency-floor, the
secondary-credit FTE, and how those combine to produce the per-skill
required headcount.

Closes the "AI shows its math" loop for multi-skill staffing — when a user
sees a number they don't trust, this tool produces the receipts.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


definition: dict[str, Any] = {
    "name": "explain_substitution",
    "description": (
        "Explain how multi-skill staffing math arrived at the required "
        "headcount for a particular (queue, skill). Cites the substitution "
        "discount, the proficiency floor, and the secondary-credit FTE. Use "
        "when the user asks 'why is this staffing right?' or 'where did "
        "that number come from?'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {"type": "string"},
            "skill": {
                "type": "string",
                "description": "Skill name (e.g. 'sales', 'support', 'billing').",
            },
            "sl": {
                "type": "number",
                "description": "Service-level target as a fraction (default 0.8).",
            },
            "asa": {
                "type": "number",
                "description": "ASA seconds (default 20).",
            },
        },
        "required": ["queue", "skill"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    skill_name: str = args["skill"]
    sl: float = float(args.get("sl", 0.8))
    asa: float = float(args.get("asa", 20))

    skill_id = db.execute(
        text("SELECT id FROM skills WHERE name = :n"),
        {"n": skill_name},
    ).scalar_one_or_none()
    if skill_id is None:
        return {
            "render": "error",
            "message": f"Unknown skill: {skill_name!r}",
            "code": "UNKNOWN_SKILL",
        }

    run_id = db.execute(
        text(
            """
            SELECT id FROM forecast_runs
            WHERE queue = :q AND status = 'completed' AND skill_id = :sid
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"q": queue, "sid": skill_id},
    ).scalar_one_or_none()
    if run_id is None:
        return {
            "render": "error",
            "message": (
                f"No per-skill forecast for {queue}/{skill_name}. "
                "Run a forecast with skill_id set first."
            ),
            "code": "NO_PER_SKILL_FORECAST",
        }

    # Pick the peak interval — the most demanding moment of the latest run.
    peak = (
        db.execute(
            text(
                """
                SELECT interval_start, forecast_offered, forecast_aht_seconds
                FROM forecast_intervals
                WHERE forecast_run_id = :rid
                ORDER BY forecast_offered DESC
                LIMIT 1
                """
            ),
            {"rid": run_id},
        )
        .mappings()
        .first()
    )
    if peak is None:
        return {
            "render": "error",
            "message": "Forecast run has no intervals.",
            "code": "EMPTY_FORECAST_RUN",
        }

    from app.services.multi_skill_staffing import (
        PRIMARY_FLOOR_RATIO,
        SUBSTITUTION_DISCOUNT,
        required_with_substitution,
        secondary_credit_for_skill,
    )

    secondary_fte = secondary_credit_for_skill(db, int(skill_id))
    req = required_with_substitution(
        forecast_offered=float(peak["forecast_offered"] or 0),
        aht_seconds=float(peak["forecast_aht_seconds"] or 0),
        secondary_credit_fte=secondary_fte,
        sl_target=sl,
        target_asa_sec=asa,
    )

    # Count primaries for this skill — they're the ones that DON'T get the
    # discount and are most explanation-worthy.
    primaries = int(
        db.execute(
            text(
                """
                WITH max_prof AS (
                    SELECT agent_id, MAX(proficiency) AS top
                    FROM agent_skills GROUP BY agent_id
                )
                SELECT COUNT(*) FROM agent_skills a_skill
                JOIN max_prof mp ON mp.agent_id = a_skill.agent_id
                JOIN agents a    ON a.id = a_skill.agent_id AND a.active = TRUE
                WHERE a_skill.skill_id = :sid
                  AND a_skill.proficiency = mp.top
                """
            ),
            {"sid": skill_id},
        ).scalar_one()
    )

    explanation = "\n".join(
        [
            f"Peak forecast interval at {peak['interval_start'].strftime('%Y-%m-%d %H:%M')}.",
            f"Offered: {float(peak['forecast_offered']):.0f} contacts; AHT {float(peak['forecast_aht_seconds']):.0f}s.",
            "",
            "Step 1 — single-skill Erlang C against this forecast:",
            f"  N_naive = {req.naive_required} agents (SL {int(sl * 100)}% / ASA {int(asa)}s).",
            "",
            "Step 2 — secondary-skill credit (cross-skilled help):",
            f"  Discount factor: {SUBSTITUTION_DISCOUNT} per FTE × proficiency / 5.",
            f"  Secondary-credit FTE for {skill_name}: {secondary_fte:.2f}.",
            "",
            "Step 3 — apply discount + primary floor:",
            f"  After credit: max(N_naive − ⌈credit⌉, 0) = {max(0, req.naive_required - int(secondary_fte + 0.999))}.",
            f"  Primary floor ({PRIMARY_FLOOR_RATIO * 100:.0f}% of N_naive): {req.primary_floor}.",
            f"  Required after substitution: {req.discounted_required} agents.",
            "",
            f"Active primaries on {skill_name}: {primaries}.",
            f"Shortfall vs primaries: {max(0, req.discounted_required - primaries)}.",
            "",
            "Note: the discount approximation matches design-doc decisions. "
            "Production WFM tools use Monte Carlo simulation — this is an "
            "honest first-order estimate, not the final answer.",
        ]
    )

    return {"render": "text", "content": explanation}
