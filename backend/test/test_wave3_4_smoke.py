"""
Smoke tests for the 22 Wave 3+4 tools.

Two layers:

1. Registry shape — runs without a DB. Confirms every new tool is wired
   into _REGISTRY with a valid definition.
2. Live dispatch — requires Postgres (skipped if unreachable). Calls each
   handler with sensible default args against the seeded DB and asserts:
     - returns a dict with a `render` key
     - render is one of the seven canonical values
     - error renders include code + message (i.e. typed, not blanks)

Run inside the docker network with:
    docker run --rm --network wfm-copilot_default \
      -v "$(pwd)/backend:/app" -w /app \
      -e POSTGRES_HOST=postgres -e POSTGRES_PORT=5432 \
      -e POSTGRES_USER=wfm -e POSTGRES_PASSWORD=wfm_dev_password \
      -e POSTGRES_DB=wfm_copilot \
      wfm-copilot-api pytest test/test_wave3_4_smoke.py -v
"""
from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.tools import _REGISTRY, dispatch

WAVE_3_4_TOOLS: dict[str, dict[str, Any]] = {
    # ---- Wave 3 adherence ----
    "get_adherence": {},
    "get_exceptions": {},
    "explain_adherence_drop": {},
    "get_conformance": {},
    # ---- Wave 3 real-time ----
    "get_realtime_status": {},
    "get_agents_on_aux": {},
    "get_realtime_alerts": {},
    "recommend_break_shift": {"direction": "earlier", "minutes": 30, "candidates": 3},
    # ---- Wave 3 PTO/leave ----
    "get_pto_balance": {},
    "get_leave_requests": {},
    "recommend_leave_approval": {"horizon_days": 30},
    # check_leave_feasibility tested separately — needs a real request_id
    # ---- Wave 4 performance ----
    "rank_agents": {"metric": "adherence", "limit": 5},
    "get_team_kpis": {},
    "get_attrition_risk": {"limit": 5},
    "get_new_hire_progress": {},
    # ---- Wave 4 training ----
    "get_training_calendar": {"horizon_days": 14},
    "recommend_coaching_slot": {},  # will fail with NO_AGENT — caught below
    "get_skill_certifications": {},
    "get_class_progress": {},
    # check_training_impact tested separately — needs a real event_id or window
    # get_agent_performance tested separately — needs a real employee_id
}

VALID_RENDERS = {"text", "chart.line", "chart.bar", "table", "gantt", "scenarios", "error"}


# ---------------------------------------------------------------------------
# Layer 1 — registry shape (no DB)
# ---------------------------------------------------------------------------


def test_all_wave_3_4_tools_registered() -> None:
    expected = set(WAVE_3_4_TOOLS.keys()) | {
        "check_leave_feasibility",
        "check_training_impact",
        "get_agent_performance",
    }
    missing = expected - set(_REGISTRY.keys())
    assert not missing, f"Wave 3+4 tools missing from registry: {missing}"


def test_all_wave_3_4_definitions_have_required_fields() -> None:
    for name in WAVE_3_4_TOOLS:
        definition, _ = _REGISTRY[name]
        assert definition["name"] == name
        assert isinstance(definition["description"], str)
        assert len(definition["description"]) > 30, (
            f"{name}: description too short (LLM tool selection needs context)"
        )
        assert definition["input_schema"]["type"] == "object"
        assert "properties" in definition["input_schema"]


