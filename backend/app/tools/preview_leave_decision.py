"""
preview_leave_decision — read-only preview of approving/denying a leave request.

Surface #1 of EXECUTION_ROADMAP.md. Mirrors preview_schedule_change: this tool
never writes. It reuses check_leave_feasibility's per-day SL math for the diff
the human reviews, then mints an apply_token that pins {request_id, decision,
note, request_version}. The frontend renders an Approve/Deny affordance only
when apply_token is present; the mutating write lives in
app/routers/leave_decisions.py. The LLM can preview but can never decide.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "preview_leave_decision",
    "description": (
        "Preview (read-only) approving or denying a specific pending leave "
        "request. Shows the per-day SL impact and surfaces an Apply button the "
        "human clicks to commit. Use after recommend_leave_approval / "
        "get_leave_requests when the user says e.g. 'approve request 42', "
        "'deny Adams' PTO'. Does NOT decide on its own."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "integer",
                "description": "The leave_requests id to decide (the 'Req ID' column).",
            },
            "decision": {
                "type": "string",
                "enum": ["approve", "deny"],
            },
            "note": {
                "type": "string",
                "description": "Optional decision note recorded on the request.",
            },
        },
        "required": ["request_id", "decision"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    from app.services.apply_tokens import issue_leave_token
    from app.services.leave_decision import (
        compute_request_version,
        leave_pto_hours,
        load_request,
    )
    from app.tools.check_leave_feasibility import handler as feasibility_handler

    request_id = int(args["request_id"])
    decision = args["decision"]
    note = args.get("note")

    info = load_request(db, request_id)
    if info is None:
        return {"render": "error", "message": "Leave request not found.", "code": "NO_REQUEST"}
    if info.status != "pending":
        return {
            "render": "error",
            "message": (
                f"Request {request_id} is already {info.status} — only pending "
                "requests can be decided."
            ),
            "code": "NOT_PENDING",
        }

    # Reuse the feasibility table as the preview body (SL impact per day).
    table = feasibility_handler({"request_id": request_id}, db)
    if table.get("render") != "table":
        # Surface the feasibility error (e.g. no staffing data) directly.
        return table

    version = compute_request_version(info.status, info.decided_at)
    token = issue_leave_token(
        db,
        request_id=request_id,
        request_version=version,
        decision=decision,
        note=note,
    )
    db.commit()  # token persists immediately so a subsequent apply can find it

    # Overall verdict from the per-day rows (last column), so the title carries
    # the SL math without the feasibility tool's redundant agent/date prefix.
    verdicts = {row[-1] for row in table.get("rows", [])}
    overall = "FAIL" if "FAIL" in verdicts else ("WARN" if "WARN" in verdicts else "OK")
    verb = "Approve" if decision == "approve" else "Deny"
    table["title"] = (
        f"{verb} {info.leave_type} — {info.full_name} ({info.employee_id}), "
        f"{info.start_ts.date().isoformat()}→{info.end_ts.date().isoformat()} "
        f"— feasibility verdict: {overall}"
    )
    table["apply_token"] = token.token
    table["leave_decision"] = {
        "request_id": request_id,
        "decision": decision,
        "request_version": version,
        "label": f"{info.full_name} ({info.employee_id})",
        "note": note,
        "pto_hours": leave_pto_hours(info.start_ts, info.end_ts)
        if decision == "approve"
        else None,
    }
    return table
