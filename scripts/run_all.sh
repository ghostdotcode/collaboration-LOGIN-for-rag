#!/usr/bin/env bash
# Start (or restart) every service for local testing.
#   scripts/run_all.sh          start everything
#   scripts/run_all.sh stop     stop what this script started
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=.run; mkdir -p "$RUN"

# This machine already runs Postgres/Redis on the default ports; use the offset ones.
export POSTGRES_PORT="${POSTGRES_PORT:-5433}" REDIS_PORT="${REDIS_PORT:-6380}"
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://postgres:postgres@localhost:5433/meritech_db}"
# Secrets are NOT handled here: the chatbot reads ./.env and the LMS reads
# lms_backend/.env. They must hold the same signing secret for SSO to work.

stop_pid() { [ -f "$RUN/$1.pid" ] && kill "$(cat "$RUN/$1.pid")" 2>/dev/null || true; rm -f "$RUN/$1.pid"; }

if [ "${1:-}" = "stop" ]; then
  for s in chatbot lms visual; do stop_pid $s; done; echo "stopped"; exit 0
fi
for s in chatbot lms visual; do stop_pid $s; done

echo "→ containers (postgres+pgvector :5433, redis :6380)"
docker compose up -d db redis >/dev/null
# wait for the *published host port* (pg_isready inside the container can succeed earlier)
for _ in $(seq 1 60); do (echo >/dev/tcp/127.0.0.1/5433) >/dev/null 2>&1 && docker compose exec -T db pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

if [ "${VISUAL_ENABLED:-false}" = "true" ] && [ -d .venv-visual ] && [ -n "$(ls data/visual/*/manifest.json 2>/dev/null)" ]; then
  echo "→ visual retrieval sidecar :8002"
  nohup .venv-visual/bin/python -m uvicorn visual_service.server:app --port 8002 >"$RUN/visual.log" 2>&1 &
  echo $! >"$RUN/visual.pid"
fi

echo "→ LMS API :8001"
( cd lms_backend
  SSO_AUTO_PROVISION="${SSO_AUTO_PROVISION:-true}" \
  nohup ../venv/bin/python -m uvicorn app.main:app --port 8001 >"../$RUN/lms.log" 2>&1 &
  echo $! >"../$RUN/lms.pid" )

echo "→ chatbot :8000 (models load in the background; /api/status shows progress)"
nohup venv/bin/python -m uvicorn main:app --port 8000 >"$RUN/chatbot.log" 2>&1 &
echo $! >"$RUN/chatbot.pid"

for _ in $(seq 1 40); do curl -fs localhost:8000/healthz >/dev/null 2>&1 && break; sleep 1; done
echo
echo "Open:  http://localhost:8000"
echo "Logs:  $RUN/*.log      Stop: scripts/run_all.sh stop"