# ---------------------------------------------------------------------------
# Layer 2 — live dispatch (requires Postgres + seeded data)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db() -> Session:
    user = os.environ.get("POSTGRES_USER", "wfm")
    pwd = os.environ.get("POSTGRES_PASSWORD", "wfm_dev_password")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "wfm_copilot")
    url = f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{name}"
    try:
        engine = create_engine(url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip(f"Postgres not reachable at {host}:{port}")
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()
    yield session
    session.close()


@pytest.mark.parametrize("tool_name,args", WAVE_3_4_TOOLS.items())
def test_wave_3_4_tool_runs_without_crashing(
    db: Session, tool_name: str, args: dict[str, Any]
) -> None:
    out = dispatch(tool_name, args, db)
    assert isinstance(out, dict)
    assert "render" in out, f"{tool_name}: response missing render key"
    assert out["render"] in VALID_RENDERS, f"{tool_name}: invalid render {out['render']!r}"
    if out["render"] == "error":
        # Typed error: code + message, never bare exception text
        assert "message" in out and out["message"]
        assert "code" in out and out["code"]


def test_check_leave_feasibility_with_real_request(db: Session) -> None:
    req_id = db.execute(
        text("SELECT id FROM leave_requests WHERE status = 'pending' ORDER BY id LIMIT 1")
    ).scalar()
    if req_id is None:
        pytest.skip("No pending leave_requests seeded")
    out = dispatch("check_leave_feasibility", {"request_id": int(req_id)}, db)
    assert out["render"] in {"table", "error"}


def test_get_agent_performance_with_real_agent(db: Session) -> None:
    eid = db.execute(
        text("SELECT employee_id FROM agents WHERE active = TRUE LIMIT 1")
    ).scalar()
    if eid is None:
        pytest.skip("No active agents seeded")
    out = dispatch("get_agent_performance", {"employee_id": eid}, db)
    assert out["render"] == "table"
    assert out["columns"] == ["Metric", "Value"]
    assert len(out["rows"]) > 5


def test_check_training_impact_with_real_event(db: Session) -> None:
    ev_id = db.execute(
        text(
            "SELECT id FROM training_events WHERE start_ts > sim_now() ORDER BY start_ts LIMIT 1"
        )
    ).scalar()
    if ev_id is None:
        pytest.skip("No upcoming training events seeded")
    out = dispatch("check_training_impact", {"event_id": int(ev_id)}, db)
    assert out["render"] in {"table", "error"}


def test_recommend_coaching_slot_with_real_agent(db: Session) -> None:
    eid = db.execute(
        text("SELECT employee_id FROM agents WHERE active = TRUE LIMIT 1")
    ).scalar()
    if eid is None:
        pytest.skip("No active agents seeded")
    out = dispatch("recommend_coaching_slot", {"employee_id": eid}, db)
    # Either a real slot table, or the "no comfortable slot found" text — both valid
    assert out["render"] in {"table", "text", "error"}


def test_sim_clock_advances(db: Session) -> None:
    """sim_now() should be inside the seeded shift_segments window."""
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    seg_range = db.execute(
        text("SELECT MIN(start_time) AS lo, MAX(start_time) AS hi FROM shift_segments")
    ).mappings().one()
    # sim_now must lie inside (or be close to) the shift window for the live
    # ticker to read real data.
    from datetime import timedelta
    assert seg_range["lo"] - timedelta(days=2) <= now <= seg_range["hi"] + timedelta(days=2), (
        f"sim_now {now} is outside the shift window {seg_range['lo']}→{seg_range['hi']}"
    )


def test_sim_anchor_self_heals_after_drift(db: Session) -> None:
    """The startup check must pull a drifted sim clock back into the window.

    Deliberately breaks the anchor (sim-now jumped 30 days past the data),
    runs the self-heal, and asserts the clock is back inside. The DB ends in
    the HEALED state, so this is safe against the live local database.
    """
    from datetime import timedelta

    from app.services.realtime_clock import (
        ensure_sim_anchor_in_window,
        reset_anchor,
        sim_now,
    )

    window = db.execute(
        text("SELECT MIN(start_time) AS lo, MAX(start_time) AS hi FROM shift_segments")
    ).mappings().one()
    if window["lo"] is None:
        pytest.skip("No shift_segments seeded")

    # Break it: anchor sim-now 30 days past the end of the data.
    reset_anchor(db, anchor_sim_ts=window["hi"] + timedelta(days=30))
    assert sim_now(db) > window["hi"]

    # Heal it.
    assert ensure_sim_anchor_in_window(db) is True
    healed = sim_now(db)
    assert window["lo"] <= healed <= window["hi"], (
        f"self-heal left sim_now {healed} outside {window['lo']}→{window['hi']}"
    )

    # Second call is a no-op.
    assert ensure_sim_anchor_in_window(db) is False


def test_read_path_sim_heal_throttles(db: Session) -> None:
    """maybe_ensure_sim_anchor heals when due and throttles between checks.

    Like the test above, the DB deliberately ends in the HEALED state.
    """
    from datetime import timedelta

    from app.services import realtime_clock as rc

    window = db.execute(
        text("SELECT MIN(start_time) AS lo, MAX(start_time) AS hi FROM shift_segments")
    ).mappings().one()
    if window["lo"] is None:
        pytest.skip("No shift_segments seeded")

    # Break the clock; an un-throttled check must heal it.
    rc.reset_anchor(db, anchor_sim_ts=window["hi"] + timedelta(days=30))
    rc._last_window_check_monotonic = float("-inf")
    assert rc.maybe_ensure_sim_anchor(db, min_interval_s=0) is True
    assert window["lo"] <= rc.sim_now(db) <= window["hi"]

    # Break it again; a throttled call must NOT touch it...
    rc.reset_anchor(db, anchor_sim_ts=window["hi"] + timedelta(days=30))
    assert rc.maybe_ensure_sim_anchor(db, min_interval_s=600) is False
    assert rc.sim_now(db) > window["hi"]

    # ...and the next due check heals it (leaves the DB healthy).
    rc._last_window_check_monotonic = float("-inf")
    assert rc.maybe_ensure_sim_anchor(db, min_interval_s=0) is True
    assert window["lo"] <= rc.sim_now(db) <= window["hi"]
