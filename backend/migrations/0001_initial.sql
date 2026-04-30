-- WFM Copilot — initial schema
-- Runs automatically on first Postgres boot (mounted via docker-entrypoint-initdb.d).
-- Idempotent: uses CREATE TABLE IF NOT EXISTS so re-runs don't blow up.

-- ============================================================
-- INTERVAL HISTORY
-- The bedrock table. Every WFM forecast and report comes from here.
-- One row per (queue, channel, interval_start). 30-min intervals are typical
-- but the schema doesn't enforce that — store whatever cadence your ACD emits.
-- ============================================================
CREATE TABLE IF NOT EXISTS interval_history (
    id              BIGSERIAL PRIMARY KEY,
    queue           TEXT        NOT NULL,
    channel         TEXT        NOT NULL DEFAULT 'voice',  -- voice | chat | email | sms
    interval_start  TIMESTAMPTZ NOT NULL,
    interval_minutes INT        NOT NULL DEFAULT 30,
    offered         INT         NOT NULL DEFAULT 0,        -- total contacts offered
    handled         INT         NOT NULL DEFAULT 0,        -- contacts actually handled
    abandoned       INT         NOT NULL DEFAULT 0,
    aht_seconds     NUMERIC(10,2) NOT NULL DEFAULT 0,      -- average handle time
    asa_seconds     NUMERIC(10,2),                         -- average speed of answer
    service_level   NUMERIC(5,4),                          -- e.g. 0.8000 = 80%
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (queue, channel, interval_start)
);

CREATE INDEX IF NOT EXISTS ix_interval_history_queue_start
    ON interval_history (queue, interval_start);
CREATE INDEX IF NOT EXISTS ix_interval_history_start
    ON interval_history (interval_start);

-- ============================================================
-- AGENTS & SKILLS
-- ============================================================
CREATE TABLE IF NOT EXISTS agents (
    id              BIGSERIAL PRIMARY KEY,
    employee_id     TEXT UNIQUE NOT NULL,           -- external HRIS id
    full_name       TEXT NOT NULL,
    email           TEXT,
    hire_date       DATE,
    contracted_hours_per_week NUMERIC(5,2) DEFAULT 40,
    timezone        TEXT NOT NULL DEFAULT 'America/New_York',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS skills (
    id      BIGSERIAL PRIMARY KEY,
    name    TEXT UNIQUE NOT NULL,                   -- e.g. 'sales_en', 'support_es'
    description TEXT
);

CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id    BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    skill_id    BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    proficiency INT    NOT NULL DEFAULT 1,          -- 1 (learner) .. 5 (expert)
    PRIMARY KEY (agent_id, skill_id)
);

-- ============================================================
-- FORECASTS
-- A forecast run produces many forecast_intervals rows.
-- ============================================================
CREATE TABLE IF NOT EXISTS forecast_runs (
    id          BIGSERIAL PRIMARY KEY,
    queue       TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'voice',
    model_name  TEXT NOT NULL,                      -- e.g. 'auto_arima', 'lgbm'
    horizon_start TIMESTAMPTZ NOT NULL,
    horizon_end   TIMESTAMPTZ NOT NULL,
    mape        NUMERIC(8,4),                       -- backtest score
    wape        NUMERIC(8,4),
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS forecast_intervals (
    forecast_run_id BIGINT NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
    interval_start  TIMESTAMPTZ NOT NULL,
    forecast_offered NUMERIC(10,2) NOT NULL,
    forecast_aht_seconds NUMERIC(10,2),
    PRIMARY KEY (forecast_run_id, interval_start)
);

-- ============================================================
-- SCHEDULES
-- A schedule covers a date range and is composed of shift segments per agent.
-- ============================================================
CREATE TABLE IF NOT EXISTS schedules (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',      -- draft | published | archived
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shift_segments (
    id          BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    agent_id    BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    segment_type TEXT NOT NULL,                     -- work | break | lunch | training | off
    start_time  TIMESTAMPTZ NOT NULL,
    end_time    TIMESTAMPTZ NOT NULL,
    skill_id    BIGINT REFERENCES skills(id),       -- which queue they're working
    CHECK (end_time > start_time)
);

CREATE INDEX IF NOT EXISTS ix_shift_segments_agent_time
    ON shift_segments (agent_id, start_time);
CREATE INDEX IF NOT EXISTS ix_shift_segments_schedule
    ON shift_segments (schedule_id);
