# Deploy Decision Matrix

**Status:** Decision aid, 2026-04-29. Pick a backend host, then ship.

**Context:** Frontend host is locked to **Vercel** (per `CLAUDE.md`). The
backend is a Docker stack (FastAPI + Postgres + Redis) — Vercel doesn't host
that, so the backend goes elsewhere. This doc compares the three plausible
"elsewhere" options.

---

## TL;DR

| Use case | Pick |
|----------|------|
| Fastest time-to-live-URL, OK with web UI | **Render** |
| Comfortable with CLI, want best long-term flexibility | **Fly.io** |
| Already have Railway credit, want simplest Docker-from-repo | Railway |

If you've never deployed to any of them, **start with Render** — the friction
between "click signup" and "URL responds" is the lowest. You can migrate to
Fly.io later if pricing or flexibility becomes the bottleneck.

---

## Comparison

| Dimension | Fly.io | Render | Railway |
|-----------|--------|--------|---------|
| **Docker support** | Native, primary deploy unit | Native, web service type | Native, default deploy unit |
| **Managed Postgres** | Fly Postgres (managed-but-thin); or use Neon/Supabase | First-class managed Postgres | Built-in Postgres add-on |
| **Free tier shape** | 3 small VMs free, 256MB Postgres free, no cold starts | Free web service sleeps after 15min idle, 90-day Postgres trial | $5 monthly credit (usage-based) |
| **Realistic monthly cost (always-on)** | ~$5-7 (1 VM + small Postgres) | ~$14 ($7 web + $7 Postgres) | ~$5-15 (depends on traffic) |
| **Time-to-live-URL (cold)** | 30-60 min if new to the CLI | 15-30 min via dashboard | 15-30 min via dashboard |
| **Time-to-live-URL (warm)** | 5-10 min | 10-15 min | 10 min |
| **Env-var ergonomics** | `fly secrets set KEY=val` | dashboard or `render.yaml` | dashboard |
| **Multi-service (api + redis)** | Easy — multiple `[processes]` or apps | Each service is its own dashboard entry | Each service is its own dashboard entry |
| **CORS to Vercel frontend** | Manual env-var | Manual env-var | Manual env-var |
| **Cold-start behavior** | None on paid tier; minimal on free | Free tier sleeps (BAD for demo recording) | None |
| **GitHub auto-deploy on push** | Yes, optional | Yes, default | Yes, default |
| **Logs/observability** | `fly logs` CLI; web tail | Web dashboard tail | Web dashboard tail |

---

## What's actually decided when you pick

### Render (recommended for first-time deploy)

- **Pros:** Web UI throughout. Postgres add-on is one click. No CLI tools to install. The free 90-day Postgres covers the demo window.
- **Cons:** Free web tier sleeps — *do not* record the demo against the free tier (cold start = 30-second wait on the GIF). Pay $7 for the smallest non-sleeping web service before recording.
- **Setup checklist (~15 min):**
  1. New web service from `backend/` → Docker → keep defaults
  2. Add Postgres service from dashboard
  3. Copy `DATABASE_URL` into web-service env
  4. Add `ANTHROPIC_API_KEY` and `WFM_DEMO_PASSWORD` env vars
  5. Deploy. Wait for build (3-5 min the first time).
  6. Run `python -m scripts.preflight` via Render's shell tab.

### Fly.io (recommended if you'll deploy >1 project this year)

- **Pros:** No cold starts even on free. Cheaper at always-on. Multi-region is a flag away. Better long-term ergonomics.
- **Cons:** Need `flyctl` installed locally. The "fly postgres" command provisions an unmanaged-by-Fly Postgres machine (you own backups). Use Neon or Supabase if that bothers you.
- **Setup checklist (~30 min the first time):**
  1. `brew install flyctl && fly auth signup`
  2. `cd backend && fly launch` — answers no to deploying immediately
  3. Edit generated `fly.toml`: ensure port 8000 internal, add `[env]` for non-secrets
  4. `fly postgres create --name wfm-copilot-db --region iad` (or pick region)
  5. `fly postgres attach wfm-copilot-db`
  6. `fly secrets set ANTHROPIC_API_KEY=sk-ant-... WFM_DEMO_PASSWORD=demo`
  7. `fly deploy`
  8. `fly ssh console -C "python -m scripts.preflight"`

### Railway (skip unless you already have credit)

- **Pros:** Simple. Docker-from-repo works without config. Postgres add-on is one click.
- **Cons:** Usage-based pricing has been hard to predict; the free credit runs out fast if you forget about it. Less "set it and forget it" than Render.
- Skip unless you have specific reason to use it.

---

## Frontend (Vercel — locked)

Already chosen per `CLAUDE.md`. After backend is live:

1. Push frontend to a separate repo or use Vercel's monorepo support
2. Set `NEXT_PUBLIC_API_URL=https://<your-backend-host>` in Vercel env
3. Set `NEXT_PUBLIC_DEMO_PASSWORD=<same-as-backend>` if the auth gate is on
4. Vercel auto-deploys on push to main

CORS: backend's `app/main.py` is currently `allow_origins=["*"]` — fine for
the demo, tighten before any real production use.

---

## Pre-flight check (run after deploy, before demo)

```bash
# Render: open the dashboard's "Shell" tab, then:
python -m scripts.preflight

# Fly.io:
fly ssh console -C "python -m scripts.preflight"

# Local-equivalent (DATABASE_URL pointing at deployed Postgres):
DATABASE_URL=postgresql+psycopg://... \
ANTHROPIC_API_KEY=sk-ant-... \
python -m scripts.preflight
```

PASS on every check = ready to seed data and record the GIF.

---

## What I'd actually do (synthesizing)

1. **Render**, smallest paid web service ($7/mo) + Postgres free for 90 days = **$7 total** for the demo window.
2. Deploy backend, run preflight via Render shell tab, fix any FAIL.
3. `seed_agents --multi-skill` and `generate_synthetic_data --per-skill --seed-db` via the same shell.
4. Deploy frontend to Vercel with `NEXT_PUBLIC_API_URL` pointing at the Render backend.
5. Open the live URL, record the GIF.

Total wall-clock estimate: 60-90 minutes. Most of it is waiting for builds.

If you want to do Fly.io instead, swap step 1-3 with the Fly checklist above — adds ~30 min for the first-time setup but you save $7/mo and avoid the cold-start trap on the free tier.
