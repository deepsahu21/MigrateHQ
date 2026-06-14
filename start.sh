#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv-backend"

if [ ! -f "$VENV/bin/uvicorn" ]; then
  echo "ERROR: $VENV/bin/uvicorn not found. Run: python3 -m venv .venv-backend && .venv-backend/bin/pip install -r backend/requirements.txt"
  exit 1
fi

cleanup() {
  echo ""
  echo "Shutting down..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
  exit 0
}
trap cleanup INT TERM

# Backend (FastAPI on :8000)
echo "Starting backend..."
cd "$ROOT/backend"
"$VENV/bin/uvicorn" main:app --reload --port 8000 &
BACKEND_PID=$!

# Frontend (Vite on :5173, proxies /api → :8000)
echo "Starting frontend..."
cd "$ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both."

wait
