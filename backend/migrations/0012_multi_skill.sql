-- Phase 8 Stage 1 — multi-skill schema additions.
-- Decisions in docs/designs/MULTI_SKILL_SCHEDULING.md.
--
-- Backward-compat principle: every existing row stays valid with skill_id = NULL.
-- Pre-Phase-8 forecasts and schedules continue to work; new ones populate the
-- column. Pre-existing API endpoints behave identically when skill_id is omitted.

-- 1) interval_history learns about skills.
ALTER TABLE interval_history
    ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);

CREATE INDEX IF NOT EXISTS ix_interval_history_skill
    ON interval_history (skill_id, interval_start) WHERE skill_id IS NOT NULL;

-- 2) forecast_runs gain a skill axis (NULL = aggregate, what Phase 2 produced).
ALTER TABLE forecast_runs
    ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);

CREATE INDEX IF NOT EXISTS ix_forecast_runs_skill
    ON forecast_runs (skill_id, created_at DESC) WHERE skill_id IS NOT NULL;

-- 3) staffing_requirements likewise.
ALTER TABLE staffing_requirements
    ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);

-- 4) schedule_coverage gains skill_id and the PK is rewritten to include it.
--    Old rows have skill_id IS NULL; new per-skill rows can coexist.
--    Postgres 15+ supports NULLS NOT DISTINCT on unique indexes — uses the
--    image set in docker-compose (postgres:16). If you change the image,
--    verify support before running this migration.
ALTER TABLE schedule_coverage
    ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);

ALTER TABLE schedule_coverage
    DROP CONSTRAINT IF EXISTS schedule_coverage_pkey;

CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_coverage_with_skill
    ON schedule_coverage (schedule_id, interval_start, skill_id) NULLS NOT DISTINCT;

-- 5) shift_segments.skill_id already exists from Phase 1 (migration 0001).
--    Phase 8 stage 3 (CP-SAT) starts populating it. No schema change here.

-- 6) Note: agents and agent_skills tables already exist (Phase 1). Phase 8
--    just starts using them properly via the updated seed_agents script.
