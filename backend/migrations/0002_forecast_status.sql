-- Phase 2 — track forecast run lifecycle.
-- Idempotent so the app-startup migration runner can apply this safely
-- whether or not it's been applied before.

ALTER TABLE forecast_runs
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
    -- pending | running | completed | failed

ALTER TABLE forecast_runs
    ADD COLUMN IF NOT EXISTS error_message TEXT;

ALTER TABLE forecast_runs
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

ALTER TABLE forecast_runs
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_forecast_runs_status_created
    ON forecast_runs (status, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_forecast_runs_queue_created
    ON forecast_runs (queue, created_at DESC);
