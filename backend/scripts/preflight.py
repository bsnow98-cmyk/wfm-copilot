"""
Pre-flight sanity check — run before deploying or recording a demo.

Verifies the things that ONLY break at runtime against real infrastructure.
The 78+20 unit tests prove correctness against mocks; this script proves the
real ANTHROPIC_API_KEY works, the real Postgres is reachable, the migrations
applied, and the tool registry boots.

Each check runs independently and prints PASS / FAIL / WARN. A non-zero exit
code means at least one check FAILed. WARNs don't fail the run — they're
observability ("you have no agents seeded yet" — true but not blocking).

Usage:
    docker compose exec api python -m scripts.preflight

    # Or against a deployed instance:
    DATABASE_URL=postgresql+psycopg://... \\
    ANTHROPIC_API_KEY=sk-ant-... \\
    python -m scripts.preflight

The Anthropic check makes a real (small) API call, so it costs a fraction of
a cent and counts against the rate limit. Skip with --no-anthropic if you
just want the local checks.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL" | "WARN"
    detail: str = ""

    @property
    def is_failure(self) -> bool:
        return self.status == "FAIL"


def _safe(fn: Callable[[], CheckResult]) -> CheckResult:
    """Run a check function, converting unexpected exceptions into FAIL."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — preflight intentionally swallows
        return CheckResult(
            name=getattr(fn, "__name__", "unknown"),
            status="FAIL",
            detail=f"{type(exc).__name__}: {exc}",
        )


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_env_vars() -> CheckResult:
    """All required env vars must be set."""
    from app.config import get_settings

    settings = get_settings()
    missing: list[str] = []
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    # Postgres has defaults that work for local docker-compose, but warn if
    # they're still those defaults in a production-looking env.
    if not settings.postgres_host:
        missing.append("POSTGRES_HOST")
    if missing:
        return CheckResult("env_vars", "FAIL", f"missing: {', '.join(missing)}")

    notes: list[str] = []
    if settings.postgres_password == "wfm_dev_password":
        notes.append("POSTGRES_PASSWORD is the dev default — change before deploy")
    if not settings.wfm_demo_password:
        notes.append("WFM_DEMO_PASSWORD unset — auth gate is disabled")
    if notes:
        return CheckResult("env_vars", "WARN", "; ".join(notes))
    return CheckResult("env_vars", "PASS")


def check_db_reach() -> CheckResult:
    from sqlalchemy import text

    from app.db import engine

    with engine.connect() as conn:
        conn.execute(text("SELECT 1")).scalar_one()
    return CheckResult("db_reach", "PASS")


