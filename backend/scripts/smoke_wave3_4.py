"""
Plain-Python smoke runner for the 22 Wave 3+4 tools.

Dispatches each tool against the live (seeded) DB and reports pass/fail
with a one-line summary per tool. Exits non-zero if any tool returned a
malformed render or raised through the dispatcher's exception guard.

Run via the same docker one-off pattern as generate_wave3_4_data.py:
    docker run --rm --network wfm-copilot_default \\
      -v "$(pwd)/backend:/app" -w /app \\
      -e POSTGRES_HOST=postgres -e POSTGRES_PORT=5432 \\
      -e POSTGRES_USER=wfm -e POSTGRES_PASSWORD=wfm_dev_password \\
      -e POSTGRES_DB=wfm_copilot \\
      wfm-copilot-api python -m scripts.smoke_wave3_4
"""
from __future__ import annotations

import sys
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

import traceback

from app.config import get_settings
from app.tools import _REGISTRY


def _call_direct(name: str, args: dict[str, Any], db: Session) -> dict[str, Any]:
    """Bypass dispatch's swallow-and-sanitize so we see the real exception."""
    _, handler = _REGISTRY[name]
    try:
        return handler(args, db)
    except Exception:
        traceback.print_exc()
        db.rollback()
        return {"render": "error", "code": "RAISED", "message": "see traceback above"}

VALID_RENDERS = {"text", "chart.line", "chart.bar", "table", "gantt", "scenarios", "error"}

# Tools with no required args (or all defaults) — dispatched with empty/defaults.
SIMPLE_CASES: list[tuple[str, dict[str, Any]]] = [
    # Wave 3 adherence
    ("get_adherence", {}),
    ("get_adherence", {"aggregation": "agent"}),
    ("get_exceptions", {}),
    ("explain_adherence_drop", {}),
    ("get_conformance", {}),
    # Wave 3 real-time
    ("get_realtime_status", {}),
    ("get_agents_on_aux", {}),
    ("get_realtime_alerts", {}),
    ("recommend_break_shift", {"direction": "earlier", "minutes": 30, "candidates": 3}),
    # Wave 3 PTO/leave
    ("get_pto_balance", {}),
    ("get_leave_requests", {}),
    ("recommend_leave_approval", {"horizon_days": 30}),
    # Wave 4 performance
    ("rank_agents", {"metric": "adherence", "limit": 5}),
    ("rank_agents", {"metric": "qa", "limit": 5, "order": "asc"}),
    ("rank_agents", {"metric": "exceptions", "limit": 5}),
    ("rank_agents", {"metric": "tenure", "limit": 5}),
    ("get_team_kpis", {}),
    ("get_attrition_risk", {"limit": 5}),
    ("get_new_hire_progress", {}),
    # Wave 4 training
    ("get_training_calendar", {"horizon_days": 14}),
    ("get_skill_certifications", {}),
    ("get_class_progress", {}),
]


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db: Session = SessionLocal()

    sim_now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    print(f"sim_now = {sim_now.isoformat()}")
    print(f"{'tool':<30}  {'render':<12}  summary")
    print("-" * 90)

    failures: list[tuple[str, str]] = []

    for name, args in SIMPLE_CASES:
        result = _call_direct(name, args, db)
        db.rollback()
        render = result.get("render", "<missing>")
        if render not in VALID_RENDERS:
            failures.append((name, f"invalid render {render!r}"))
            print(f"{name:<30}  {render:<12}  ❌ invalid render")
            continue
        summary = _summarize(result)
        flag = "❌" if render == "error" else "✅"
        print(f"{name:<30}  {render:<12}  {flag} {summary}")
        if render == "error":
            failures.append((name, result.get("message", "<no message>")))

    # The three tools that need real IDs.
    req_id = db.execute(
        text("SELECT id FROM leave_requests WHERE status = 'pending' ORDER BY id LIMIT 1")
    ).scalar()
    if req_id:
        result = _call_direct("check_leave_feasibility", {"request_id": int(req_id)}, db)
        db.rollback()
        flag = "❌" if result["render"] == "error" else "✅"
        print(
            f"{'check_leave_feasibility':<30}  {result['render']:<12}  "
            f"{flag} {_summarize(result)}"
        )
        if result["render"] == "error":
            failures.append(("check_leave_feasibility", result.get("message", "")))

    ev_id = db.execute(
        text(
            "SELECT id FROM training_events WHERE start_ts > sim_now() ORDER BY start_ts LIMIT 1"
        )
    ).scalar()
    if ev_id:
        result = _call_direct("check_training_impact", {"event_id": int(ev_id)}, db)
        db.rollback()
        flag = "❌" if result["render"] == "error" else "✅"
        print(
            f"{'check_training_impact':<30}  {result['render']:<12}  "
            f"{flag} {_summarize(result)}"
        )
        if result["render"] == "error":
            failures.append(("check_training_impact", result.get("message", "")))

    eid = db.execute(
        text("SELECT employee_id FROM agents WHERE active = TRUE LIMIT 1")
    ).scalar()
    if eid:
        result = _call_direct("get_agent_performance", {"employee_id": eid}, db)
        db.rollback()
        flag = "❌" if result["render"] == "error" else "✅"
        print(
            f"{'get_agent_performance':<30}  {result['render']:<12}  "
            f"{flag} {_summarize(result)}"
        )
        if result["render"] == "error":
            failures.append(("get_agent_performance", result.get("message", "")))

        result = _call_direct("recommend_coaching_slot", {"employee_id": eid}, db)
        db.rollback()
        flag = "❌" if result["render"] == "error" else "✅"
        print(
            f"{'recommend_coaching_slot':<30}  {result['render']:<12}  "
            f"{flag} {_summarize(result)}"
        )
        if result["render"] == "error":
            failures.append(("recommend_coaching_slot", result.get("message", "")))

    print("-" * 90)
    if failures:
        print(f"\n{len(failures)} failures:")
        for name, msg in failures:
            print(f"  {name}: {msg}")
        return 1
    print(f"\nAll smoke checks passed.")
    return 0


def _summarize(result: dict[str, Any]) -> str:
    r = result["render"]
    if r == "error":
        return f"{result.get('code','?')} — {result.get('message','')[:60]}"
    if r == "table":
        title = result.get("title", "<no title>")
        return f"{len(result.get('rows', []))} rows — {title[:70]}"
    if r == "chart.line":
        n_points = sum(len(s.get("points", [])) for s in result.get("series", []))
        return f"{len(result.get('series', []))} series, {n_points} points — {result.get('title','')[:50]}"
    if r == "chart.bar":
        return f"{len(result.get('bars', []))} bars — {result.get('title','')[:60]}"
    if r == "text":
        return f"{(result.get('content','') or '')[:80]}"
    return r


if __name__ == "__main__":
    raise SystemExit(main())
