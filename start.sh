#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv-backend"

if [ ! -f "$VENV/bin/uvicorn" ]; then
  echo "ERROR: $VENV/bin/uvicorn not found. Run: python3 -m venv .venv-backend && .venv-backend/bin/pip install -r backend/requirements.txt"
  exit 1
fi

free_port() {
  local port="$1"
  local pids
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "Stopping process on port $port..."
    kill $pids 2>/dev/null || true
    sleep 1
  fi
}

cleanup() {
  echo ""
  echo "Shutting down..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
  exit 0
}
trap cleanup INT TERM

free_port 8000
free_port 5173

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

for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "ERROR: Backend failed to start on port 8000"
  cleanup
fi

echo ""
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both."

wait
