"""
FastAPI application entry point.

Run via uvicorn:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import BasicAuthMiddleware
from app.config import get_settings
from app.db import engine
from app.db_migrate import run_migrations
from app.routers import (
    anomalies,
    chat,
    forecasts,
    health,
    ingest,
    intraday,
    leave_decisions,
    notifications,
    offers,
    schedule_changes,
    schedules,
    skills,
    staffing,
)

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("wfm")

if not settings.anthropic_api_key:
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. The Phase 6 chat copilot requires it. "
        "Set it in backend/.env (see backend/.env.example) and restart."
    )

app = FastAPI(
    title="WFM Copilot",
    description="AI-native open-source workforce management for contact centers.",
    version="0.6.0",
)

# CORS — browsers REJECT a response that combines allow_credentials=True
# with allow_origins=["*"], so we drop credentials. The frontend uses
# Authorization headers explicitly (not cookies), so this works without
# credentials mode. If we ever need cookie-based auth, switch to a
# concrete origin allowlist instead of toggling credentials back on.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single-password gate (Phase 6). No-op when WFM_DEMO_PASSWORD is unset.
app.add_middleware(BasicAuthMiddleware, password=settings.wfm_demo_password)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(forecasts.router)
app.include_router(staffing.router)
app.include_router(schedules.router)
app.include_router(schedule_changes.router)
app.include_router(leave_decisions.router)
app.include_router(offers.router)
app.include_router(skills.router)
app.include_router(anomalies.router)
app.include_router(chat.router)
app.include_router(intraday.router)
app.include_router(notifications.router)


@app.on_event("startup")
async def on_startup() -> None:
    log.info("WFM Copilot API starting on %s:%s", settings.api_host, settings.api_port)
    # Apply any pending DB migrations. All SQL files are idempotent so this
    # is safe on every boot.
    try:
        applied = run_migrations(engine)
        log.info("Migrations applied: %s", applied)
    except Exception:
        log.exception("Migration runner failed — API will still start, "
                      "but the DB may be on an older schema.")

    # Self-heal the demo sim clock: it advances with real time, so after
    # ~a week it drifts past the seeded shift window and the live ticker
    # reads into the void. Cheap no-op when the clock is in range.
    try:
        from app.db import SessionLocal
        from app.services.realtime_clock import ensure_sim_anchor_in_window

        with SessionLocal() as db:
            if ensure_sim_anchor_in_window(db):
                log.info("Sim clock re-anchored into the seeded data window.")
    except Exception:
        log.exception("Sim-anchor check failed — API will still start.")

    # Fail-fast for jobs orphaned by a restart: BackgroundTasks die with the
    # process, and a row stuck at pending/running spins polling clients
    # forever. Only rows past the staleness threshold are touched.
    try:
        from app.db import SessionLocal
        from app.services.job_sweeper import sweep_orphaned_jobs

        with SessionLocal() as db:
            counts = sweep_orphaned_jobs(db)
            if any(counts.values()):
                log.info("Orphaned-job sweep: %s", counts)
    except Exception:
        log.exception("Orphaned-job sweep failed — API will still start.")
