-- Wave 3 stage 3 — PTO and leave management.
-- check_leave_feasibility joins leave_requests against schedule_coverage
-- to answer "if we approve this, what's the SL impact on the affected days?"

CREATE TABLE IF NOT EXISTS pto_ledger (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    event_ts        TIMESTAMPTZ NOT NULL,
    event_type      TEXT NOT NULL,                 -- accrual | use | adjust | reset
    hours           NUMERIC(8,2) NOT NULL,         -- positive = added, negative = consumed
    balance_after   NUMERIC(8,2) NOT NULL,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_pto_agent_time
    ON pto_ledger (agent_id, event_ts DESC);

CREATE TABLE IF NOT EXISTS leave_requests (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ NOT NULL,
    leave_type      TEXT NOT NULL,                 -- PTO | sick | unpaid | swap | bereavement | jury
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | approved | denied | cancelled
    reason          TEXT,
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    decision_note   TEXT,
    CHECK (end_ts > start_ts),
    CHECK (status IN ('pending','approved','denied','cancelled'))
);

CREATE INDEX IF NOT EXISTS ix_leave_status_start
    ON leave_requests (status, start_ts);
CREATE INDEX IF NOT EXISTS ix_leave_agent
    ON leave_requests (agent_id, start_ts DESC);
