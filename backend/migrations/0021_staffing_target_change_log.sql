-- Surface #5 (EXECUTION_ROADMAP.md) — change an SL/ASA staffing target.
-- Unlike #1–#4 this is NOT a sync write: changing a target requires recomputing
-- staffing (Erlang C over the forecast horizon), which runs as a background job
-- (same FastAPI BackgroundTasks + status-column pattern as the schedule solver).
--
-- v1 recomputes the *current* staffing scenario IN PLACE (the single
-- staffing_requirements row for the queue's latest forecast). The dashboard
-- tools join staffing_requirement_intervals without a staffing_id predicate, so
-- spawning a parallel scenario would fan those joins out — recomputing in place
-- preserves the single-scenario invariant. Per-window targets = a v2 schema.

CREATE TABLE IF NOT EXISTS staffing_target_change_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by      TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    staffing_id     BIGINT NOT NULL REFERENCES staffing_requirements(id) ON DELETE CASCADE,
    forecast_run_id BIGINT,
    before_targets  JSONB NOT NULL,  -- {sl, target_answer_seconds, target_asa_seconds, shrinkage}
    after_targets   JSONB NOT NULL,
    -- Async recompute job state (mirrors schedules.solver_status).
    recompute_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (recompute_status IN ('pending','running','completed','failed')),
    recompute_error TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    peak_required_before INT,
    peak_required_after  INT,
    undo_window_ends_at TIMESTAMPTZ NOT NULL,
    undone_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_staffing_target_applied
    ON staffing_target_change_log (applied_at DESC);
CREATE INDEX IF NOT EXISTS ix_staffing_target_status
    ON staffing_target_change_log (recompute_status)
    WHERE recompute_status IN ('pending', 'running');

-- Idempotency: a consumed staffing-target token points at its log row.
ALTER TABLE chat_apply_tokens
    ADD COLUMN IF NOT EXISTS consumed_staffing_log_id UUID
        REFERENCES staffing_target_change_log(id) ON DELETE SET NULL;
