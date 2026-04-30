-- Cherry-pick D, decision D-3 — in-app notification feed.
-- recipient = NULL is the v1 "global feed" (single-tenant). When RBAC lands
-- this column gets populated and the unread index already filters correctly.

CREATE TABLE IF NOT EXISTS notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_at         TIMESTAMPTZ,
    recipient       TEXT,
    category        TEXT NOT NULL,
    source          TEXT NOT NULL,
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    payload         JSONB NOT NULL
);

-- Unread feed query is the hot path; index it.
CREATE INDEX IF NOT EXISTS ix_notifications_recipient_unread
    ON notifications (recipient, created_at DESC) WHERE read_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_notifications_created
    ON notifications (created_at DESC);
