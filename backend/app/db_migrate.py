"""
App-startup migration runner.

Why this exists: docker-entrypoint-initdb.d only runs SQL files on the FIRST
boot of an empty Postgres volume. After that, new migrations are silently
ignored. This module fixes that — on every API startup we walk the
backend/migrations/*.sql directory in alphabetical order and execute each file.

All migration SQL must be idempotent (use IF NOT EXISTS / IF EXISTS, or guard
with DO blocks). With idempotency, re-execution is harmless, so we don't even
need a "schema_migrations" tracking table for now. We log which files ran.

If you outgrow this (need rollbacks, complex multi-step changes, or multiple
API instances racing on startup), swap to Alembic.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("wfm.migrate")

# Resolve to backend/migrations regardless of where the app is launched.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def run_migrations(engine: Engine) -> list[str]:
    """Execute all *.sql files in backend/migrations/ in alphabetical order.

    Returns the list of files that ran successfully.
    """
    if not MIGRATIONS_DIR.is_dir():
        log.warning("Migrations dir not found at %s — skipping.", MIGRATIONS_DIR)
        return []

    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        log.info("No migration files found in %s.", MIGRATIONS_DIR)
        return []

    applied: list[str] = []
    with engine.begin() as conn:
        for sql_path in sql_files:
            log.info("Applying migration: %s", sql_path.name)
            sql = sql_path.read_text(encoding="utf-8")
            # `text()` + `exec_driver_sql` would also work; this is simplest.
            # Note: psycopg can run multi-statement strings.
            conn.exec_driver_sql(sql)
            applied.append(sql_path.name)
    log.info("Migrations complete (%d files).", len(applied))
    return applied
