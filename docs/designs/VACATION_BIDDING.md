# Vacation bidding — add-on design

**Status:** Design draft 2026-06-24. A new copilot domain that *composes* what's already shipped (seniority, capacity, PTO balance, leave) into the single most-requested WFM module. Follows the trust boundary and mechanics of [`EXECUTION_ROADMAP.md`](EXECUTION_ROADMAP.md) + [`CHAT_WRITE_ACTIONS.md`](CHAT_WRITE_ACTIONS.md): **the LLM proposes; a human commits.**

---

## What it is

Annual (or quarterly) **vacation bidding**: agents submit *ranked* preferences for which weeks they want off; management awards weeks **in seniority order**, subject to a per-week capacity cap and each agent's PTO balance. It's the highest-stakes, most-political WFM ritual — and a perfect fit for "the AI shows its math," because the award is a deterministic algorithm over data the copilot already owns:

| Bidding needs | Already in the system |
|---|---|
| Seniority order | `agents.hire_date` (used today by `recommend_ot`/`recommend_vto`) |
| Per-week capacity (how many can be off) | `staffing_requirement_intervals` vs `schedule_coverage` headroom |
| Can the agent afford it | `pto_ledger` balance (the `get_pto_balance` math) |
| The award itself | approved `leave_requests` rows + `pto_ledger` holds — exactly what Surface #1 already writes |

So the award surface is **Surface #1 (leave approval) generalized from one request to a seniority-ordered batch** — same output tables, same audit/undo shape, same envelope.

---

## The principle (unchanged)

Awarding a bid round writes dozens of approved leave rows + PTO holds at once — the highest-blast-radius write yet. It stays a **preview→apply pair**:

1. `preview_award_bids` (read-only, in the registry) runs the award algorithm, renders the full proposed outcome (who got which weeks, who got bumped and why, per-week capacity utilization), and mints one `apply_token`.
2. The UI renders an **Award** button only when the token is present.
3. `POST /vacation/rounds/{id}/award` (NOT a tool, gated to **wfm_manager+**) consumes the token, writes all the approved `leave_requests` + `pto_ledger` holds + a `vacation_award_log` row **in one transaction**, and fires a notification.
4. **Undo** within the window reverses the whole batch; idempotency via the single-use token.

The batch nature is the one real departure from #1–#6 (which write one row). That raises specific decisions — see [§Hard choices](#hard-choices-the-review-part).

---

## New schema — `0023_vacation_bidding.sql`

Three tables. Awards reuse `leave_requests` + `pto_ledger` (no new "awarded vacation" store — the award *is* approved leave, so it shows up everywhere leave already does: calendar, `get_leave_requests`, feasibility math).

```sql
-- A bidding cycle over a set of biddable weeks.
CREATE TABLE bid_rounds (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,                -- "2027 Annual Vacation Bid"
    status        TEXT NOT NULL DEFAULT 'draft' -- draft | open | closed | awarded | cancelled
                  CHECK (status IN ('draft','open','closed','awarded','cancelled')),
    bids_open_at  TIMESTAMPTZ NOT NULL,
    bids_close_at TIMESTAMPTZ NOT NULL,
    season_start  DATE NOT NULL,                -- first biddable week (Monday)
    season_end    DATE NOT NULL,                -- last biddable week (Monday)
    max_weeks_per_agent INT NOT NULL DEFAULT 2, -- bid-policy cap
    awarded_at    TIMESTAMPTZ,                  -- set when status → awarded
    published_at  TIMESTAMPTZ,                  -- set by the SEPARATE publish step (notify decoupled, D-fix #9)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (EXTRACT(DOW FROM season_start) = 1 AND EXTRACT(DOW FROM season_end) = 1)  -- Mondays only
);

-- Per-week capacity = how many agents may be off that week. Default derived
-- from staffing headroom at round-open; an analyst can override per week.
CREATE TABLE bid_week_capacity (
    round_id      BIGINT NOT NULL REFERENCES bid_rounds(id) ON DELETE CASCADE,
    week_start    DATE NOT NULL,                -- Monday
    slots         INT NOT NULL,                 -- agents allowed off
    PRIMARY KEY (round_id, week_start),
    CHECK (EXTRACT(DOW FROM week_start) = 1)    -- Monday-aligned (D-fix #6)
);

-- An agent's ranked preferences. rank 1 = most wanted.
CREATE TABLE vacation_bids (
    id            BIGSERIAL PRIMARY KEY,
    round_id      BIGINT NOT NULL REFERENCES bid_rounds(id) ON DELETE CASCADE,
    agent_id      BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    week_start    DATE NOT NULL,
    rank          INT NOT NULL,                 -- 1..N preference order
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (round_id, agent_id, week_start),
    UNIQUE (round_id, agent_id, rank),
    CHECK (EXTRACT(DOW FROM week_start) = 1)    -- Monday-aligned (D-fix #6)
);
CREATE INDEX ix_vacation_bids_round ON vacation_bids (round_id, agent_id, rank);

-- Award audit (one row per round award; reversible). Mirrors the other
-- *_change_log tables. Persists BOTH awards and the full denial trace so the
-- "why you lost" record is durable, not preview-only (D-fix #4).
CREATE TABLE vacation_award_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    round_id      BIGINT NOT NULL REFERENCES bid_rounds(id) ON DELETE CASCADE,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by    TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    awards        JSONB NOT NULL,   -- [{agent_id, seniority_rank, week_start, leave_request_id, ledger_event_id, awarded_pref_rank}]
    denials       JSONB NOT NULL,   -- [{agent_id, seniority_rank, week_start, pref_rank, reason}]  (D-fix #4)
    summary       JSONB NOT NULL,   -- {n_awarded, n_agents, n_zero_win, n_unfilled_bids, weeks_at_capacity}
    undo_window_ends_at TIMESTAMPTZ NOT NULL,
    undone_at     TIMESTAMPTZ
);

ALTER TABLE chat_apply_tokens
    ADD COLUMN IF NOT EXISTS consumed_vacation_log_id UUID
        REFERENCES vacation_award_log(id) ON DELETE SET NULL;
```

