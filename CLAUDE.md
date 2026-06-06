# Claude Code Project Notes — WFM Copilot

AI-native, open-source workforce management platform for contact centers. Forecasting, staffing (Erlang C), schedule optimization (CP-SAT), with an LLM copilot that calls real tools and renders charts inline.

Vault (thinking archive): `~/Desktop/Projects/wfm-copilot-vault/`

## Status

Phases 1-4 shipped. Phases 5-7 in plan, with cherry-picks A, B, C, G accepted via `/plan-ceo-review`. Engineering decisions locked via `/plan-eng-review`. Design system locked via `/design-consultation`. See full plan: `~/.gstack/projects/wfm-copilot-vault/ceo-plans/2026-04-29-wfm-copilot-roadmap.md`.

## Design System

**Always read [DESIGN.md](DESIGN.md) before making any visual or UI decisions.** All font choices, colors, spacing, and aesthetic direction are defined there. Do not deviate without explicit user approval.

The memorable thing: **"The AI shows its math."** Every visual decision serves this. Strip anything that doesn't.

Hard rules:
- Geist (display + body) + IBM Plex Mono (IDs, timestamps, exact numbers). NEVER Inter, Roboto, Arial, system-ui as primary.
- Single accent: `#0F766E` (deep teal-green). NO purple, violet, indigo, gradients.
- No shadows, no decorative chrome, no avatars in chat, no message bubbles, no AI branding.
- Severity uses shape + visually-hidden text + color (in that order). Color alone never encodes severity.
- White background, dark text. Dark mode is v1.1, not v1.

In QA mode, flag any code that doesn't match DESIGN.md.

## Stack

- Backend: Python (FastAPI), Postgres, Anthropic Python SDK with tool-use loop
- Frontend: Next.js + TypeScript + Tailwind + shadcn/ui (de-facto starter), Recharts for charts, custom Gantt component
- Tests: pytest (backend), vitest + playwright (frontend)
- Auth (v1): single shared password gate via Basic-Auth middleware
- Deploy: Vercel (frontend), existing Docker stack (API)

## Conventions

- Tools live at `backend/app/tools/<tool_name>.py`, each exporting `definition` (Anthropic SDK shape) + `handler` function. Registry in `tools/__init__.py`.
- The `ToolResponse` discriminated union (defined in `frontend/src/chat/types.ts`) is the contract between Phase 6 tools and Phase 7 renderers. NEVER change without updating both sides.
- Conversation persistence: Postgres `chat_conversations` + `chat_messages`. Frontend stores only `conversation_id` in localStorage.
- Streaming TTFB target: ≤ 800ms p50.
- Anomaly score is detector-specific range (NOT 0-1). Use `score: number` + `detector: enum`, never `confidence`.
- **Schedule writes are aggregate in `schedule_coverage`, per-skill in `shift_segments`.** The 40 dashboard tools join `schedule_coverage` and `staffing_requirement_intervals` on `interval_start` *without* a `skill_id` predicate, so per-skill rows in those tables would fan the joins out. Persist coverage and staffing **aggregate only** (`skill_id IS NULL`); carry per-skill richness in `shift_segments.skill_id` + per-skill `forecast_runs`. The multi-skill solver's per-skill `required[d,slot,k]` is computed in-memory by `app/services/multi_skill_demand.py` and never persisted as staffing rows.
- **Multi-skill schedule persistence:** `solve_multi_skill` is pure math; the persistence wrapper is `app/services/scheduling_multi_skill_persist.py`. It writes the **break explosion** — work / break15 / work / lunch30 / work / break15 / work (two 15-min breaks bracketing a 30-min lunch; 7 segments, 7h work per 8h shift) — because Wave 3+4's adherence generator pattern-matches `segment_type='break'`/`'lunch'`, so a single-`work` schedule would silently drop those exception types.
- **Per-skill `interval_history` requires migration 0017** (`(queue, channel, interval_start, skill_id) NULLS NOT DISTINCT`). Migration 0012 added the column but left the 3-col unique constraint; 0017 closes that. Any `ON CONFLICT` on `interval_history` must target the 4-col form.

## Open critical gaps (must close before Phase 6 GA)

1. Conversation persistence DB failure → user-visible warning + retry queue
2. Anomaly id hash collision → DB unique constraint on `id`, surface upsert errors
3. Solver timeout → 30s timeout + error render + cancellable from UI
4. Missing `ANTHROPIC_API_KEY` → fail fast at startup with clear message

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore

## Testing

Backend: `pytest backend/test/`
Frontend: `bun run test` (vitest unit) and `bun run test:e2e` (playwright)
Eval suites for LLM-touching code: `backend/test/eval_*.py`

## Prompt/LLM changes

If a change touches `backend/app/chat/`, `backend/app/tools/`, or system prompts, run the eval suites:
- `backend/test/eval_anomaly_chat.py` — anomaly citation + hallucination check
- `backend/test/eval_tool_selection.py` — correct tool invoked for each prompt
- `backend/test/eval_render_assertion.py` — typed renderer mounts, not JsonPretty fallback

Compare against the baseline at `eval/baselines/2026-04-29.jsonl`.

## Deployment (locked 2026-04-30)