def check_db_schema() -> CheckResult:
    """Verify every Phase migration has applied — query for a known table
    from each phase's schema."""
    from sqlalchemy import text

    from app.db import engine

    expected_tables = [
        "interval_history",          # Phase 1
        "forecast_runs",             # Phase 2
        "staffing_requirements",     # Phase 3
        "schedules",                 # Phase 4
        "anomalies",                 # Phase 5
        "chat_conversations",        # Phase 6
        "chat_messages",             # Phase 6
        "chat_tool_calls",           # Phase 6 observability
        "schedule_change_log",       # Cherry-pick D
        "notifications",             # Cherry-pick D
        "chat_apply_tokens",         # Cherry-pick D
    ]
    with engine.connect() as conn:
        existing = {
            r[0]
            for r in conn.execute(
                text(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                    """
                )
            ).all()
        }
    missing = [t for t in expected_tables if t not in existing]
    if missing:
        return CheckResult(
            "db_schema",
            "FAIL",
            f"missing tables: {', '.join(missing)} — migrations didn't fully run",
        )
    return CheckResult("db_schema", "PASS", f"{len(expected_tables)} tables present")


def check_tool_registry() -> CheckResult:
    """Importing the registry exercises every tool module's import path —
    catches dangling syntax / missing-import errors that test_tools_registry
    might miss if it's stale."""
    from app.tools import all_definitions

    defs = all_definitions()
    if len(defs) < 8:
        return CheckResult(
            "tool_registry",
            "FAIL",
            f"expected ≥8 tools, found {len(defs)}: {[d['name'] for d in defs]}",
        )
    return CheckResult(
        "tool_registry",
        "PASS",
        f"{len(defs)} tools: {sorted(d['name'] for d in defs)}",
    )


def check_anthropic_roundtrip() -> CheckResult:
    """Make a tiny real API call. Costs a fraction of a cent. Catches
    invalid keys, model-not-available, network blocks before they hit
    the live demo."""
    from anthropic import Anthropic

    from app.config import get_settings

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )
    if not resp.content:
        return CheckResult("anthropic_roundtrip", "FAIL", "empty response")
    return CheckResult(
        "anthropic_roundtrip",
        "PASS",
        f"model={settings.anthropic_model} tokens_in={resp.usage.input_tokens}",
    )


def check_seed_status() -> CheckResult:
    """Warn (not fail) when the DB is empty — common on fresh deploys before
    seed-data scripts run. The chat won't have anything to talk about."""
    from sqlalchemy import text

    from app.db import engine

    with engine.connect() as conn:
        intervals = conn.execute(
            text("SELECT COUNT(*) FROM interval_history")
        ).scalar_one()
        agents = conn.execute(
            text("SELECT COUNT(*) FROM agents WHERE active = TRUE")
        ).scalar_one()
        skills = conn.execute(text("SELECT COUNT(*) FROM skills")).scalar_one()

    notes: list[str] = []
    if intervals == 0:
        notes.append(
            "interval_history empty — run scripts.generate_synthetic_data --seed-db"
        )
    if agents == 0:
        notes.append("no active agents — run scripts.seed_agents --multi-skill")
    if skills == 0:
        notes.append("skills table empty — Phase 8 features won't work")

    detail = (
        f"intervals={intervals:,} agents={agents} skills={skills}"
        + (" — " + "; ".join(notes) if notes else "")
    )
    if intervals == 0 or agents == 0:
        return CheckResult("seed_status", "WARN", detail)
    return CheckResult("seed_status", "PASS", detail)


def check_chat_loop_can_load_history() -> CheckResult:
    """Verifies _load_history can run against the live DB — catches column
    drift, casts (uuid::text), and JSONB serialization issues that mocks
    don't see."""
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.routers.chat import _load_history, _persist_message

    with SessionLocal() as db:
        new_id = db.execute(
            text("INSERT INTO chat_conversations DEFAULT VALUES RETURNING id")
        ).scalar_one()
        db.commit()
        conv_id = str(new_id)
        msg_id = _persist_message(db, conv_id, "user", "preflight ping")
        if msg_id is None:
            return CheckResult(
                "chat_loop", "FAIL", "_persist_message returned None — DB writes failing"
            )
        history = _load_history(db, conv_id)
        # Clean up — preflight shouldn't litter the DB.
        db.execute(
            text("DELETE FROM chat_conversations WHERE id = :id::uuid"),
            {"id": conv_id},
        )
        db.commit()

    if not history:
        return CheckResult("chat_loop", "FAIL", "_load_history returned empty after persist")
    return CheckResult("chat_loop", "PASS", f"{len(history)} message(s) round-tripped")


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
ALL_CHECKS: list[Callable[[], CheckResult]] = [
    check_env_vars,
    check_db_reach,
    check_db_schema,
    check_tool_registry,
    check_seed_status,
    check_chat_loop_can_load_history,
    # check_anthropic_roundtrip is opt-out via --no-anthropic
]


def run(skip_anthropic: bool = False) -> int:
    checks = list(ALL_CHECKS)
    if not skip_anthropic:
        checks.append(check_anthropic_roundtrip)

    print("=" * 64)
    print(" WFM Copilot — pre-flight check")
    print("=" * 64)

    failures = 0
    warns = 0
    for fn in checks:
        result = _safe(fn)
        symbol = {"PASS": "✓", "FAIL": "✗", "WARN": "!"}.get(result.status, "?")
        line = f"  {symbol} {result.status:<5} {result.name}"
        if result.detail:
            line += f"  ({result.detail})"
        print(line)
        if result.is_failure:
            failures += 1
        elif result.status == "WARN":
            warns += 1

    print("-" * 64)
    print(f"  {failures} failed, {warns} warned, {len(checks) - failures - warns} passed")
    print("=" * 64)
    if failures:
        print(
            "\nDo NOT deploy or record a demo until failures are resolved.\n"
            "Warnings are tolerable but worth understanding."
        )
        return 1
    if warns:
        print(
            "\nReady to deploy, with the warnings noted above. "
            "Address them before the demo recording if relevant."
        )
        return 0
    print("\nReady to deploy. All checks green.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pre-flight sanity check.")
    p.add_argument(
        "--no-anthropic",
        action="store_true",
        help="Skip the live Anthropic API roundtrip (saves a fraction of a cent).",
    )
    args = p.parse_args(argv)
    return run(skip_anthropic=args.no_anthropic)


if __name__ == "__main__":
    raise SystemExit(main())
