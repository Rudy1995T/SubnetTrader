#!/usr/bin/env bash
# ─────────────────────────────────────────────
# SubnetTrader — startup script
# Usage: ./start.sh
# ─────────────────────────────────────────────
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[1/5] Stopping any running processes..."
kill $(lsof -ti:8081) 2>/dev/null && echo "  ✓ Stopped backend (port 8081)" || echo "  – Backend was not running"
fuser -k 3000/tcp 2>/dev/null && echo "  ✓ Stopped frontend (port 3000)" || echo "  – Frontend was not running"
sleep 1

echo "[2/5] Starting backend..."
source .venv/bin/activate
nohup python -u -m app.main >> data/bot.log 2>&1 &
BACKEND_PID=$!
echo "  ✓ Backend started (PID $BACKEND_PID)"

echo "[3/5] Waiting for backend to be ready..."
for i in $(seq 1 15); do
  if curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/health | grep -q "200"; then
    echo "  ✓ Backend ready"
    break
  fi
  sleep 1
  if [ $i -eq 15 ]; then
    echo "  ✗ Backend did not respond after 15s — check data/bot.log"
  fi
done

echo "[4/5] Cleaning stale frontend cache..."
fuser -k 3000/tcp 2>/dev/null || true; sleep 1
rm -rf frontend/.next
echo "  ✓ .next cache cleared"

echo "[5/5] Starting frontend..."
cd frontend
nohup npm run dev >> /tmp/frontend.log 2>&1 &
FRONTEND_PID=$!
cd "$SCRIPT_DIR"
echo "  ✓ Frontend started (PID $FRONTEND_PID) — first page load will take ~10s to compile"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SubnetTrader is running"
echo "  Backend:  http://localhost:8081"
echo "  Frontend: http://localhost:3000"
echo "  Bot logs: tail -f data/bot.log"
echo "  UI logs:  tail -f /tmp/frontend.log"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
