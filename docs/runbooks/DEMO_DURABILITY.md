# Demo Durability Runbook

The public demo has two known rot vectors. Both have happened at least once.
This runbook is how the demo stays alive without anyone babysitting it.

## 1. Render free Postgres expires (~30 days)

**What happens:** the free-tier database is suspended ~1 month after
creation and deleted ~7 days later (observed 2026-06-06: internal host
stopped resolving, API 500'd on every data endpoint).

**Prevention — weekly snapshot** (from the repo root, with the prod
`DATABASE_URL` from the Render dashboard):

```bash
DATABASE_URL=postgres://... backend/scripts/db_snapshot.sh backup
```

Snapshots land in `backend/backups/` (gitignored, last 8 kept). Local
compose DB needs no env var.

**Recovery — when the DB dies:**

1. Render dashboard → delete the dead `wfm-copilot-db` if still listed.
2. Re-apply the Blueprint (render.yaml) — Render recreates the database
   and rewires the `fromDatabase` env vars automatically.
3. Restore the latest snapshot:
   ```bash
   DATABASE_URL=<new url> backend/scripts/db_snapshot.sh restore backend/backups/wfm_<latest>.dump
   ```
4. Verify: `python -m scripts.preflight` from `backend/` (all green), then
   load the dashboard.

Total: ~10 minutes vs ~20+ for a full reseed — and the restore preserves
chat history, schedule-change audit logs, and notifications, which a
reseed loses.

**Permanent fix (decision, costs money):** flip `plan: free` to the paid
tier in `render.yaml` (`basic_256mb`, ~$6/mo — check the dashboard
dropdown; Render renames plans). Worth it the week the demo is in active
outreach rotation.

## 2. Sim clock drifts past the seeded data (~6 days)

**What happens:** `sim_now()` advances with real time, but shift/adherence
data covers a fixed window. Once sim-now exits the window, the intraday
ticker and Wave 3/4 tools read into the void.

**Self-healing (no action needed):** two layers, both re-anchor to the
mid-window date at the current time-of-day and write an audit note to
`sim_anchor.notes`:

- **Startup** — `ensure_sim_anchor_in_window()` runs on every boot
  (`main.py` startup hook).
- **Read path** — `/intraday/today` runs the same check, throttled to once
  per 10 minutes, never raising. This covers long uptimes on the
  non-sleeping `starter` web tier where the startup hook may not fire for
  weeks.

**Manual override** (e.g. to demo a specific day):

```sql
UPDATE sim_anchor
SET anchor_real_ts = NOW(), anchor_sim_ts = '<sim ts you want>'
WHERE id = TRUE;
```

## 3. Background jobs orphaned by restarts

Covered automatically: `sweep_orphaned_jobs()` runs at startup and fails
any schedule/forecast row stuck at pending/running for >15 minutes with a
clear "interrupted — re-submit" message.
