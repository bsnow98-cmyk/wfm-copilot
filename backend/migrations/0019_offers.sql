-- Surface #2 (EXECUTION_ROADMAP.md) — OT/VTO offer publishing.
-- Publish-only v1: post an offer to a recommended target group. No agent
-- accept/decline loop yet. Same preview -> apply -> undo (retract) shape as
-- Surface #1, but an offer is a *create* (no existing row to version), so the
-- offers row IS its own audit record: status open -> retracted, with a 24h
-- retract window.

CREATE TABLE IF NOT EXISTS offers (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('ot','vto')),
    schedule_id     BIGINT REFERENCES schedules(id) ON DELETE SET NULL,
    target_date     DATE NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    targets         JSONB NOT NULL,        -- [{employee_id, full_name}, ...] snapshot
    slots           INT NOT NULL,          -- how many the offer seeks to fill
    policy          TEXT,
    message         TEXT,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','retracted')),
    published_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_by    TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    undo_window_ends_at TIMESTAMPTZ NOT NULL,
    retracted_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_offers_status ON offers (status, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_offers_date ON offers (target_date);

-- Idempotency: a consumed offer token points at the offers row it created.
ALTER TABLE chat_apply_tokens
    ADD COLUMN IF NOT EXISTS consumed_offer_id BIGINT
        REFERENCES offers(id) ON DELETE SET NULL;
