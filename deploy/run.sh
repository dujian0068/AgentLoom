#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.lock.txt
fi
if [[ ! -d apps/web/dist ]]; then
  npm --prefix apps/web ci
  npm --prefix apps/web run build
fi
exec .venv/bin/uvicorn agentloom.app:app --app-dir apps/api --host "${AGENT_LOOM_HOST:-127.0.0.1}" --port "${AGENT_LOOM_PORT:-8766}"
