#!/bin/bash
# Keeps ngrok tunnel running with permanent URL. Restarts on crash.
# Run with: nohup ./scripts/watchdog_tunnel.sh &
#
# To stop: kill $(cat /tmp/moomoo_tunnel_watchdog.pid)

NGROK="/Users/rory/Documents/Claude/moomoo-trader/bin/ngrok"
DOMAIN="india-clothbound-keira.ngrok-free.dev"
LOG_DIR="/Users/rory/Documents/Claude/moomoo-trader/logs"
PID_FILE="/tmp/moomoo_tunnel_watchdog.pid"
TUNNEL_PID_FILE="/tmp/moomoo_tunnel.pid"

echo $$ > "$PID_FILE"

cleanup() {
    echo "[$(date)] Tunnel watchdog shutting down..."
    if [ -f "$TUNNEL_PID_FILE" ]; then
        kill "$(cat "$TUNNEL_PID_FILE")" 2>/dev/null
        rm -f "$TUNNEL_PID_FILE"
    fi
    rm -f "$PID_FILE"
    exit 0
}

trap cleanup SIGINT SIGTERM

while true; do
    # Wait for dashboard to be up
    if ! curl -sf http://localhost:8877/api/data > /dev/null 2>&1; then
        echo "[$(date)] Dashboard not ready. Waiting 15s..."
        sleep 15
        continue
    fi

    # Kill any stale ngrok or cloudflared
    pkill -f "ngrok http" 2>/dev/null
    pkill -f "cloudflared tunnel" 2>/dev/null
    sleep 1

    echo "[$(date)] Starting ngrok tunnel (${DOMAIN})..."
    $NGROK http 8877 --domain "$DOMAIN" --log "${LOG_DIR}/ngrok.log" --log-format json &
    TPID=$!
    echo $TPID > "$TUNNEL_PID_FILE"
    echo "[$(date)] Tunnel started (PID: $TPID) -> https://${DOMAIN}"

    wait $TPID
    echo "[$(date)] Tunnel exited. Restarting in 10s..."
    sleep 10
done
