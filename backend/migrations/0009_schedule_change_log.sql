-- Cherry-pick D — append-only audit log of schedule mutations.
-- Decision D-1 (24h undo) is enforced via undo_window_ends_at, NOT a bg job —
-- the undo endpoint just refuses if NOW() > undo_window_ends_at. That keeps
-- old rows around for analytics while still expiring undoability.
-- Decision D-2: applied_by stores the literal "demo" string in v1.

CREATE TABLE IF NOT EXISTS schedule_change_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by      TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    user_msg_id     UUID REFERENCES chat_messages(id)      ON DELETE SET NULL,
    schedule_id     BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    change_set      JSONB NOT NULL,
    before_state    JSONB NOT NULL,
    after_state     JSONB NOT NULL,
    undo_window_ends_at  TIMESTAMPTZ NOT NULL,
    undone_at       TIMESTAMPTZ,
    undone_by_log_id     UUID REFERENCES schedule_change_log(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_schedule_change_log_applied
    ON schedule_change_log (applied_at DESC);

CREATE INDEX IF NOT EXISTS ix_schedule_change_log_conv
    ON schedule_change_log (conversation_id, applied_at DESC);

CREATE INDEX IF NOT EXISTS ix_schedule_change_log_undoable
    ON schedule_change_log (undo_window_ends_at) WHERE undone_at IS NULL;
