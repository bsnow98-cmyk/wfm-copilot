-- RBAC foundation — real identities + roles (unblocks Surface #6).
-- Identity rides the existing Basic-Auth gate: the password stays shared (bounds
-- Anthropic spend), but the *username* field — previously ignored — is now the
-- identity, resolved against this table for a role + display name. Writes record
-- applied_by/decided_by/published_by = <username> instead of the literal 'demo'.
--
-- No credentials column: the shared password is the only secret. This is
-- identity + authorization for a portfolio demo, not a full auth system.

CREATE TABLE IF NOT EXISTS users (
    id           BIGSERIAL PRIMARY KEY,
    username     TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('admin', 'wfm_manager', 'analyst', 'viewer')),
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed demo identities (idempotent). 'demo' is kept as an admin so any
-- pre-RBAC client (proxy default username 'demo') keeps full access, and
-- 'guest' is the fallback identity for an unknown/empty username (read-only).
INSERT INTO users (username, display_name, role) VALUES
    ('demo',   'Demo Admin',  'admin'),
    ('admin',  'Admin',       'admin'),
    ('jchen',  'J. Chen',     'wfm_manager'),
    ('apatel', 'A. Patel',    'analyst'),
    ('guest',  'Guest',       'viewer')
ON CONFLICT (username) DO NOTHING;
