-- Wave 3 stage 1 — simulation clock.
-- Live ticker: a single-row anchor maps real wall-clock NOW() to a point in
-- the synthetic dataset, so the demo "is alive" without a worker process.
--
-- sim_now() = anchor_sim_ts + (NOW() - anchor_real_ts) * speed_multiplier
--
-- speed_multiplier = 1.0 means real-time. 60.0 means 1 real second = 1 sim
-- minute (good for "watch the queue drift" demos). Defaults to 1.0.
--
-- Reset by UPDATEing the row; nothing depends on the table having more than
-- one row, but we keep a singleton via a unique-true partial index.

CREATE TABLE IF NOT EXISTS sim_anchor (
    id                  BOOLEAN PRIMARY KEY DEFAULT TRUE,
    anchor_real_ts      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    anchor_sim_ts       TIMESTAMPTZ NOT NULL,
    speed_multiplier    NUMERIC(10,2) NOT NULL DEFAULT 1.0,
    notes               TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT sim_anchor_singleton CHECK (id = TRUE)
);

-- Seed the anchor once. Default to 60 days ago in sim-time so there's plenty
-- of historical aux/adherence data behind the live cursor. Demos can reset
-- this to any point inside the synthetic data window.
INSERT INTO sim_anchor (id, anchor_real_ts, anchor_sim_ts, speed_multiplier, notes)
VALUES (
    TRUE,
    NOW(),
    NOW() - INTERVAL '60 days',
    1.0,
    'Default anchor: 60 days behind wall-clock at first migration run.'
)
ON CONFLICT (id) DO NOTHING;

-- sim_now() — the canonical "what time is it in the simulation" function.
-- Every Wave 3+ tool that wants "now" should call this, never NOW().
CREATE OR REPLACE FUNCTION sim_now() RETURNS TIMESTAMPTZ AS $$
    SELECT anchor_sim_ts + (NOW() - anchor_real_ts) * speed_multiplier
    FROM sim_anchor
    WHERE id = TRUE
    LIMIT 1;
$$ LANGUAGE SQL STABLE;
