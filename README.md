# WFM Copilot

An AI-native, open-source workforce management platform for contact centers.
Think IEX/NICE, rebuilt with modern ML, an OR scheduling solver, and an LLM copilot.

> **Status: Phases 1-7 shipped + Phase 8 stages 1-5 (multi-skill).** Data
> foundation, forecasting, Erlang C staffing, CP-SAT scheduling, anomaly
> detection, the LLM copilot, the web dashboard, and chat write actions are
> all in. Multi-skill scheduling is feature-complete in code (math + solver +
> chat tools + UI + drift detection); live runtime tuning + per-skill SL
> backtest still need a Postgres+Anthropic deployment. See
> [`docs/designs/MULTI_SKILL_SCHEDULING.md`](docs/designs/MULTI_SKILL_SCHEDULING.md).

## What's in here today

- **FastAPI** backend with `/health`, `/ingest/intervals`, `/forecasts`,
  `/staffing-requirements`.
- **Postgres 16** schema for interval history, agents, skills, schedules,
  forecasts, staffing requirements.
- **Redis 7** ready for the Celery job queue when forecast loads outgrow
  FastAPI BackgroundTasks.
- **Synthetic data generator** so you have realistic numbers from day one.
- **Forecasting service** using Nixtla `statsforecast`. Three models:
  `seasonal_naive` (baseline), `auto_arima`, `mstl` (multi-seasonal, default).
  Built-in MAPE/WAPE backtest scoring.
- **Erlang C staffing service** — pure-Python implementation, no external
  dependency. Computes minimum agents to hit a service-level target, with
  shrinkage applied.
- **Docker Compose** so the whole thing comes up with one command.

## Prerequisites (Mac)

```bash
brew install --cask docker      # then launch Docker Desktop once
brew install postgresql@16      # for the `psql` client (optional but useful)
brew install httpie             # nicer than curl (optional)
```

## First run

From the repo root:

```bash
cp .env.example .env
docker compose up --build
```

> **Heads up:** the first build takes 3-5 minutes because `statsforecast` pulls
> in `numba`. Subsequent builds are fast (cached layer).

Three containers come up:

| Service  | Port | What it does                              |
|----------|------|-------------------------------------------|
| postgres | 5432 | Interval history, forecasts, schedules    |
| redis    | 6379 | Reserved for Celery in a later phase      |
| api      | 8000 | FastAPI app                               |

Verify it's alive:

```bash
curl http://localhost:8000/health
# {"status":"ok","db":"ok"}
```

Auto-generated API docs at <http://localhost:8000/docs>.

## Phase 1 — seed data

```bash
# Easiest: seed 12 months of synthetic data straight into Postgres.
docker compose exec api python -m scripts.generate_synthetic_data --seed-db

# Or write a CSV and ingest via the API:
docker compose exec api python -m scripts.generate_synthetic_data \
  --months 12 \
  --queues sales,support,retention \
  --out /tmp/intervals.csv
curl -X POST http://localhost:8000/ingest/intervals \
  -F "file=@/tmp/intervals.csv"
```

Sanity check:

```bash
docker compose exec postgres psql -U wfm -d wfm_copilot -c \
  "SELECT queue, COUNT(*), MIN(interval_start), MAX(interval_start)
   FROM interval_history GROUP BY queue;"
```

## Phase 2 — run a forecast

```bash
# Kick off a 14-day MSTL forecast for the 'sales' queue:
curl -X POST http://localhost:8000/forecasts \
  -H "Content-Type: application/json" \
  -d '{"queue":"sales","horizon_days":14,"model":"mstl","backtest_days":14}'
# → {"id":1,"status":"pending",...}

# Poll until status flips to "completed" (usually 30-90 seconds for MSTL):
curl http://localhost:8000/forecasts/1

# Once complete, fetch the forecast intervals:
curl http://localhost:8000/forecasts/1/intervals | jq '. | length'
# → 672  (14 days × 48 half-hour intervals)
```

The `mape` and `wape` fields on the run record tell you how the model
performed on the holdout period. Lower is better; under 0.20 (20%) is solid
for contact-center 30-min data, under 0.10 is excellent.

### Available models

| Model            | When to use                                                       |
|------------------|-------------------------------------------------------------------|
| `seasonal_naive` | Baseline. Fast. Just copies last week's pattern.                  |
| `auto_arima`     | Single-seasonality SARIMA. Slower fit. Good when daily curve dominates. |
| `mstl` (default) | Multi-seasonal (daily + weekly). Best fit for typical contact-center data. |

### Architecture note — background work

For Phase 2, forecast jobs run in FastAPI `BackgroundTasks`. This is fine for
single-queue, single-user dev. When you need parallel runs across many queues,
swap the BackgroundTask call in `app/routers/forecasts.py` for a Celery task
that publishes to the Redis container that's already in `docker-compose.yml`.

## Phase 3 — compute staffing requirements

Erlang C math: given a forecast, what's the minimum number of agents per
interval to hit your service-level target? Then apply shrinkage to get what
you actually have to schedule.

```bash
# Compute requirements at the industry-standard "80/20" target with 30%
# shrinkage, off forecast run #1:
curl -X POST http://localhost:8000/staffing-requirements \
  -H "Content-Type: application/json" \
  -d '{
    "forecast_run_id": 1,
    "service_level_target": 0.80,
    "target_answer_seconds": 20,
    "shrinkage": 0.30
  }' | jq
# → returns the staffing record + per-interval rows immediately (Erlang C is fast).

# List staffing records for a forecast:
curl "http://localhost:8000/staffing-requirements?forecast_run_id=1" | jq

# Pull just the interval rows:
curl http://localhost:8000/staffing-requirements/1/intervals | jq
```

