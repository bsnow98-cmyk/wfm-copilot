-- Phase 4 — link schedules to a staffing requirement, add solver metadata,
-- and a coverage cache that compares scheduled vs. required per interval.
-- Idempotent (ALTER ... IF NOT EXISTS, CREATE ... IF NOT EXISTS).

-- 1) Schedules now know which staffing demand they cover and how the solve went.
ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS staffing_id BIGINT REFERENCES staffing_requirements(id);

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS solver_status TEXT;
    -- one of: 'pending', 'running', 'optimal', 'feasible', 'infeasible', 'failed'

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS solver_runtime_seconds NUMERIC(10,2);

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS objective_value NUMERIC(14,2);
    -- total interval-shortage in agent-intervals (lower is better; 0 = perfectly staffed)

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS total_understaffed_intervals INT;
    -- count of intervals where coverage < required

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS error_message TEXT;

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_schedules_staffing
    ON schedules (staffing_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_schedules_status
    ON schedules (solver_status, created_at DESC);

-- 2) Coverage cache: derived from shift_segments but stored for fast reporting.
CREATE TABLE IF NOT EXISTS schedule_coverage (
    schedule_id      BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    interval_start   TIMESTAMPTZ NOT NULL,
    required_agents  INT NOT NULL DEFAULT 0,
    scheduled_agents INT NOT NULL DEFAULT 0,
    shortage         INT NOT NULL DEFAULT 0,    -- max(0, required - scheduled)
    PRIMARY KEY (schedule_id, interval_start)
);

CREATE INDEX IF NOT EXISTS ix_schedule_coverage_interval
    ON schedule_coverage (schedule_id, interval_start);
