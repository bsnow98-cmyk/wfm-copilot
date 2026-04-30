-- Cherry-pick D — single-use apply tokens (decision D-5/D-6).
-- A preview tool mints a token; the apply endpoint consumes it inside the
-- same transaction as the write. Duplicate request finds a row with
-- consumed_at != NULL and returns the original log_id (idempotency).
-- TTL of 5 minutes is enforced at consume time via expires_at.

CREATE TABLE IF NOT EXISTS chat_apply_tokens (
    token           TEXT PRIMARY KEY,
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    -- The preview the token authorizes — locked at issue time so the apply
    -- endpoint can't be coerced into writing something the user didn't see.
    schedule_id     BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    schedule_version BIGINT NOT NULL,
    change_set      JSONB NOT NULL,
    -- Provenance for the audit log.
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    user_msg_id     UUID REFERENCES chat_messages(id)      ON DELETE SET NULL,
    -- Consumption. NULL = unused.
    consumed_at     TIMESTAMPTZ,
    consumed_log_id UUID REFERENCES schedule_change_log(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_chat_apply_tokens_expiry
    ON chat_apply_tokens (expires_at) WHERE consumed_at IS NULL;