What you get per interval:

| Field                      | What it means                                         |
|----------------------------|-------------------------------------------------------|
| `forecast_offered`         | Echoed from forecast for convenience.                 |
| `forecast_aht_seconds`     | Echoed AHT.                                           |
| `required_agents_raw`      | Min agents needed on the phones to hit SL target.     |
| `required_agents`          | Raw grossed up by 1/(1-shrinkage). Schedule to this.  |
| `expected_service_level`   | What SL the raw count actually achieves (≥ target).   |
| `expected_asa_seconds`     | Predicted average speed of answer at the raw count.   |
| `occupancy`                | Offered load / raw agents. Watch for >0.85 (burnout). |

You can compute multiple staffing scenarios off the same forecast by varying
the parameters. Each (forecast_id, sl_target, target_answer_sec, shrinkage)
combination is a unique row — re-running with the same params replaces the
intervals in place.

## Project layout

```
wfm-copilot/
  docker-compose.yml         # 3-service stack
  .env.example               # copy to .env
  backend/
    Dockerfile
    pyproject.toml           # Python deps
    app/
      main.py                # FastAPI app entry; runs migrations on startup
      config.py              # env-based settings
      db.py                  # SQLAlchemy engine + session
      db_migrate.py          # idempotent migration runner
      schemas/               # Pydantic request/response models
        forecasts.py
        staffing.py
      routers/               # /health, /ingest, /forecasts, /staffing-requirements
      services/              # business logic, kept out of routers
        forecasting.py       # statsforecast wrapper + backtest
        staffing.py          # Erlang C math + service
    migrations/
      0001_initial.sql       # base schema
      0002_forecast_status.sql  # adds run lifecycle columns
      0003_staffing.sql      # staffing_requirements + intervals
    scripts/
      generate_synthetic_data.py
```

## Why this stack

- **FastAPI** — async, auto OpenAPI docs, de facto standard for new Python APIs.
- **Postgres** — boring, reliable, great time-series support via the
  `interval_start` index. Add TimescaleDB later if interval volume gets serious.
- **SQLAlchemy 2.0 + psycopg3** — modern Python DB stack.
- **Nixtla `statsforecast`** — fast, modern time-series ML. MSTL handles
  contact-center's two-seasonality (daily + weekly) elegantly.
- **Idempotent SQL migrations** — every `*.sql` in `migrations/` reruns safely
  on every API startup. Swap to Alembic when you need rollbacks or multi-step
  changes.

## Phase 8 — Multi-skill scheduling

Most contact centers run multiple skills (sales, support, billing) with
agents qualified on a subset. Phase 8 makes the whole stack skill-aware.

```bash
# Seed 50 agents with the realistic distribution: 25 single-skill,
# 20 dual-skill, 5 universal. Mix matches docs/designs/MULTI_SKILL_SCHEDULING.md.
docker compose exec api python -m scripts.seed_agents --multi-skill

# Generate per-skill synthetic history. Sales peaks Mondays; support has
# a lunch dip; billing spikes on the 1st of each month.
docker compose exec api python -m scripts.generate_synthetic_data \
  --per-skill --queues sales --skills sales,support,billing --months 6 --seed-db

# Forecast a single skill (per-skill MSTL run):
curl -X POST http://localhost:8000/forecasts \
  -H "Content-Type: application/json" \
  -d '{"queue":"sales","skill_id":2,"horizon_days":7,"model":"mstl","backtest_days":7}'

# Detect skill_mix_drift alongside the standard residual detectors:
curl -X POST http://localhost:8000/anomalies/detect \
  -H "Content-Type: application/json" \
  -d '{"queue":"sales","start_date":"2026-04-01","end_date":"2026-04-29","include_skill_drift":true}'
```

The chat copilot gains two tools:

- `get_skills_coverage(queue, date?)` → per-skill required headcount with
  the substitution discount applied, alongside primaries available and
  shortfall. Sorted by shortfall — biggest gap first.
- `explain_substitution(queue, skill)` → text walk-through of the discount
  math: naive Erlang C → secondary-credit FTE → primary floor → final
  required. Closes the "AI shows its math" loop for multi-skill staffing.

Frontend ships with a top-nav skill picker (persisted to localStorage).
Forecast view goes multi-curve when "All skills" is selected; Schedule view
groups agents by primary skill with badge counts; Scenarios scopes per-skill
demand. See [`docs/designs/MULTI_SKILL_SCHEDULING.md`](docs/designs/MULTI_SKILL_SCHEDULING.md)
for the full math caveat — the discount-based approximation is honest about
not being a Monte Carlo simulator.

## Roadmap

- [x] Phase 1 — Data foundation
- [x] Phase 2 — Forecasting (Nixtla statsforecast, MAPE/WAPE backtest)
- [x] Phase 3 — Staffing requirements (Erlang C, pure Python)
- [x] Phase 4 — Schedule optimization (OR-Tools CP-SAT, flexible 8-hr shift starts)
- [x] Phase 5 — Anomaly detection (sklearn IF + LOF + rolling-mean; TPR=100%/FPR=4.36% on synthetic backtest)
- [x] Phase 6 — LLM copilot (Anthropic Claude API + tool use, SSE streaming, persistence + warning UX, 30s tool timeout, 8 chat tools)
- [x] Phase 7 — Next.js + TS + Tailwind dashboard (4 views + chat panel + render contract)
- [x] Cherry-pick D — Chat write actions (apply tokens, audit log, notifications, Apply button)
- [x] Phase 8 stages 1-5 — Multi-skill scheduling (schema, math, CP-SAT, UI, drift detection)

## License

MIT.
