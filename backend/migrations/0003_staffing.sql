-- Phase 3 — staffing requirements derived from a forecast via Erlang C.
-- One staffing_requirements row per (forecast_run, parameter set). Re-run with
-- different SL targets / shrinkage assumptions = new rows. The forecast itself
-- is unchanged.
-- Idempotent (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS staffing_requirements (
    id                       BIGSERIAL PRIMARY KEY,
    forecast_run_id          BIGINT NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
    service_level_target     NUMERIC(5,4) NOT NULL,    -- e.g. 0.8000 = 80%
    target_answer_seconds    INT          NOT NULL,    -- e.g. 20
    shrinkage                NUMERIC(5,4) NOT NULL DEFAULT 0.30,  -- 30% by default
    interval_minutes         INT          NOT NULL DEFAULT 30,
    notes                    TEXT,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (forecast_run_id, service_level_target, target_answer_seconds, shrinkage)
);

CREATE INDEX IF NOT EXISTS ix_staffing_requirements_forecast
    ON staffing_requirements (forecast_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS staffing_requirement_intervals (
    staffing_id              BIGINT NOT NULL REFERENCES staffing_requirements(id) ON DELETE CASCADE,
    interval_start           TIMESTAMPTZ NOT NULL,
    forecast_offered         NUMERIC(10,2) NOT NULL,    -- echoed from forecast for convenience
    forecast_aht_seconds     NUMERIC(10,2) NOT NULL,
    required_agents_raw      INT           NOT NULL,    -- before shrinkage
    required_agents          INT           NOT NULL,    -- after shrinkage (this is what you schedule to)
    expected_service_level   NUMERIC(5,4),              -- SL the raw count actually achieves
    expected_asa_seconds     NUMERIC(8,2),
    occupancy                NUMERIC(5,4),
    PRIMARY KEY (staffing_id, interval_start)
);

CREATE INDEX IF NOT EXISTS ix_staffing_intervals_start
    ON staffing_requirement_intervals (staffing_id, interval_start);
