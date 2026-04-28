#!/bin/bash
# Start the entire trading system: prevent sleep + trader + tunnel.
# Usage: ./scripts/start_all.sh
# Stop:  ./scripts/stop_all.sh

PROJECT_DIR="/Users/rory/Documents/Claude/moomoo-trader"
cd "$PROJECT_DIR"

# Check OpenD first
if ! nc -z 127.0.0.1 11111 2>/dev/null; then
    echo "WARNING: OpenD is not running on 127.0.0.1:11111"
    echo "Start moomoo OpenD first, then re-run this script."
    exit 1
fi

# Kill any existing instances
./scripts/stop_all.sh 2>/dev/null

echo "Starting trading system..."

# 1. Prevent sleep
nohup caffeinate -si > /dev/null 2>&1 &
echo "  [OK] caffeinate (prevent sleep)"

# 2. Trader watchdog
nohup ./scripts/watchdog.sh > ./logs/watchdog.log 2>&1 &
echo "  [OK] auto_trader watchdog (PID: $!)"

# 3. Tunnel watchdog
nohup ./scripts/watchdog_tunnel.sh > ./logs/watchdog_tunnel.log 2>&1 &
echo "  [OK] cloudflared tunnel watchdog (PID: $!)"

sleep 3
echo ""
echo "System running. Check status with:"
echo "  ps aux | grep -E 'watchdog|auto_trader|caffeinate|cloudflared' | grep -v grep"
echo ""
echo "Dashboard: http://localhost:8877"
echo "Tunnel URL will appear in: logs/tunnel_stderr.log"
echo "  grep trycloudflare logs/tunnel_stderr.log | tail -1"
