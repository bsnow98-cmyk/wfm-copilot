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
