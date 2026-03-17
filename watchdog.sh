#!/usr/bin/env bash
# ─────────────────────────────────────────────
# SubnetTrader — watchdog (run via cron every 5 min)
#
# Install:
#   crontab -e
#   */5 * * * * /home/pi/Desktop/SN_Bot/SubnetTrader/watchdog.sh >> /home/pi/Desktop/SN_Bot/SubnetTrader/data/watchdog.log 2>&1
# ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TIMESTAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# Check if backend responds to /health within 5 seconds
if curl -sf --max-time 5 http://localhost:8080/health > /dev/null 2>&1; then
    # Healthy — no action needed
    exit 0
fi

echo "[$TIMESTAMP] Backend not responding — restarting..."

# Kill any stale processes
kill $(lsof -ti:8080) 2>/dev/null
sleep 2

# Start backend
source .venv/bin/activate
nohup python -u -m app.main >> data/bot.log 2>&1 &
BACKEND_PID=$!
echo "[$TIMESTAMP] Backend restarted (PID $BACKEND_PID)"

# Wait for backend to come up
for i in $(seq 1 20); do
    if curl -sf --max-time 3 http://localhost:8080/health > /dev/null 2>&1; then
        echo "[$TIMESTAMP] Backend healthy after ${i}s"
        break
    fi
    sleep 1
    if [ $i -eq 20 ]; then
        echo "[$TIMESTAMP] WARNING: Backend did not respond after 20s"
    fi
done

# Check frontend too
if ! curl -sf --max-time 3 http://localhost:3000 > /dev/null 2>&1; then
    echo "[$TIMESTAMP] Frontend not responding — restarting..."
    fuser -k 3000/tcp 2>/dev/null
    sleep 1
    cd frontend
    nohup npm run dev >> /tmp/frontend.log 2>&1 &
    cd "$SCRIPT_DIR"
    echo "[$TIMESTAMP] Frontend restarted"
fi