`target_kind='vacation_award'` reuses the generalized token store. The award log keeps the created `leave_request_id`s so **undo deletes exactly those rows + reverses exactly those ledger holds** — no guessing.

---

## The award algorithm (the "math" the copilot shows)

Pure function, deterministic, in `app/services/vacation_bidding.py` — no LLM. Seniority-greedy, the industry-standard "waterfall".

**Award window = Mon–Fri (5 workdays).** Each awarded week writes a `leave_request` spanning the 5 workdays so `leave_decision.leave_pto_hours` (`days×8`) computes **40h** — the same number the affordability check uses. Weekends aren't PTO. `week_hours = 40`. (Fixes the hours mismatch the outside voice caught — a Mon→Sun row would have debited 56h while the gate checked 40h.) Window timestamps are built in UTC from the Monday `week_start` to match the seed/`sim_now` convention; `.date()` bucketing in the staffing-margin math stays stable.

**Pre-load once (no N+1):** all bids for the round, and all `approved`/`pending` `leave_requests` overlapping `[season_start, season_end]`. Existing leave folds into the math on both axes (D2). **Balance decrements in-memory as the agent wins weeks** (D-fix #2 — a multi-week winner must not be checked against the same starting balance twice):

```
remaining_slots[w] = bid_week_capacity[w] - count(existing leave overlapping w)   # capacity net of non-bid leave (D2)
agents sorted by hire_date ASC (most senior first)        # tie-break: employee_id (placeholder policy, see decisions)
for each agent (in seniority order):
    awarded = 0
    bal = pto_balance(agent)                              # running, decremented below
    for each of the agent's bids by rank ASC:
        if awarded == round.max_weeks_per_agent: break
        w = bid.week_start
        if agent already has leave (approved/pending) covering w: record denial("conflict"); continue   # no double-book (D2)
        if remaining_slots[w] <= 0:  record denial("week_full@<senior agent>"); continue
        if bal < week_hours:         record denial("insufficient_pto"); continue   # skip + warn
        award(agent, w); remaining_slots[w] -= 1; awarded += 1; bal -= week_hours   # decrement (D-fix #2)
    if awarded == 0: record zero_win(agent)              # persisted denial trace (D-fix #4)
```

Every non-award is recorded with a reason — the **denial trace is persisted** (not just previewed) so a grievance/union review has the frozen "why you lost" record (D-fix #4).

The preview renders three things — this *is* the product:
1. **Awards table** — agent (with seniority rank), week granted, which preference rank it satisfied (1st choice vs 3rd choice), PTO debited.
2. **Bumped/unfilled** — agents whose top choices were full by the time their turn came, and why (week X hit capacity at senior agent Y). This is what makes a bid defensible to a union.
3. **Capacity heatmap** — slots used / total per week; weeks that maxed out.

Determinism + the visible waterfall is the whole pitch: every award traces to "you're #34 by seniority; week 27 filled at #31."

---

## Surfaces

Read tools (registry):
- `get_bid_round` — round status, window, capacity-by-week, bid counts.
- `get_vacation_bids` — an agent's or the team's submitted preferences.
- `preview_award_bids` — runs the algorithm read-only, renders the 3-part outcome above + `apply_token`. **The marquee tool.**

Write endpoints (NOT tools; on `write_actions.apply_via_token`):
- `POST /vacation/rounds/{id}/award` (**wfm_manager+**) — consume token → batch-insert approved `leave_requests` + `pto_ledger` `use` rows + `vacation_award_log` (awards + denials) → set `awarded_at` → commit. **Does NOT notify** (D-fix #9).
- `POST /vacation/rounds/{id}/publish` (**wfm_manager+**) — separate, deliberate step: fires the agent notifications + sets `published_at`. Lets the manager review the committed award (and undo it) *before* dozens of agents are told. Recommended after the undo window.
- `POST /vacation/rounds/awards/{log_id}/undo` (**wfm_manager+**) — within window: strict reverse (only rows still in the awarded state), reverse matching ledger holds, mark log undone, report drift. Blocked once `published_at` is set unless forced.
- `GET /vacation/rounds` + `GET /vacation/rounds/{id}/awards` — audit feed (includes the denial trace).

Admin/analyst surfaces (v2, keep v1 focused): `preview_open_round` / `preview_close_round`, per-week capacity override (a forecast-override-style pin), bid submission on behalf of an agent.

Frontend: the award preview is a `table` ToolResponse carrying a `vacation_award` block → an **"Award N weeks to M agents"** affordance (manager-gated, like Surface #6). Capacity heatmap can reuse `chart.bar`.

---

## RBAC, audit, undo (reuses everything)

- **Gate:** awarding is `wfm_manager+` (it's roster-shaping, same tier as shift creation). Reading bids/rounds is open behind the password.
- **Actor:** every created `leave_request.decided_by` + the `vacation_award_log.applied_by` = the awarding manager's username (the RBAC layer already threads this).
- **Audit/undo:** `vacation_award_log` mirrors the other `*_change_log` tables; undo reverses the exact `leave_request_id`s + `ledger_event_id`s it created. 24h window.
- **Idempotency/concurrency:** single-use token; the token pins the round's bid-set hash, so if bids change between preview and award → 409 + fresh preview (same as the version check elsewhere).

---

## Hard choices (the "review" part)

The batch write makes this the riskiest surface; these are the decisions I'd lock before building:

1. **Partial failure inside the batch.** If agent #40's PTO balance is stale by award time, do we award everyone else and skip them (partial award + a "skipped" list), or fail the whole transaction? **Recommend:** all-or-nothing transaction for atomicity + a *preview-time* warning list; the preview already shows who'd be skipped, so apply is "commit what you saw."
2. **Capacity source.** Auto-derive per-week slots from staffing headroom, or require an analyst to set them? **Recommend:** auto-seed at round-open from `staffing_requirement_intervals` headroom, analyst-overridable per week (v2). Bidding without a capacity model is just a spreadsheet.
3. **PTO insufficiency.** Hard-block an award the agent can't afford, or award + warn (consistent with Surface #1's "show the math, human decides")? **Recommend:** skip-and-warn in the waterfall; surface it in the bumped list. Hard-block is a v2 policy toggle.
4. **Re-running an award.** A round is `awarded` once. Re-award requires explicit undo first (the token/version guard enforces this). No silent re-award.
5. **Week granularity.** v1 = whole weeks (Mon–Sun), the overwhelming WFM norm. Partial-week / day-level bidding is a much larger schema — explicitly v2.
6. **Fairness policy is named + swappable.** Seniority-greedy is the default and ships first, but the algorithm's policy belongs in the title/audit (like `recommend_ot`'s `policy=`) so a "rotating/round-robin" or "tenure-weighted-lottery" variant can slot in without a rewrite.

## Out of scope (v1)

Agent self-service bid submission UI (v1 ingests bids via seed/import + an analyst-on-behalf tool); appeals/swaps after award (that's the existing leave `swap` type); multi-round / multi-tier seniority groups; partial-week bidding.

## Phasing

1. `0023_vacation_bidding.sql` + seed a demo round with bids.
2. `vacation_bidding.py` service — the award algorithm (pure, unit-tested against a known waterfall).
3. `get_bid_round` / `get_vacation_bids` read tools.
4. `preview_award_bids` (the 3-part render + token).
5. `POST /vacation/rounds/{id}/award` + `/undo` on `apply_via_token` (manager-gated); batch leave + ledger writes.
6. Frontend award affordance + capacity heatmap.
7. Tests: algorithm waterfall (seniority/capacity/PTO edges), batch apply/undo round-trip, RBAC gate, idempotency/409.

## Locked decisions (plan-eng-review + outside voice, 2026-06-24)

1. **Scope:** full v1 as written (~10 files = one write-surface, matches #1–#6). v2 deferrals stand.
2. **No double-booking + capacity nets existing leave (D2):** load approved/pending leave for the season once; (a) skip any week an agent is already off, (b) `remaining_slots[w] = capacity − agents_already_off[w]`.
3. **Concurrency guard (D3, revised by outside voice):** award requires `round.status='closed'` **and** a version/hash over the inputs that can still move while closed — the season's **approved+pending leave set + capacity overrides** (NOT the bids, which are frozen once closed, so hashing them guards nothing). Awarding flips `closed → awarded`; re-award → 409 (wrong status); a leave/capacity change after preview → 409 + fresh preview. `preview_award_bids` errors unless the round is `closed`.
4. **Undo = strict reverse + drift report (D4):** reverse only rows still in the exact awarded state (`status='approved'`, matching window/`decided_by`) and ledger entries not already reversed; drifted rows skipped + listed. No clobber, no double-credit.
5. **Atomicity:** all-or-nothing transaction; apply's only failure is the §3 guard, which rejects before any write.
6. **Hours = 40 (outside voice #1):** award Mon–Fri so `leave_pto_hours` (days×8) computes 40h — matches the affordability gate. Kills the 40-vs-56 divergence.
7. **Cumulative balance (outside voice #2):** decrement the agent's balance in-memory across multi-week wins; never check two weeks against the same starting balance.
8. **Persist denials (outside voice #4):** `vacation_award_log.denials` stores the per-agent, per-bid "why you lost" trace + zero-win list — durable, not preview-only.
9. **Notify decoupled (outside voice #9):** award commits silently; a separate `POST /publish` fires agent notifications (recommended after the undo window), so undo is a real backstop.
10. **Monday alignment + UTC window (outside voice #6):** `CHECK (DOW = 1)` on `season_*` + both week_start columns; the Monday→timestamp window is built in UTC to keep `.date()` staffing math stable.
11. **Seed = the demo (outside voice #8):** hand-tune contention (oversubscribed weeks, a senior blocking a junior, a can't-afford, a zero-win) so the waterfall visibly does something.
12. **Tie-break is a placeholder:** `hire_date ASC, employee_id` is deterministic for v1, but `employee_id` isn't a legitimate fairness rule; real tie resolution (lottery/published policy) is the named-policy v2 hook.

**Test plan (from the review's coverage pass) — implementation must ship these:**
- *Algorithm (pure, unit):* seniority order; capacity exhaustion mid-waterfall; PTO-insufficiency skip; **existing-leave exclusion + capacity netting (D2)**; `hire_date` tie-break determinism; `max_weeks_per_agent` cap; empty bids; all-weeks-full.
- *Batch apply (integration):* round-trips N `leave_requests` + N `pto_ledger` holds + `vacation_award_log`; idempotent re-apply (same token → same log); **409 when status≠closed**; **409 when bid-hash drifts** (+ fresh preview); forced-error → full rollback (zero rows written).
- *Undo (integration):* full reverse round-trip; **drift guard** (a cancelled awarded week is skipped + reported, others reverse); double-undo → 409; outside-window → 409.
- *RBAC:* award + undo gated `wfm_manager+` (viewer/analyst → 403; manager → 200); `applied_by`/`decided_by` = awarding user.
- *Eval (per CLAUDE.md "Prompt/LLM changes"):* tool-selection pins for `preview_award_bids`, `get_bid_round`, `get_vacation_bids` in `eval_tool_selection.py` (use a fixture that picks the demo round id at eval-time, mirroring the existing leave/training pin pattern in TODOS).

**Performance:** trivial at demo scale (50 agents). The one rule: load bids + existing leave in **2 aggregate queries**, compute the waterfall in memory — never query per-agent/per-week (N+1). Batch the leave/ledger inserts (`executemany`), one transaction.

## See also
- `EXECUTION_ROADMAP.md` — the surface pattern this extends; vacation bidding is effectively "Surface #7" (batch leave award).
- `backend/app/services/leave_decision.py`, `pto_ledger`, `app/services/write_actions.py` — the write path this composes.
- `recommend_ot.py` — the seniority (`hire_date`) + named-policy precedent.
