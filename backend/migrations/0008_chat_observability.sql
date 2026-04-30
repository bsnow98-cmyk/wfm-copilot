-- Phase 6 follow-up — chat observability.
-- One row per tool_use block invoked in the chat loop. Eight fields per the
-- TODOS spec. Idempotent (CREATE TABLE IF NOT EXISTS).
--
-- Why: chat is the new operational risk surface. Without these logs you can't
-- answer "why did chat give a bad answer last Tuesday?"

CREATE TABLE IF NOT EXISTS chat_tool_calls (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_msg_id     UUID REFERENCES chat_messages(id) ON DELETE SET NULL,
    tool_name       TEXT NOT NULL,
    args_json       JSONB NOT NULL,
    latency_ms      INT  NOT NULL,
    error           TEXT,                      -- NULL = success
    tokens_in       INT,                       -- per the model iteration that emitted this tool_use
    tokens_out      INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_chat_tool_calls_conv_created
    ON chat_tool_calls (conversation_id, created_at);

CREATE INDEX IF NOT EXISTS ix_chat_tool_calls_tool
    ON chat_tool_calls (tool_name, created_at DESC);
