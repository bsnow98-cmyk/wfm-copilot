-- Phase 5 — anomaly detection.
-- Schema locked in Decisions.md. Idempotent.
--
-- The id is SHA256 truncated to 16 hex chars of (date|queue|interval_start|category).
-- The UNIQUE constraint is load-bearing: hash collisions would silently mis-merge
-- records from different (queue, category, interval) tuples. INSERT ON CONFLICT
-- DO NOTHING in the service surfaces a collision via the affected-row count
-- before any bad data ships.

CREATE TABLE IF NOT EXISTS anomalies (
    id              TEXT PRIMARY KEY,
    date            DATE NOT NULL,
    interval_start  TIMESTAMPTZ NOT NULL,
    queue           TEXT NOT NULL,
    category        TEXT NOT NULL,        -- open enum: volume_spike | forecast_bias_drift | adherence_breach | ...
    severity        TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    score           NUMERIC NOT NULL,     -- detector-specific range, NOT 0-1
    observed        NUMERIC,
    expected        NUMERIC,
    residual        NUMERIC,               -- observed - expected
    detector        TEXT NOT NULL CHECK (detector IN ('isolation_forest', 'lof', 'rolling_mean')),
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_anomalies_date_queue
    ON anomalies (date DESC, queue);

CREATE INDEX IF NOT EXISTS ix_anomalies_severity
    ON anomalies (severity, date DESC);
