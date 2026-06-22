-- Surface #1 (EXECUTION_ROADMAP.md) — leave-approval write actions.
-- Mirrors cherry-pick D (schedule_change_log + chat_apply_tokens) for the
-- leave-decision domain: preview -> apply, append-only audit, 24h undo.
--
-- Two parts:
--   1. Generalize chat_apply_tokens so a non-schedule surface can mint tokens
--      (the roadmap's "token store reused as-is" — we add target_kind + a
--      leave-specific consumed_log reference rather than overloading the
--      schedule FK columns, which keeps the shipped schedule path untouched).
--   2. The leave_decision_log audit table.

-- ---------------------------------------------------------------------------
-- 1. Generalize the apply-token store.
-- ---------------------------------------------------------------------------
-- schedule_id / schedule_version are meaningless for a leave decision; make
-- them optional. Existing schedule tokens keep populating them as before.
ALTER TABLE chat_apply_tokens ALTER COLUMN schedule_id DROP NOT NULL;
ALTER TABLE chat_apply_tokens ALTER COLUMN schedule_version DROP NOT NULL;

-- target_kind discriminates which surface a token authorizes. Default keeps
-- every pre-existing row valid as a schedule token.
ALTER TABLE chat_apply_tokens
    ADD COLUMN IF NOT EXISTS target_kind TEXT NOT NULL DEFAULT 'schedule';

-- ---------------------------------------------------------------------------
-- 2. Leave decision audit log (append-only, 24h undo window).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leave_decision_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by      TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    request_id      BIGINT NOT NULL REFERENCES leave_requests(id) ON DELETE CASCADE,
    decision        TEXT NOT NULL CHECK (decision IN ('approve','deny')),
    before_state    JSONB NOT NULL,   -- {status, decided_at, decided_by, decision_note}
    after_state     JSONB NOT NULL,
    ledger_event_id BIGINT REFERENCES pto_ledger(id) ON DELETE SET NULL,  -- 'use' row on approve
    undo_window_ends_at TIMESTAMPTZ NOT NULL,
    undone_at       TIMESTAMPTZ,
    undone_by_log_id UUID REFERENCES leave_decision_log(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_leave_decision_log_applied
    ON leave_decision_log (applied_at DESC);
CREATE INDEX IF NOT EXISTS ix_leave_decision_log_request
    ON leave_decision_log (request_id);

-- The token store needs to reference the leave decision it consumed (the
-- existing consumed_log_id FK points at schedule_change_log, the wrong table).
ALTER TABLE chat_apply_tokens
    ADD COLUMN IF NOT EXISTS consumed_leave_log_id UUID
        REFERENCES leave_decision_log(id) ON DELETE SET NULL;
