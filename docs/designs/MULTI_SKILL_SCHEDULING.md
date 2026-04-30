# Multi-Skill Scheduling — Design Doc (Phase 8)

**Status:** Draft. Authored 2026-04-29 in response to the locked audience target (WFM-savvy hiring manager + call-center managers). Per [Roadmap.md](../../../wfm-copilot-vault/Roadmap.md), this becomes Phase 8 once accepted; needs `/plan-eng-review` before implementation.

**Source:** `TODOS.md` cherry-pick E in the vault, promoted to a phase because the audience now demands it. *"Without this, the 'real WFM' claim wobbles for any reviewer who knows the domain."*

**Reviewers:** product owner. Domain check from someone with WFM operational experience would also be valuable — multi-skill is where the math gets non-trivial.

---

## Goal

Make scheduling **skill-aware** so it matches how real contact centers actually operate. Most centers run multiple skills (sales, support, billing, retention) with agents qualified on a subset, and effective scheduling has to reconcile demand-per-skill against supply-per-skill — not a single aggregate headcount.

The audience answer (WFM-savvy hiring manager + call-center managers) makes this the difference between "smart prototype" and "tool a real WFM team would recognize." The Phase 4 v1 single-skill solver is the right learning vehicle and the wrong final answer.

## Scope

### v1 (this design)

- Agents have **skills** with **proficiency levels** (1 learner – 5 expert). The `skills` and `agent_skills` tables already exist from Phase 1; this phase finally uses them.
- **Demand** is forecast and staffed **per (queue, skill)** rather than per queue alone.
- **Scheduling** assigns agents to a (skill, interval) pair, respecting their qualifications. Same CP-SAT solver, more dimensions.
- **Substitution credit** for cross-skilled agents — a sales-primary agent whose secondary is support contributes a fraction of an FTE to support coverage. v1 uses a fixed proficiency-based discount; v2 calibrates it from data.
- **Synthetic data** gets realistic skill mixes (most agents 1–2 skills, a few generalists with 3+).
- **API/UI** gets a `skill_id` filter on forecast / staffing / schedule endpoints and views. Agent grid shows skill badges.

### Out of scope (deferred — name them)

- **Mid-shift skill changes.** Agents stay on one skill per shift in v1. Real-world "skill flexing during a peak" is a v2 conversation tied to the substitution model getting more sophisticated.
- **Training plans / proficiency growth over time.** Useful to plan upskilling but separate from scheduling. v3+.
- **Truly correct multi-skill Erlang C** (the joint-queue substitution math). Computationally hard; literature points at simulation as the practical answer. v1 uses a discount approximation with explicit honesty about the limitation. See "The math caveat" below.
- **Skill priority routing.** When two skills compete for the same agent, who wins? v1 minimizes total shortage across skills; v2 might add per-skill priority weights.
- **Inter-queue skill substitution within the same skill family** (e.g., "sales_en" can fall back to "sales_es" with reduced effectiveness). Out — would require a skill-relationship graph.

## The math caveat (read this first)

Multi-skill staffing is one of the parts of WFM where naive math is wrong in a way that's easy to miss. **Per-skill Erlang C summed across skills overstaffs**, because cross-skilled agents implicitly cover peaks across skills they're qualified on. **Per-skill Erlang C with full substitution credit understaffs**, because not every cross-skilled agent is fully effective on every skill they have, and skills compete for the same body simultaneously.

The literature consensus is that **simulation is the gold standard**: model arrivals, service times, agent skill matrices, and routing policy, run Monte Carlo, observe SL. Production WFM products (NICE, Verint, Calabrio) do this.

This design **does not** ship a simulator in v1. It ships a discount-based approximation with three honest properties:

1. The math is documented and inspectable (in line with "the AI shows its math").
2. Validation runs against held-out synthetic data with known ground truth.
3. The chat copilot can explain the approximation when asked (a new tool call returning the substitution math used for a given staffing computation).

If the discount approximation produces SL gaps > 5% from the synthetic ground truth, v1.1 swaps it for a small Monte Carlo simulator. Defer that judgment until we have real numbers.

## Data model changes

### Existing tables — schema changes

