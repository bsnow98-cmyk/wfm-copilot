-- Phase 3.5 — add ASA (Average Speed of Answer) as a staffing constraint.
-- Most ops teams manage to BOTH a service-level target AND an ASA ceiling
-- (e.g. "80% in 20s AND average wait <= 30s"). The staffing math now finds
-- the smallest N that satisfies whichever constraints are set.
--
-- target_asa_seconds is nullable: NULL means "don't enforce ASA, only SL".
-- We do NOT include this column in the unique constraint to avoid a tricky
-- migration; re-running with same SL/AT/Shrinkage but different ASA will
-- update the existing row in place.

ALTER TABLE staffing_requirements
    ADD COLUMN IF NOT EXISTS target_asa_seconds INT;

COMMENT ON COLUMN staffing_requirements.target_asa_seconds IS
    'Maximum acceptable average speed of answer in seconds. NULL = no ASA constraint, SL only.';
