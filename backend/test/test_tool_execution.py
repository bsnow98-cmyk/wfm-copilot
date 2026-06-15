"""
Execution eval: every registered tool must RUN, not just get selected.

eval_tool_selection.py proves the model picks the right tool; nothing proved
the tool works when called. A signature bug in get_anomalies crashed on every
invocation for weeks and no test noticed, because the registry wrapper
converts crashes into render:'error' with code=TOOL_ERROR.

This test calls every tool in the registry against the live local DB with
synthesized arguments. Graceful, typed errors (NO_SCHEDULE, UNKNOWN_SKILL,
FORECAST_NOT_FOUND...) are acceptable — an unhandled crash (TOOL_ERROR) or a
malformed render is a failure. Also asserts every render is JSON-serializable,
which is the ToolResponse wire contract.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.tools import _REGISTRY, dispatch


@pytest.fixture(scope="module")
def db():
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
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(scope="module")
def ctx(db: Session) -> dict[str, Any]:
    """Real identifiers from the seeded DB so args resolve to actual rows."""
    from app.services.realtime_clock import sim_today

    today = sim_today(db)
    employee_id = db.execute(
        text("SELECT employee_id FROM agents WHERE active = TRUE ORDER BY id LIMIT 1")
    ).scalar_one_or_none()
    queue = db.execute(
        text("SELECT queue FROM forecast_runs WHERE status='completed' "
             "ORDER BY created_at DESC LIMIT 1")
    ).scalar_one_or_none()
    skill = db.execute(text("SELECT name FROM skills ORDER BY id LIMIT 1")).scalar_one_or_none()
    if employee_id is None:
        pytest.skip("No agents seeded")
    return {
        "today": today,
        "employee_id": employee_id,
        "queue": queue or "all",
        "skill": skill or "sales",
    }


def _value_for(name: str, spec: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """Synthesize a plausible value for one schema property."""
    if "enum" in spec:
        return spec["enum"][0]
    n = name.lower()
    t = spec.get("type")
    if n in ("date", "since_date", "target_date", "day", "on_date"):
        return ctx["today"].isoformat()
    if n in ("start_date",):
        return ctx["today"].isoformat()
    if n in ("end_date",):
        return (ctx["today"] + timedelta(days=7)).isoformat()
    if n == "start_time":
        return "09:00"
    if n == "end_time":
        return "11:00"
    if "employee" in n or n == "agent_id":
        return ctx["employee_id"]
    if n == "queue":
        return ctx["queue"]
    if "skill" in n:
        return ctx["skill"]
    if t == "integer":
        return 5
    if t == "number":
        return 5
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "string":
        return ctx["today"].isoformat()
    return None


def _args_for(definition: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    schema = definition.get("input_schema", {})
    props = schema.get("properties", {})
    required = schema.get("required", [])
    return {name: _value_for(name, props.get(name, {}), ctx) for name in required}


# Tools whose required args can't be synthesized generically.
def _overrides(ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    d = ctx["today"].isoformat()
    return {
        "preview_schedule_change": {
            "changes": [
                {
                    "agent_id": ctx["employee_id"],
                    "start": f"{d}T09:00:00+00:00",
                    "end": f"{d}T10:00:00+00:00",
                    "activity": "available",
                }
            ],
        },
    }


def test_every_registered_tool_executes_without_crashing(
    db: Session, ctx: dict[str, Any]
) -> None:
    overrides = _overrides(ctx)
    crashed: list[str] = []
    malformed: list[str] = []

    for name, (definition, _handler) in sorted(_REGISTRY.items()):
        args = {**_args_for(definition, ctx), **overrides.get(name, {})}
        out = dispatch(name, args, db)
        # dispatch leaves the session dirty after graceful error paths in
        # some tools; reset so one tool's state never poisons the next.
        db.rollback()

        if not isinstance(out, dict) or not isinstance(out.get("render"), str):
            malformed.append(f"{name}: {out!r:.120}")
            continue
        try:
            json.dumps(out, default=str)
        except (TypeError, ValueError):
            malformed.append(f"{name}: render not JSON-serializable")
        # Graceful typed errors are fine; an unhandled crash is not.
        if out.get("render") == "error" and out.get("code") == "TOOL_ERROR":
            crashed.append(f"{name}({args})")

    assert not crashed, (
        "Tools crashed (unhandled exception swallowed by dispatch):\n  "
        + "\n  ".join(crashed)
    )
    assert not malformed, "Malformed renders:\n  " + "\n  ".join(malformed)
