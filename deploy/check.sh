#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/ruff check apps/api packages tests examples deploy/check_db.py deploy/check_workspace.py
.venv/bin/ruff format --check apps/api packages tests examples deploy/check_db.py deploy/check_workspace.py
.venv/bin/pytest -q "$@"
npm --prefix apps/web run format:check
npm --prefix apps/web test
npm --prefix apps/web run build
