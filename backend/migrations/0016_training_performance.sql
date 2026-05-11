-- Wave 4 stage — training, certifications, performance signals.
-- These feed: get_training_calendar, check_training_impact,
--             recommend_coaching_slot, get_skill_certifications,
--             get_class_progress, get_agent_performance, rank_agents,
--             get_team_kpis, get_attrition_risk, get_new_hire_progress.

CREATE TABLE IF NOT EXISTS training_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,                 -- new_hire_class | skill_cert | coaching | team_meeting | system_training
    title           TEXT NOT NULL,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ NOT NULL,
    required        BOOLEAN NOT NULL DEFAULT FALSE,
    target_skill_id BIGINT REFERENCES skills(id),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_ts > start_ts)
);

CREATE INDEX IF NOT EXISTS ix_training_time
    ON training_events (start_ts, event_type);

CREATE TABLE IF NOT EXISTS training_attendees (
    training_event_id   BIGINT NOT NULL REFERENCES training_events(id) ON DELETE CASCADE,
    agent_id            BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    attended            BOOLEAN,                   -- NULL = not yet, TRUE/FALSE = recorded
    PRIMARY KEY (training_event_id, agent_id)
);

CREATE TABLE IF NOT EXISTS agent_certifications (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    skill_id        BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    level           INT NOT NULL,                  -- 1..5, matches agent_skills.proficiency scale
    certified_at    TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ,
    certifier       TEXT,
    UNIQUE (agent_id, skill_id, certified_at)
);

CREATE TABLE IF NOT EXISTS agent_qa_scores (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    evaluated_at    TIMESTAMPTZ NOT NULL,
    score           NUMERIC(5,2) NOT NULL,         -- 0..100
    sample_size     INT NOT NULL DEFAULT 1,
    reviewer        TEXT,
    note            TEXT,
    CHECK (score >= 0 AND score <= 100)
);

CREATE INDEX IF NOT EXISTS ix_qa_agent_time
    ON agent_qa_scores (agent_id, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS new_hire_classes (
    id              BIGSERIAL PRIMARY KEY,
    class_name      TEXT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,                 -- expected graduation
    target_skill_id BIGINT REFERENCES skills(id),
    target_size     INT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'in_class', -- in_class | graduated | cancelled
    notes           TEXT,
    CHECK (end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS new_hire_progress (
    id              BIGSERIAL PRIMARY KEY,
    class_id        BIGINT NOT NULL REFERENCES new_hire_classes(id) ON DELETE CASCADE,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    evaluated_at    TIMESTAMPTZ NOT NULL,
    nesting_week    INT,                           -- 1..N, after class
    qa_score        NUMERIC(5,2),
    aht_seconds     NUMERIC(8,2),
    adherence_pct   NUMERIC(5,4),                  -- 0..1
    status          TEXT NOT NULL DEFAULT 'on_track', -- on_track | watch | at_risk | washed_out
    UNIQUE (class_id, agent_id, evaluated_at)
);

CREATE INDEX IF NOT EXISTS ix_new_hire_progress_class
    ON new_hire_progress (class_id, evaluated_at DESC);
