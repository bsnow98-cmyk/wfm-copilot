"""
preview_staffing_target — read-only preview of changing an SL/ASA target.

Surface #5 of EXECUTION_ROADMAP.md. Resolves the current staffing scenario for a
queue (latest completed forecast run → its staffing_requirements row), shows the
target change and its *estimated* peak-staffing impact (Erlang C, read-only),
and mints a token. Applying runs an async recompute (see
app/routers/staffing_targets.py); the LLM previews but never writes.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "preview_staffing_target",
    "description": (
        "Preview (read-only) changing the service-level and/or ASA target for a "
        "queue's staffing, then recomputing required headcount. Shows current vs "
        "proposed targets and the estimated peak-staffing impact, and surfaces "
        "an Apply button. Use when the user says e.g. 'raise the SL target to "
        "85%', 'tighten ASA to 20s and restaff'. Does NOT modify staffing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {"type": "string", "description": "Queue name, e.g. 'all'."},
            "sl": {
                "type": "number",
                "description": "New service-level target as a percent (e.g. 85) or fraction (0.85).",
            },
            "target_answer_seconds": {
                "type": "integer",
                "description": "Seconds the SL is measured within (e.g. 20).",
            },
            "asa": {
                "type": "number",
                "description": "New ASA ceiling in seconds (e.g. 20). 0 or omitted leaves it unchanged.",
            },
            "shrinkage": {
                "type": "number",
                "description": "New shrinkage as a percent (e.g. 30) or fraction (0.30).",
            },
        },
        "required": ["queue"],
    },
}


def _as_fraction(v: float) -> float:
    return v / 100.0 if v > 1 else v


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    from app.services.apply_tokens import issue_staffing_token
    from app.services.staffing_target import (
        compute_peak_for_targets,
        compute_targets_version,
        load_targets,
        peak_required,
    )

    queue = args["queue"]

    run_id = db.execute(
        text(
            """
            SELECT id FROM forecast_runs
            WHERE queue = :queue AND status = 'completed'
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"queue": queue},
    ).scalar_one_or_none()
    if run_id is None:
        return {"render": "error", "message": f"No completed forecast run for queue {queue!r}.", "code": "FORECAST_NOT_FOUND"}

    staffing_id = db.execute(
        text(
            """
            SELECT id FROM staffing_requirements
            WHERE forecast_run_id = :rid ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"rid": int(run_id)},
    ).scalar_one_or_none()
    if staffing_id is None:
        return {
            "render": "error",
            "message": f"No staffing scenario exists for queue {queue!r} yet — compute staffing first.",
            "code": "NO_STAFFING",
        }

    loaded = load_targets(db, int(staffing_id))
    if loaded is None:
        return {"render": "error", "message": "Staffing scenario vanished.", "code": "NO_STAFFING"}
    before, forecast_run_id = loaded

    # Build the proposed targets from whichever fields were supplied.
    new_targets: dict[str, Any] = {}
    if args.get("sl") is not None:
        new_targets["sl"] = _as_fraction(float(args["sl"]))
    if args.get("target_answer_seconds") is not None:
        new_targets["target_answer_seconds"] = int(args["target_answer_seconds"])
    if args.get("asa"):
        new_targets["target_asa_seconds"] = int(args["asa"])
    if args.get("shrinkage") is not None:
        new_targets["shrinkage"] = _as_fraction(float(args["shrinkage"]))

    if not new_targets:
        return {
            "render": "error",
            "message": "Provide at least one of: sl, target_answer_seconds, asa, shrinkage.",
            "code": "BAD_ARGS",
        }

    after = dict(before)
    after.update(new_targets)
    if after == before:
        return {"render": "error", "message": "Proposed targets match current — nothing to change.", "code": "NO_CHANGE"}

    peak_before = peak_required(db, int(staffing_id))
    peak_after = compute_peak_for_targets(db, forecast_run_id, after)

    version = compute_targets_version(before)
    token = issue_staffing_token(
        db,
        staffing_id=int(staffing_id),
        new_targets=new_targets,
        expected_version=version,
    )
    db.commit()

    def _sl(t: dict[str, Any]) -> str:
        return f"{t['sl']*100:.0f}%" if t.get("sl") is not None else "—"

    def _asa(t: dict[str, Any]) -> str:
        return f"{t['target_asa_seconds']}s" if t.get("target_asa_seconds") is not None else "—"

    rows = [
        ["Service level", _sl(before), _sl(after)],
        ["Answer within", f"{before['target_answer_seconds']}s", f"{after['target_answer_seconds']}s"],
        ["ASA ceiling", _asa(before), _asa(after)],
        ["Shrinkage", f"{before['shrinkage']*100:.0f}%", f"{after['shrinkage']*100:.0f}%"],
        ["Peak required (est.)", str(peak_before), str(peak_after)],
    ]
    return {
        "render": "table",
        "title": f"Change staffing target — {queue} — applying recomputes staffing (async)",
        "columns": ["Metric", "Current", "Proposed"],
        "rows": rows,
        "apply_token": token.token,
        "staffing_target": {
            "queue": queue,
            "peak_before": peak_before,
            "peak_after_est": peak_after,
        },
    }
