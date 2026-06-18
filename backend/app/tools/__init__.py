"""
Tool registry — Phase 6 + Waves 1–4.

Each tool module exports:
- `definition`: Anthropic SDK tool definition (name, description, input_schema)
- `handler(args: dict, db: Session) -> dict`: returns a ToolResponse-shaped dict
  that the frontend renderer dispatches on `render`.

The `render` field MUST match one of the seven values defined in
frontend/src/chat/types.ts. If you add a new value, add a renderer in
frontend/src/chat/renderers/ and a sample to test/chat-renderer.spec.tsx.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.tools import (
    # Phase 6 base
    compare_scenarios,
    get_anomalies,
    get_forecast,
    get_long_range_forecast,
    get_schedule,
    get_staffing,
    preview_schedule_change,
    # Phase 8
    explain_substitution,
    get_skills_coverage,
    # Wave 1+2 (May 1)
    explain_sl_miss,
    find_shift_coverage,
    get_daily_summary,
    get_forecast_accuracy,
    get_intraday_gaps,
    get_occupancy,
    get_top_risks,
    recommend_ot,
    recommend_skill_rebalance,
    recommend_vto,
    # Wave 3 — adherence
    explain_adherence_drop,
    get_adherence,
    get_conformance,
    get_exceptions,
    # Wave 3 — real-time
    get_agents_on_aux,
    get_realtime_alerts,
    get_realtime_status,
    recommend_break_shift,
    # Wave 3 — PTO/leave
    check_leave_feasibility,
    get_leave_requests,
    get_pto_balance,
    recommend_leave_approval,
    # Wave 4 — performance
    get_agent_performance,
    get_attrition_risk,
    get_new_hire_progress,
    get_team_kpis,
    rank_agents,
    # Wave 4 — training
    check_training_impact,
    get_class_progress,
    get_skill_certifications,
    get_training_calendar,
    recommend_coaching_slot,
)

log = logging.getLogger("wfm.tools")

ToolHandler = Callable[[dict[str, Any], Session], dict[str, Any]]

_MODULES = [
    # Phase 6 base
    get_forecast,
    get_long_range_forecast,
    get_staffing,
    get_schedule,
    get_anomalies,
    compare_scenarios,
    preview_schedule_change,
    # Phase 8
    get_skills_coverage,
    explain_substitution,
    # Wave 1+2
    get_intraday_gaps,
    get_forecast_accuracy,
    explain_sl_miss,
    get_top_risks,
    get_daily_summary,
    recommend_vto,
    recommend_ot,
    find_shift_coverage,
    recommend_skill_rebalance,
    get_occupancy,
    # Wave 3 — adherence
    get_adherence,
    get_exceptions,
    explain_adherence_drop,
    get_conformance,
    # Wave 3 — real-time
    get_realtime_status,
    get_agents_on_aux,
    get_realtime_alerts,
    recommend_break_shift,
    # Wave 3 — PTO/leave
    get_pto_balance,
    get_leave_requests,
    check_leave_feasibility,
    recommend_leave_approval,
    # Wave 4 — performance
    get_agent_performance,
    rank_agents,
    get_team_kpis,
    get_attrition_risk,
    get_new_hire_progress,
    # Wave 4 — training
    get_training_calendar,
    check_training_impact,
    recommend_coaching_slot,
    get_skill_certifications,
    get_class_progress,
]

_REGISTRY: dict[str, tuple[dict[str, Any], ToolHandler]] = {
    m.definition["name"]: (m.definition, m.handler) for m in _MODULES
}


def all_definitions() -> list[dict[str, Any]]:
    return [d for d, _ in _REGISTRY.values()]


def dispatch(name: str, args: dict[str, Any], db: Session) -> dict[str, Any]:
    """Run a tool by name. Returns a ToolResponse-shaped dict.

    Wraps every handler so a thrown exception becomes a typed render:'error'
    instead of a 500. The chat loop relies on this — every tool call must
    produce *something* the frontend can render.
    """
    if name not in _REGISTRY:
        return {
            "render": "error",
            "message": f"Unknown tool: {name}",
            "code": "UNKNOWN_TOOL",
        }
    _, handler = _REGISTRY[name]
    try:
        return handler(args, db)
    except Exception as exc:  # noqa: BLE001 — we intentionally swallow into the render
        # Log the full exception server-side, but don't leak request IDs,
        # SQL fragments, or stack details into the user-facing render.
        log.exception("Tool %s failed: %s", name, exc)
        return {
            "render": "error",
            "message": f"{name} failed unexpectedly. The team has been notified.",
            "code": "TOOL_ERROR",
        }
