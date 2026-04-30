"""
Phase 6 eval — does every tool actually return one of the seven render types?

This is a structural check that does NOT require the LLM. It runs every tool
against a real DB session and asserts the output matches a typed renderer
(NOT the JsonPretty fallback).

Skipped if WFM_DB_TEST_URL is unset. The CI Postgres image uses synthetic data
seeded by the docker-compose setup, so the tools have rows to query.
"""
from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.tools import dispatch

pytestmark = pytest.mark.skipif(
    not os.environ.get("WFM_DB_TEST_URL"),
    reason="WFM_DB_TEST_URL not set",
)

VALID_RENDERS = {
    "text",
    "chart.line",
    "chart.bar",
    "table",
    "gantt",
    "scenarios",
    "error",
}


@pytest.fixture(scope="module")
def db():
    engine = create_engine(os.environ["WFM_DB_TEST_URL"])
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


CASES = [
    ("get_forecast", {"queue": "sales_inbound", "date": date.today().isoformat()}),
    ("get_staffing", {"queue": "sales_inbound", "sl": 0.8, "asa": 20}),
    ("get_schedule", {"date": date.today().isoformat()}),
    ("get_anomalies", {}),
    (
        "compare_scenarios",
        {
            "queue": "sales_inbound",
            "scenarios": [
                {"name": "Baseline", "sl": 0.8, "asa": 20},
                {"name": "Tight", "sl": 0.9, "asa": 15},
            ],
        },
    ),
    (
        "preview_schedule_change",
        {
            "date": date.today().isoformat(),
            "changes": [
                {
                    "agent_id": "ag_001",
                    "start": f"{date.today().isoformat()}T13:00:00",
                    "end": f"{date.today().isoformat()}T13:30:00",
                    "activity": "lunch",
                }
            ],
        },
    ),
    # Phase 8 stage 5 — new tools.
    (
        "get_skills_coverage",
        {"queue": "sales_inbound", "date": date.today().isoformat()},
    ),
    (
        "explain_substitution",
        {"queue": "sales_inbound", "skill": "support"},
    ),
]


@pytest.mark.parametrize("name,args", CASES, ids=lambda x: x if isinstance(x, str) else "")
def test_tool_returns_typed_render(name: str, args: dict, db) -> None:
    out = dispatch(name, args, db)
    assert isinstance(out, dict)
    assert out.get("render") in VALID_RENDERS, (
        f"{name} returned render={out.get('render')!r}, not in the typed set. "
        f"This would fall through to JsonPretty on the frontend."
    )