```sql
-- 0009_multi_skill.sql

-- 1) interval_history learns about skills.
ALTER TABLE interval_history
  ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);
-- NULL means "queue-aggregate, pre-multi-skill data". Coexists with new rows.

CREATE INDEX IF NOT EXISTS ix_interval_history_skill
  ON interval_history (skill_id, interval_start) WHERE skill_id IS NOT NULL;

-- 2) forecast_runs gets a skill_id (NULL = aggregate, what Phase 2 produced).
ALTER TABLE forecast_runs
  ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);

-- 3) forecast_intervals are already per (run_id, interval_start). The skill
--    is implicit through the run. No change needed.

-- 4) staffing_requirements gains skill_id. Backfill existing rows to NULL.
ALTER TABLE staffing_requirements
  ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);

-- 5) shift_segments.skill_id already exists from Phase 1. Phase 8 starts
--    populating it on every solver write.

-- 6) schedule_coverage already exists from Phase 4. Add per-skill columns.
ALTER TABLE schedule_coverage
  ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills(id);
-- Existing rows have NULL = "all skills aggregated". Mixed-skill rows go to
-- separate (schedule_id, interval_start, skill_id) entries; primary key
-- becomes (schedule_id, interval_start, COALESCE(skill_id, 0)).
```

Backward compatibility: every existing row stays valid with `skill_id = NULL`. Pre-Phase-8 forecasts and schedules continue to work; new ones populate the column.

### Skill proficiency — interpretation

The `agent_skills.proficiency` column already runs 1 (learner) – 5 (expert). We use it as:

