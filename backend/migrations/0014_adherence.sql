-- Wave 3 stage 2 — strict adherence.
--
-- "Strict" means: per-second comparison of planned aux (from shift_segments)
-- vs actual aux (from agent_aux_events). No interval rollup buried in the
-- ingest path — adherence is computed at query time so the model can be
-- tweaked without re-ingesting.
--
-- Planned source: shift_segments.segment_type ∈ (work, break, lunch, training, off).
-- Actual source: agent_aux_events.aux_code ∈ (available, on_call, acw,
--                break, lunch, training, meeting, coaching, system, offline).
--
-- Match rule (used by Wave 3 tools, not enforced in schema):
--   planned=work     → actual in {available, on_call, acw}            adherent
--   planned=break    → actual=break                                    adherent
--   planned=lunch    → actual=lunch                                    adherent
--   planned=training → actual in {training, meeting, coaching}         adherent
--   planned=off      → actual=offline                                  adherent (or N/A)
--   else                                                               OUT OF ADHERENCE

CREATE TABLE IF NOT EXISTS agent_aux_events (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ,                       -- NULL if currently active
    aux_code        TEXT NOT NULL,                     -- see comment above
    reason_code     TEXT,                              -- optional free-form
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_ts IS NULL OR end_ts > start_ts)
);

CREATE INDEX IF NOT EXISTS ix_aux_events_agent_time
    ON agent_aux_events (agent_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_aux_events_open
    ON agent_aux_events (agent_id) WHERE end_ts IS NULL;
CREATE INDEX IF NOT EXISTS ix_aux_events_start
    ON agent_aux_events (start_ts);

-- Exception log — a finished aux event that violated plan, plus the
-- categorization (late_start, missed_break, early_out, unplanned_aux,
-- extended_break). Populated by a backend job (or seed data); the
-- get_exceptions tool reads it directly so it doesn't have to re-derive
-- causation each call.
CREATE TABLE IF NOT EXISTS adherence_exceptions (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ NOT NULL,
    duration_seconds INT NOT NULL,
    exception_type  TEXT NOT NULL,                     -- late_start | missed_break | early_out | unplanned_aux | extended_break | no_show
    planned_state   TEXT,                              -- what shift_segments said they should be doing
    actual_state    TEXT,                              -- aux_code they were in
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_ts > start_ts)
);

CREATE INDEX IF NOT EXISTS ix_exceptions_agent_day
    ON adherence_exceptions (agent_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_exceptions_day_type
    ON adherence_exceptions (start_ts, exception_type);
