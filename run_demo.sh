#!/usr/bin/env bash
# End-to-end demo runner for WFM Copilot.
#
# What this does:
#   1. Bring up the docker stack (postgres + redis + api) in the background.
#   2. Wait for /health to return ok.
#   3. Seed 12 months of synthetic interval history.
#   4. Kick off an MSTL forecast for the 'sales' queue.
#   5. Poll until that forecast completes.
#   6. Compute 80/20 staffing with 30% shrinkage off that forecast.
#   7. Print a summary.
#
# Run from the repo root: bash run_demo.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- color helpers ---------------------------------------------------
RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
say() { printf "${BLU}%s${RST}\n" "$*"; }
ok()  { printf "${GRN}%s${RST}\n" "$*"; }
warn() { printf "${YEL}%s${RST}\n" "$*"; }
err() { printf "${RED}%s${RST}\n" "$*"; }

# ---- preflight -------------------------------------------------------
command -v docker >/dev/null 2>&1 || { err "docker not found. Install Docker Desktop for Mac and retry."; exit 1; }
command -v curl >/dev/null 2>&1   || { err "curl not found."; exit 1; }
docker info >/dev/null 2>&1       || { err "Docker daemon not running. Open Docker Desktop and retry."; exit 1; }

# Need .env for compose interpolation.
if [ ! -f .env ]; then
  say "==> .env not found, copying from .env.example"
  cp .env.example .env
fi

# ---- 1) bring up the stack ------------------------------------------
say "==> Starting the stack (first build pulls statsforecast/numba — 3-5 min)"
docker compose up -d --build

# ---- 2) wait for /health --------------------------------------------
say "==> Waiting for the API to come up..."
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    ok "API is up."
    break
  fi
  if [ "$i" -eq 60 ]; then
    err "API never came up. Run 'docker compose logs api' to debug."
    exit 1
  fi
  sleep 2
done
echo

# ---- 3) seed data ----------------------------------------------------
ROW_COUNT=$(docker compose exec -T postgres \
    psql -U wfm -d wfm_copilot -tAc "SELECT COUNT(*) FROM interval_history;" \
    2>/dev/null || echo 0)
ROW_COUNT="${ROW_COUNT//[^0-9]/}"
ROW_COUNT="${ROW_COUNT:-0}"

if [ "$ROW_COUNT" -lt 1000 ]; then
  say "==> Seeding 12 months of synthetic interval data..."
  docker compose exec -T api python -m scripts.generate_synthetic_data --seed-db
else
  ok "==> interval_history already has $ROW_COUNT rows — skipping seed."
fi
echo

# ---- 4) run a forecast ----------------------------------------------
say "==> Kicking off a 7-day MSTL forecast for queue=sales..."
FORECAST_RESPONSE=$(curl -fsS -X POST http://localhost:8000/forecasts \
  -H 'Content-Type: application/json' \
  -d '{"queue":"sales","horizon_days":7,"model":"mstl","backtest_days":14}')
FORECAST_ID=$(echo "$FORECAST_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
ok "Forecast run id = $FORECAST_ID"

# ---- 5) poll until completed ----------------------------------------
say "==> Waiting for the forecast to finish (MSTL on 12mo of 30-min data ~30-90s)..."
for i in $(seq 1 120); do
  STATUS=$(curl -fsS "http://localhost:8000/forecasts/$FORECAST_ID?include_intervals=false" \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['status'])")
  printf "\r  status=%s (poll %d)   " "$STATUS" "$i"
  case "$STATUS" in
    completed) echo; ok "Forecast complete."; break ;;
    failed)    echo; err "Forecast failed. Check 'docker compose logs api'."; exit 1 ;;
  esac
  sleep 3
  if [ "$i" -eq 120 ]; then echo; err "Forecast timed out."; exit 1; fi
done

# Backtest scores
echo
say "==> Forecast backtest scores:"
curl -fsS "http://localhost:8000/forecasts/$FORECAST_ID?include_intervals=false" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
mape = d.get('mape'); wape = d.get('wape')
print(f'  MAPE = {mape:.4f}' if mape is not None else '  MAPE = (no backtest)')
print(f'  WAPE = {wape:.4f}' if wape is not None else '  WAPE = (no backtest)')
"
echo

# ---- 6) compute staffing --------------------------------------------
say "==> Computing 80/20 staffing with 30% shrinkage..."
STAFFING_RESPONSE=$(curl -fsS -X POST http://localhost:8000/staffing-requirements \
  -H 'Content-Type: application/json' \
  -d "{\"forecast_run_id\":$FORECAST_ID,\"service_level_target\":0.80,\"target_answer_seconds\":20,\"shrinkage\":0.30}")
echo "$STAFFING_RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  staffing id          = {d[\"id\"]}')
print(f'  intervals_count      = {d[\"intervals_count\"]}')
print(f'  peak_required_agents = {d[\"peak_required_agents\"]}  (after shrinkage)')
"
echo

# ---- 7) summary ------------------------------------------------------
ok "=========================================="
ok "  Demo complete. Open the API docs at:"
ok "  http://localhost:8000/docs"
ok ""
ok "  Forecast run:   GET /forecasts/$FORECAST_ID"
ok "  Staffing run:   GET /staffing-requirements (list)"
ok ""
ok "  Tear down with:  docker compose down"
ok "  Wipe DB volume:  docker compose down -v"
ok "=========================================="
