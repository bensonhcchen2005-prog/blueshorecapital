#!/bin/bash
# Wrapper script for auto_trader.py — used by launchd for 24/7 operation.
# Handles log rotation and clean environment setup.

set -euo pipefail

PROJECT_DIR="/Users/rory/Documents/Claude/moomoo-trader"
VENV_PYTHON="${PROJECT_DIR}/venv/bin/python"
LOG_DIR="${PROJECT_DIR}/logs"
STDOUT_LOG="${LOG_DIR}/trader_stdout.log"
MAX_LOG_SIZE=$((50 * 1024 * 1024))  # 50MB

cd "$PROJECT_DIR"

# Rotate log if too large
if [ -f "$STDOUT_LOG" ] && [ "$(stat -f%z "$STDOUT_LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG_SIZE" ]; then
    mv "$STDOUT_LOG" "${STDOUT_LOG}.$(date +%Y%m%d_%H%M%S).bak"
    # Keep only last 3 backups
    ls -t "${STDOUT_LOG}".*.bak 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true
fi

# Wait for OpenD to be reachable (it needs to start first)
echo "[$(date)] Waiting for OpenD on 127.0.0.1:11111..."
for i in $(seq 1 60); do
    if nc -z 127.0.0.1 11111 2>/dev/null; then
        echo "[$(date)] OpenD is ready."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[$(date)] ERROR: OpenD not reachable after 60s. Exiting."
        exit 1
    fi
    sleep 1
done

echo "[$(date)] Starting auto_trader.py..."
exec "$VENV_PYTHON" auto_trader.py
