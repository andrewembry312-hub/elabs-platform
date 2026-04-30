#!/usr/bin/env bash
# start.sh — Start the E-Labs backend (Linux / macOS / WSL)
# Usage: ./scripts/start.sh [--prod]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND="$ROOT/backend"

# Load .env if present
if [ -f "$ROOT/.env" ]; then
    set -o allexport
    source "$ROOT/.env"
    set +o allexport
fi

# Activate venv if it exists
VENV="$ROOT/.venv"
if [ -d "$VENV" ]; then
    source "$VENV/bin/activate"
else
    echo "No .venv found — using system Python. Run: python -m venv .venv && source .venv/bin/activate && pip install -r backend/requirements.txt"
fi

HOST="${BACKEND_HOST:-127.0.0.1}"
PORT="${BACKEND_PORT:-8001}"

if [[ "${1:-}" == "--prod" ]]; then
    echo "Starting uvicorn in PRODUCTION mode (no reload) on $HOST:$PORT"
    uvicorn app:app --host "$HOST" --port "$PORT" --workers 2
else
    echo "Starting uvicorn in DEV mode (--reload) on $HOST:$PORT"
    cd "$BACKEND"
    uvicorn app:app --host "$HOST" --port "$PORT" --reload
fi