| Service | Host | URL | Notes |
|---|---|---|---|
| Backend (FastAPI + Postgres) | Render | <https://wfm-copilot-api.onrender.com> | starter web ($7/mo) + free Postgres (90d expiry) |
| Frontend (Next.js dashboard) | Vercel | <https://wfm-copilot.vercel.app> | hobby tier, free |
| Repo | GitHub | <https://github.com/bsnow98-cmyk/wfm-copilot> | auto-deploy on push to main (both hosts) |

`render.yaml` at the repo root is the single source of truth for the backend stack — push changes to `main` and Render rebuilds. Vercel auto-detects Next.js with no config needed.

### Required env vars

- `ANTHROPIC_API_KEY` — set in Render via dashboard (`sync: false` in YAML so it never enters git).
- `WFM_DEMO_PASSWORD` — set in Render. **Must match** the Vercel `NEXT_PUBLIC_DEMO_PASSWORD` exactly. Mismatch produces 401 on every chat call.
- `ANTHROPIC_MODEL` — defaults to `claude-sonnet-4-5-20250929` (dated pin). 4.6 silently no-ops `cache_control`; the chat loop's prompt caching requires 4.5 or earlier. Verified against the API on 2026-05-01.
- Postgres vars wire automatically via `fromDatabase` references in `render.yaml`.

### Pre-flight check

`backend/scripts/preflight.py` is the canonical pre-deploy / post-deploy sanity check. Run it via Render's Shell tab:

```
python -m scripts.preflight
```

Verifies: env vars, DB reach, all 11 tables present, tool registry boots with 8 tools, chat-loop persistence round-trips, Anthropic key works, seed status. Exit code = number of FAILs.

### Operational quirks (deploy-time bugs we've already hit — don't re-step on these)

1. **psycopg3 parses `%` as a placeholder** even in `exec_driver_sql`. Migration files use `%` freely in comments ("80%"), which crashes startup migration. Fix: `db_migrate.py` escapes `%` → `%%` before execution. Future migrations should write `%%` in source if a literal `%` is needed.
2. **SQLAlchemy 2.0 doesn't bind `:name::type`**. The `::` postgres cast operator confuses the parser; use `CAST(:name AS type)` instead. Affects every `:cid::uuid`, `:content::jsonb` pattern. All current code uses CAST; new code must too.
3. **`BaseHTTPMiddleware` short-circuit responses don't get CORS headers** from the outer CORSMiddleware. The auth gate's 401 sets headers directly via `_unauthorized()`. Any new middleware that returns a Response without `call_next` must do the same.
4. **`allow_credentials=True` + `allow_origins=["*"]` is invalid.** Browsers reject the combo. We use `allow_credentials=False` and pass auth via `Authorization` headers explicitly.
5. **Render's `starter` Postgres tier was deprecated** for new databases. Use `free` (90-day expiry) for demos, `basic_256mb` ($6/mo) for non-expiring.
6. **statsforecast/numba MSTL OOMs the 512MB starter web tier**. Two options: bump to `standard` ($25/mo) for real forecast runs, or synthesize forecast data directly via SQL (`scripts/preflight.py`-adjacent pattern: copy `interval_history` forward 7 days into `forecast_intervals`).
7. **CP-SAT scheduling solver also won't run on 512MB.** Same workaround applies — synthesize a schedule for the demo, or bump tier.
8. **Anomaly detection needs `forecast_intervals` aligned to historical dates** (not just future). The `JOIN` requires matching `interval_start` between `forecast_intervals` and `interval_history`. To get non-zero anomalies, backfill `forecast_intervals` for past weeks with values close-but-not-equal to actuals.
9. **psycopg3 can't infer the type of a bound `NULL` parameter.** Any pattern like `WHERE (:x IS NULL OR col = :x)` blows up with `AmbiguousParameter: could not determine data type of parameter $3` when `:x` is None. Wrap the binding: `WHERE (CAST(:x AS BIGINT) IS NULL OR col = CAST(:x AS BIGINT))`. This is gotcha #2 (`CAST(:name AS type)`) in disguise — it also covers bound NULLs, not just `::type` casts. First hit in `forecasting.py:_load_history` on 2026-05-28; fixed there. Watch new query patterns for the same shape.
10. **CP-SAT + MSTL CAN run for prod data — just not on the Render dyno.** The seeders (`seed_prod_skeleton.py`, `seed_prod_real.py`) connect to the target Postgres via `DATABASE_URL` and run the heavy math **in the local process** — no need to upgrade the Render web tier. Pattern: `DATABASE_URL=… python -m scripts.seed_prod_real`. CP-SAT and MSTL OOM only on the 512MB *dyno*, not on a laptop.

### Demo data state (current production)

- 50 active agents with multi-skill mix
- 15,120 interval_history rows (sales/support/retention, 6 months — `sales` was renamed to `sales_inbound` to match chat suggestion chips)
- 1 synthetic forecast_runs row (`model_name='demo_synthetic'`) with 392 forecast_intervals
- 35 anomalies in the table
- No schedules (CP-SAT couldn't run on starter; synthesize if needed for the gantt demo)

### Recording the demo (cherry-pick G)

The chat panel works end-to-end. Suggested flow:
1. Open the live URL fresh
2. Click "Show today's forecast for sales_inbound" — produces inline `chart.line`
3. Try "What anomalies happened this week?" — produces `table` render with monospace ids
4. Toggle the skill picker (top nav)
5. ~60 seconds total, capture with Kap or CleanShot, save as GIF, drop at top of README.md.
