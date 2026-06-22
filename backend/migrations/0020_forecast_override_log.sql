-- Surface #4 (EXECUTION_ROADMAP.md) — analyst forecast overrides.
-- Pin a single forecast interval's offered-volume to an analyst value. The
-- mutation is an UPDATE on forecast_intervals; this log is the append-only
-- audit record (before/after value, 24h undo). Downstream staffing recompute
-- is a *job* (deferred — pairs with Surface #5 / the async solver); v1 pins the
-- value + audits, and the preview says so.

CREATE TABLE IF NOT EXISTS forecast_override_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by      TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    forecast_run_id BIGINT NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
    interval_start  TIMESTAMPTZ NOT NULL,
    before_value    NUMERIC(10,2) NOT NULL,
    after_value     NUMERIC(10,2) NOT NULL,
    undo_window_ends_at TIMESTAMPTZ NOT NULL,
    undone_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_forecast_override_applied
    ON forecast_override_log (applied_at DESC);
CREATE INDEX IF NOT EXISTS ix_forecast_override_run_interval
    ON forecast_override_log (forecast_run_id, interval_start);

-- Idempotency: a consumed forecast-override token points at its log row.
ALTER TABLE chat_apply_tokens
    ADD COLUMN IF NOT EXISTS consumed_forecast_log_id UUID
        REFERENCES forecast_override_log(id) ON DELETE SET NULL;
