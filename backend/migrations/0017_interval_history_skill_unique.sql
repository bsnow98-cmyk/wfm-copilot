-- Phase 8 stage 2 (deferred from 0012) — let interval_history carry per-skill rows.
--
-- 0012 added interval_history.skill_id but left the UNIQUE constraint at
-- (queue, channel, interval_start). That blocks per-skill history: three skill
-- rows for the same (queue, channel, interval_start) collide. The per-skill
-- forecast path (ForecastService(skill_id=...), get_skills_coverage,
-- recommend_skill_rebalance, explain_substitution) therefore never had data to
-- read. This migration rewrites the constraint to include skill_id so aggregate
-- rows (skill_id IS NULL) and per-skill rows coexist.
--
-- NULLS NOT DISTINCT (Postgres 15+) makes the aggregate row's NULL skill_id
-- still collision-checked against itself — same approach 0012 used for
-- schedule_coverage. Image is postgres:16 locally; prod (Render) already ran
-- 0012's NULLS NOT DISTINCT index, so it is 15+.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS.
-- Runs on every app boot via db_migrate.run_migrations — harmless to re-apply.
--
-- RIPPLE: this removes the 3-column unique, so any `ON CONFLICT (queue, channel,
-- interval_start)` against interval_history must move to the 4-column target.
-- Updated in scripts/generate_synthetic_data.py (_seed_db / _seed_db_per_skill)
-- and scripts/seed_prod_real.py. The auto-named 3-col constraint is
-- interval_history_queue_channel_interval_start_key (inline UNIQUE in 0001).

ALTER TABLE interval_history
    DROP CONSTRAINT IF EXISTS interval_history_queue_channel_interval_start_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_interval_history_queue_channel_start_skill
    ON interval_history (queue, channel, interval_start, skill_id) NULLS NOT DISTINCT;
