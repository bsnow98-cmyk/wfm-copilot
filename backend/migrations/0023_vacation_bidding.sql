-- Vacation bidding (VACATION_BIDDING.md) — seniority-greedy weekly bid award.
-- The award writes approved leave_requests + pto_ledger holds (it's Surface #1
-- batched), so there's no separate "awarded vacation" store. These tables model
-- the round, per-week capacity, the ranked bids, and the reversible award audit.
--
-- Weeks are Monday-aligned (CHECK DOW=1). Awards span Mon–Fri (5 workdays = 40h
-- PTO, matching leave_decision.leave_pto_hours) — see design doc §"award window".

CREATE TABLE IF NOT EXISTS bid_rounds (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','open','closed','awarded','cancelled')),
    bids_open_at  TIMESTAMPTZ NOT NULL,
    bids_close_at TIMESTAMPTZ NOT NULL,
    season_start  DATE NOT NULL,
    season_end    DATE NOT NULL,
    max_weeks_per_agent INT NOT NULL DEFAULT 2,
    awarded_at    TIMESTAMPTZ,
    published_at  TIMESTAMPTZ,            -- set by the separate publish step (notify decoupled)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (EXTRACT(DOW FROM season_start) = 1 AND EXTRACT(DOW FROM season_end) = 1)
);

CREATE TABLE IF NOT EXISTS bid_week_capacity (
    round_id      BIGINT NOT NULL REFERENCES bid_rounds(id) ON DELETE CASCADE,
    week_start    DATE NOT NULL,
    slots         INT NOT NULL CHECK (slots >= 0),
    PRIMARY KEY (round_id, week_start),
    CHECK (EXTRACT(DOW FROM week_start) = 1)
);

CREATE TABLE IF NOT EXISTS vacation_bids (
    id            BIGSERIAL PRIMARY KEY,
    round_id      BIGINT NOT NULL REFERENCES bid_rounds(id) ON DELETE CASCADE,
    agent_id      BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    week_start    DATE NOT NULL,
    rank          INT NOT NULL CHECK (rank >= 1),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (round_id, agent_id, week_start),
    UNIQUE (round_id, agent_id, rank),
    CHECK (EXTRACT(DOW FROM week_start) = 1)
);
CREATE INDEX IF NOT EXISTS ix_vacation_bids_round ON vacation_bids (round_id, agent_id, rank);

-- Award audit. Persists BOTH awards and the full denial trace (the "why you
-- lost" record), so it's durable for a grievance review, not preview-only.
CREATE TABLE IF NOT EXISTS vacation_award_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    round_id      BIGINT NOT NULL REFERENCES bid_rounds(id) ON DELETE CASCADE,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by    TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    awards        JSONB NOT NULL,   -- [{agent_id, employee_id, full_name, seniority_rank, week_start, leave_request_id, ledger_event_id, awarded_pref_rank}]
    denials       JSONB NOT NULL,   -- [{agent_id, employee_id, seniority_rank, week_start, pref_rank, reason}]
    summary       JSONB NOT NULL,   -- {n_awarded, n_agents, n_zero_win, weeks_at_capacity}
    undo_window_ends_at TIMESTAMPTZ NOT NULL,
    undone_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_vacation_award_round ON vacation_award_log (round_id, applied_at DESC);

ALTER TABLE chat_apply_tokens
    ADD COLUMN IF NOT EXISTS consumed_vacation_log_id UUID
        REFERENCES vacation_award_log(id) ON DELETE SET NULL;
