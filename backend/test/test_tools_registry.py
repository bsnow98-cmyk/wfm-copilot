"""
Smoke tests for the Phase 6 tool registry.

These run without a database — they exercise the registry shape, definition
schemas, and the dispatch error wrapping. End-to-end DB tests live in
test_tools_with_db.py (require Postgres).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.tools import _REGISTRY, all_definitions, dispatch

EXPECTED_TOOLS = {
    "get_forecast",
    "get_staffing",
    "get_schedule",
    "get_anomalies",
    "compare_scenarios",
    "preview_schedule_change",
    "get_skills_coverage",   # Phase 8 stage 2
    "explain_substitution",  # Phase 8 stage 3
}

VALID_RENDERS = {
    "text",
    "chart.line",
    "chart.bar",
    "table",
    "gantt",
    "scenarios",
    "error",
}


def test_registry_has_all_expected_tools() -> None:
    assert set(_REGISTRY.keys()) == EXPECTED_TOOLS


def test_definitions_have_required_fields() -> None:
    for d in all_definitions():
        assert "name" in d and isinstance(d["name"], str)
        assert "description" in d and len(d["description"]) > 20
        schema = d["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema


def test_dispatch_unknown_tool_returns_error_render() -> None:
    db = MagicMock()
    out = dispatch("not_a_tool", {}, db)
    assert out["render"] == "error"
    assert out["code"] == "UNKNOWN_TOOL"


def test_dispatch_handler_exception_becomes_error_render() -> None:
    """A tool that raises should produce render:'error', not bubble."""
    from app.tools import _REGISTRY as R
    from app.tools import get_forecast as gf

    original = R["get_forecast"]

    def boom(_args: dict, _db: object) -> dict:
        raise RuntimeError("simulated")

    R["get_forecast"] = (gf.definition, boom)
    try:
        out = dispatch("get_forecast", {"queue": "x"}, MagicMock())
        assert out["render"] == "error"
        assert "simulated" in out["message"]
        assert out["code"] == "TOOL_ERROR"
    finally:
        R["get_forecast"] = original


def test_get_anomalies_falls_back_to_empty_when_table_missing() -> None:
    """Phase 5 hasn't shipped yet; the tool must not blow up."""
    from sqlalchemy.exc import ProgrammingError

    from app.tools import get_anomalies as ga

    db = MagicMock()
    db.execute.side_effect = ProgrammingError("stmt", {}, Exception("no table"))
    out = ga.handler({}, db)
    assert out["render"] == "table"
    assert out["rows"] == []
    assert out["columns"] == [
        "id",
        "date",
        "queue",
        "category",
        "severity",
        "score",
    ]
