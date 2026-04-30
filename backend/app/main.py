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
    notifications,
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

# Permissive CORS in dev. Tighten before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
app.include_router(skills.router)
app.include_router(anomalies.router)
app.include_router(chat.router)
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
