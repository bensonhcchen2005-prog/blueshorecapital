#!/bin/bash
# Watchdog: keeps auto_trader.py running 24/7.
# Restarts on crash with backoff. Run with: nohup ./scripts/watchdog.sh &
#
# To stop: kill $(cat /tmp/moomoo_watchdog.pid)

PROJECT_DIR="/Users/rory/Documents/Claude/moomoo-trader"
PYTHON="${PROJECT_DIR}/venv/bin/python"
SCRIPT="${PROJECT_DIR}/auto_trader.py"
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="/tmp/moomoo_watchdog.pid"
TRADER_PID_FILE="/tmp/moomoo_trader.pid"

echo $$ > "$PID_FILE"

MAX_BACKOFF=300  # 5 min max between restarts
backoff=5

cleanup() {
    echo "[$(date)] Watchdog shutting down..."
    if [ -f "$TRADER_PID_FILE" ]; then
        kill "$(cat "$TRADER_PID_FILE")" 2>/dev/null
        rm -f "$TRADER_PID_FILE"
    fi
    rm -f "$PID_FILE"
    exit 0
}

trap cleanup SIGINT SIGTERM

cd "$PROJECT_DIR"

rotate_logs() {
    local MAX_SIZE=$((50 * 1024 * 1024))  # 50MB
    for logfile in system.log trader_stdout.log trader_stderr.log; do
        local src="${LOG_DIR}/${logfile}"
        if [ -f "$src" ]; then
            local size=$(stat -f%z "$src" 2>/dev/null || echo 0)
            if [ "$size" -gt "$MAX_SIZE" ]; then
                mv "$src" "${src}.$(date +%Y%m%d_%H%M%S)"
                ls -t "${src}".* 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true
                echo "[$(date)] Rotated $logfile (was ${size} bytes)"
            fi
        fi
    done
}

while true; do
    # Rotate logs if needed (checked every restart)
    rotate_logs

    # Check if OpenD is running
    if ! nc -z 127.0.0.1 11111 2>/dev/null; then
        echo "[$(date)] OpenD not reachable. Waiting 30s..."
        sleep 30
        continue
    fi

    echo "[$(date)] Starting auto_trader.py (backoff=${backoff}s)..."
    $PYTHON -u "$SCRIPT" >> "${LOG_DIR}/trader_stdout.log" 2>> "${LOG_DIR}/trader_stderr.log" &
    TRADER_PID=$!
    echo $TRADER_PID > "$TRADER_PID_FILE"
    echo "[$(date)] auto_trader started (PID: $TRADER_PID)"

    # Wait for it to exit
    wait $TRADER_PID
    EXIT_CODE=$?
    echo "[$(date)] auto_trader exited with code $EXIT_CODE"

    # If it ran for more than 60s, reset backoff (it was a real crash, not a config error)
    # We can't easily measure uptime here, so just reset on clean exit
    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date)] Clean shutdown. Will restart at next market open."
        rm -f "$TRADER_PID_FILE"
        # Sleep until next likely market window (60s intervals) instead of
        # permanently exiting — avoids missing Monday after weekend shutdown.
        SLEEP_SECS=120
        echo "[$(date)] Sleeping ${SLEEP_SECS}s before next restart..."
        sleep $SLEEP_SECS
        backoff=5  # reset backoff after clean exit
        continue
    fi

    echo "[$(date)] Restarting in ${backoff}s..."
    sleep $backoff

    # Exponential backoff, capped
    backoff=$((backoff * 2))
    if [ $backoff -gt $MAX_BACKOFF ]; then
        backoff=$MAX_BACKOFF
    fi
done