- `proficiency = 0` — no qualification (row simply doesn't exist for this agent+skill).
- `proficiency = 1–2` — learner. Counts as the agent's secondary skill at substitution discount; not assignable as primary.
- `proficiency ≥ 3` — qualified. Fully eligible as primary skill.
- `proficiency = 5` — expert. No bonus in v1 — explicit decision, "don't reward expertise with overload."

## Forecasting per skill

Two paths. Pick by data volume.

**Path A — aggregate-then-split (v1 default).** Forecast queue-level volume as today (MSTL). Split into per-skill series using historical skill mix (last 28-day rolling average per `(queue, interval-of-day, day-of-week, skill)`). Cheap, good enough when skill mix is stable.

**Path B — independent per-(queue, skill).** Forecast each skill's volume directly. More accurate when skill mix shifts (e.g., billing peaks at month-end, sales peaks Mondays). Requires more history. Defer to v1.1.

Decision: **A in v1.** Add a metric `skill_mix_drift` (KS-test or χ² between recent and rolling skill mix) — when it exceeds a threshold, surface an anomaly via Phase 5 saying "skill mix has shifted, consider per-skill forecasting." That's a clean upgrade path.

## Staffing — discount-based multi-skill Erlang C

For each interval and each skill `s`:

1. Compute single-skill required `N_s` via the existing Erlang C function against the per-skill forecast.
2. Compute available **effective FTE** for `s` from the assigned agents:
   - Primary-skill agents on `s`: count as 1.0 FTE each.
   - Secondary-skill agents (proficiency ≥ 1) when *not* working their primary skill in this interval: count as `0.7 × proficiency / 5` FTE.
3. Required-after-substitution `N_s' = max(N_s - secondary_credit, primary_required)` where `primary_required = ceil(N_s × 0.5)` is a floor that says "even with cross-skilled help, half your agents on this skill must be primaries." Prevents pathological "all secondaries" staffing.

The discount factor `0.7` and the `0.5` floor are **explicit tunables** with documented defaults. When the chat copilot is asked "why does this staffing look right?" the response includes the discount math used.

### Validation

A new test `backend/test/test_multi_skill_staffing.py` asserts:

- Single-skill case (every agent has one skill) recovers Phase 3 results within 1 agent.
- Two-skill toy case (10 agents, 50/50 split, no cross-skill) — required headcount equals per-skill Erlang C summed.
- Two-skill cross-skill case (10 agents, 5 primary-each, 5 cross) — required headcount is ≥ pure-primary case but < no-substitution case (sanity check, not ground-truth).
- Discount sweep: vary the 0.7 substitution factor across {0.5, 0.7, 0.9, 1.0}, plot required headcount, document the elbow.

If real ops deployment ever happens, replace the discount with a Monte Carlo sim run as a one-time calibration and lock the resulting per-skill effective-FTE table. v1 ships with the discount approximation and the path documented.

## Scheduling — CP-SAT model changes

The design originally proposed per-interval skill assignment with an
anti-thrashing constraint. That conflicts with the v1 deferral of
mid-shift skill changes (see Out-of-scope above), so v1 ships with
**one skill per shift** instead. The simpler model is consistent with
the deferral and avoids carrying a constraint (anti-thrashing) that's
moot when an agent can't switch mid-shift anyway.

**v1 variables** (Phase 8 Stage 3, shipped 2026-04-29):

  `assign[a, d, s_idx, k]` ∈ {0, 1}  — agent `a` starts at shift index `s_idx`
                                       on day `d` working skill `k`.
  `off[a, d]`              ∈ {0, 1}  — agent is off on day `d`.

`(a, k)` variables only created for skills the agent is qualified on, so
the variable count scales with average skills per agent (~2), not
`n_agents × n_skills`.

**Constraints:**

1. **One assignment per (agent, day)**: exactly one of {`assign[a, d, *, *]`,
   `off[a, d]`} = 1.
2. **Skill qualification**: `assign[a, d, s_idx, k] = 0` whenever
   `proficiency[a, k] = 0`. Enforced by skipping variable creation.
3. **Per-skill coverage**: `Σ_a (assign[a, d, s_idx, k] × proficiency_factor[a, k]) ≥ required[d, slot, k]`
   for shifts that cover the slot. `proficiency_factor` is 1.0 for primary
   and `SUBSTITUTION_DISCOUNT × proficiency / 5` for secondary (matches
   `multi_skill_staffing.py`).
4. Existing **H2** (target shifts/week), **H3** (min rest), **H4** (max
   consecutive days) carry over from Phase 4.

**Objective:**

`minimize 100 × Σ shortage + OVERSTAFF_PENALTY_PCT × Σ overage`

The `100×` weight on shortage matches Phase 4's "under-staffing dominates
overage by 10×" policy.

**Coverage scaling.** CP-SAT is integer-only, so effective-FTE values
(which can be 0.7 × 4/5 = 0.56) are scaled by 100 before going into
constraints. Required is also scaled by 100.

**v1.1 path — per-interval skill axis with anti-thrashing.** When mid-shift
skill changes are eventually wanted, the v1.1 model would split `assign`
to `assign[a, d, slot, k]` and add the
`Σ_slot |assign[a, d, slot, k] - assign[a, d, slot+1, k]| ≤ K`
anti-thrashing constraint per `(a, d, k)`. That's a meaningfully bigger
model and tied to the substitution math getting more sophisticated.

### Solver size and runtime

Phase 4 v1 solved 50 agents × 48 intervals (one day) in <30s. Adding a 3-skill axis triples the variable count; expected solve time is **5–10× longer** for an exact solve. We accept up to 90s; on a 30s cap we use the best feasible (`solver.parameters.linearization_level = 1`, `num_search_workers = 8`).

## Synthetic data

Update `scripts/generate_synthetic_data.py` to produce realistic multi-skill data:

- 3 skills: `sales`, `support`, `billing`.
- 50 agents:
  - 25 single-skill (mostly support — 12 — because support is the largest skill in real centers).
  - 20 dual-skill (sales+support: 10, support+billing: 8, sales+billing: 2).
  - 5 tri-skill ("universal agents" — older, experienced, expensive).
- Proficiency distribution: primary 4–5, secondary 2–3, tertiary 1–2.
- Per-interval volume per skill follows distinct seasonality:
  - `sales` peaks Mondays + late afternoon.
  - `support` is steady all week with a midday lunch dip.
  - `billing` spikes month-end and end-of-day.
- Skill mix drift injected at month-end so the `skill_mix_drift` anomaly fires.

## API and UI changes

### Endpoints

All four phase endpoints accept `skill_id` (optional) and return per-skill data when set, aggregated when omitted:

```http
POST /forecasts                    # body: existing fields + optional skill_id
GET  /forecasts?queue=...&skill_id=...
POST /staffing/{forecast_run_id}   # gains skill_id in body
POST /schedules                    # gains skill-aware demand input
GET  /schedules/{id}               # response includes per-skill coverage
```

### New chat tools

Two additions to the Phase 6 tool registry:

| Tool | Returns | Why |
|---|---|---|
| `get_skills_coverage(date, queue?)` | `table` | "How is each skill covered today?" — primary user question once multi-skill is real. |
| `explain_substitution(staffing_id)` | `text` | "Why is this staffing right?" — surfaces the discount math used. Closes the AI-shows-its-math loop. |

### Dashboard changes

- **Top-nav skill picker** — global, defaults to "All skills". Persists to localStorage.
- **Forecast view** — when a single skill is picked, show forecast curve for just that skill. When "All skills", stack curves with a legend.
- **Schedule view** — agent grid groups by primary skill. Each agent row shows skill badges. Gantt segments are color-coded by *skill being worked* (in addition to activity), with a small skill abbreviation in the segment.
- **Scenarios view** — scenario rows can pin a skill_id; the comparison shows per-skill required vs. scheduled.

## Acceptance criteria

1. **Schema migration** `0009_multi_skill.sql` applies idempotently; pre-existing data still queries correctly with `skill_id IS NULL`.
2. **Synthetic data** produces 50 agents with the documented mix; `agent_skills` populated.
3. **Forecast endpoint** with `skill_id` set produces a (queue, skill)-specific MSTL run; aggregate-then-split path documented in the run's `notes` column.
4. **Staffing endpoint** with `skill_id` set returns per-skill required headcount within 1 agent of the validation tests' expectations on the toy datasets.
5. **CP-SAT solver** finds a feasible solution for 50 agents × 48 intervals × 3 skills within 90s; respects qualification, no-overlap, and anti-thrashing constraints.
6. **Schedule view** colors gantt segments by skill, shows skill badges per agent, supports the global skill picker.
7. **Chat tools** `get_skills_coverage` and `explain_substitution` are registered, return typed renders, and the existing `eval_render_assertion` covers them.
8. **Backtest**: per-skill SL target met ≥ 95% of intervals on the synthetic dataset's held-out test month.

## Open questions for the user

1. **Skill substitution discount.** Default 0.7 (with proficiency scaling) is documented. Is that a research input you want to revisit before implementation, or implementation-default acceptable?
2. **Anti-thrashing K.** "At most 2 skill switches per shift" — too tight? Too loose? Real-world ops opinion would help.
3. **Skill picker default.** "All skills" is the safe default. Or should the dashboard default to the user's most recently-viewed skill (better personalization, more localStorage state)?
4. **Phase 8 vs. Phase 8 + chat write actions order.** Cherry-pick D (write actions) is design-locked and ~90-120 min CC. Phase 8 is multi-session. Run them sequentially (D first, then Phase 8) or stack Phase 8 first because audience answer made it more important?

## Implementation phasing

Multi-session work. Sequential, stops are natural:

**Stage 1 — data foundation** (one session)
1. Migration `0009_multi_skill.sql`.
2. Synthetic data generator with realistic skill mixes.
3. Backfill existing forecast/staffing rows with `skill_id = NULL`.
4. Smoke test: existing endpoints still return data unchanged.

**Stage 2 — forecast + staffing per skill** (one session)
5. `ForecastService` accepts `skill_id`, persists it on `forecast_runs`.
6. Staffing service adds `skill_id`, implements discount math.
7. `test_multi_skill_staffing.py` with the four validation cases.
8. New chat tool `get_skills_coverage`.

**Stage 3 — CP-SAT skill axis** (one session, riskiest)
9. CP-SAT model gains `assign[a][i][s]`; constraints + thrashing penalty.
10. Tune `linearization_level`, `num_search_workers` against runtime cap.
11. Backtest: per-skill SL ≥ 95%.
12. New chat tool `explain_substitution`.

**Stage 4 — UI** (one session)
13. Top-nav skill picker.
14. Forecast view multi-curve mode.
15. Schedule view skill badges + skill-colored gantt.
16. Scenarios view per-skill comparison.

**Stage 5 — anomaly + chat polish** (one session)
17. `skill_mix_drift` anomaly category in Phase 5 detector.
18. Chat eval suite picks up the two new tools.
19. README + Demo Walkthrough updates.

Estimated effort: **5 sessions of CC** at the current pace, plus user-side time for the strategic checkpoints (between stages 2 and 3 especially, where the math approximation might want a pause).

## See also

- `~/Desktop/Projects/wfm-copilot-vault/TODOS.md` — original cherry-pick E entry.
- `~/Desktop/Projects/wfm-copilot-vault/Decisions.md` — render contract, single-shared-password auth, Phase 4 done-v1 status.
- `~/Desktop/Projects/wfm-copilot/docs/designs/CHAT_WRITE_ACTIONS.md` — sister design for cherry-pick D, sequenced ahead of Phase 8 in the current plan.
- `~/Desktop/Projects/wfm-copilot/backend/app/services/staffing.py` — Phase 3 single-skill Erlang C implementation, the math we're extending.
- `~/Desktop/Projects/wfm-copilot/backend/app/services/scheduling.py` — Phase 4 CP-SAT model, the solver we're extending.
