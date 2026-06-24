"""
preview_award_bids — read-only preview of awarding a vacation bid round.

The marquee tool. Runs the seniority-greedy waterfall read-only, renders the
proposed awards (and the summary of denials/zero-wins/capacity), and mints an
apply_token. Awarding (batch-writing approved leave) happens in
app/routers/vacation.py, gated to wfm_manager+. The LLM previews; only a manager
awards.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "preview_award_bids",
    "description": (
        "Preview (read-only) awarding a CLOSED vacation bid round by seniority. "
        "Shows who gets which week (and which preference it satisfied), plus how "
        "many agents got nothing and which weeks maxed out, and surfaces an Award "
        "button. Use when the user says 'award the vacation bids', 'run the bid', "
        "'who gets what for the vacation round'. Does NOT write anything."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"round_id": {"type": "integer"}},
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    from app.services.apply_tokens import issue_vacation_token
    from app.services.vacation_bidding import (
        compute_award,
        compute_inputs_version,
        load_inputs,
        load_round,
    )

    round_id = args.get("round_id")
    if round_id is None:
        round_id = db.execute(
            text("SELECT id FROM bid_rounds ORDER BY created_at DESC LIMIT 1")
        ).scalar_one_or_none()
        if round_id is None:
            return {"render": "error", "message": "No bid rounds exist yet.", "code": "NO_ROUND"}
    round_id = int(round_id)

    rnd = load_round(db, round_id)
    if rnd is None:
        return {"render": "error", "message": f"Bid round {round_id} not found.", "code": "NO_ROUND"}
    if rnd["status"] != "closed":
        return {
            "render": "error",
            "message": (
                f"Round {round_id} is '{rnd['status']}' — bids must be closed before "
                "awarding. Close the round first."
            ),
            "code": "NOT_CLOSED",
        }

    inp = load_inputs(db, round_id)
    result = compute_award(inp)
    version = compute_inputs_version(inp)
    token = issue_vacation_token(db, round_id=round_id, expected_version=version)
    db.commit()

    # Awards table, sorted by seniority for a readable waterfall.
    rows = [
        [a["seniority_rank"], a["full_name"], a["employee_id"], a["week_start"],
         f"#{a['awarded_pref_rank']}"]
        for a in sorted(result.awards, key=lambda x: x["seniority_rank"])
    ]
    s = result.summary
    return {
        "render": "table",
        "title": (
            f"Award preview — {rnd['name']} (round {round_id}): "
            f"{s['n_awarded']} weeks to {s['n_agents']} agents, "
            f"{s['n_zero_win']} got nothing, {len(s['weeks_at_capacity'])} weeks maxed"
        ),
        "columns": ["Seniority", "Agent", "ID", "Week", "Pref"],
        "rows": rows,
        "apply_token": token.token,
        "vacation_award": {
            "round_id": round_id,
            "n_awarded": s["n_awarded"],
            "n_agents": s["n_agents"],
            "n_zero_win": s["n_zero_win"],
            "n_denied": len(result.denials),
            "weeks_at_capacity": len(s["weeks_at_capacity"]),
        },
    }
