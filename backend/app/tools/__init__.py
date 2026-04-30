"""
Tool registry — Phase 6.

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
    compare_scenarios,
    explain_substitution,
    get_anomalies,
    get_forecast,
    get_schedule,
    get_skills_coverage,
    get_staffing,
    preview_schedule_change,
)

log = logging.getLogger("wfm.tools")

ToolHandler = Callable[[dict[str, Any], Session], dict[str, Any]]

_REGISTRY: dict[str, tuple[dict[str, Any], ToolHandler]] = {
    get_forecast.definition["name"]: (get_forecast.definition, get_forecast.handler),
    get_staffing.definition["name"]: (get_staffing.definition, get_staffing.handler),
    get_schedule.definition["name"]: (get_schedule.definition, get_schedule.handler),
    get_anomalies.definition["name"]: (get_anomalies.definition, get_anomalies.handler),
    compare_scenarios.definition["name"]: (
        compare_scenarios.definition,
        compare_scenarios.handler,
    ),
    preview_schedule_change.definition["name"]: (
        preview_schedule_change.definition,
        preview_schedule_change.handler,
    ),
    get_skills_coverage.definition["name"]: (
        get_skills_coverage.definition,
        get_skills_coverage.handler,
    ),
    explain_substitution.definition["name"]: (
        explain_substitution.definition,
        explain_substitution.handler,
    ),
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
        log.exception("Tool %s failed: %s", name, exc)
        return {
            "render": "error",
            "message": f"{name} failed: {exc}",
            "code": "TOOL_ERROR",
        }
