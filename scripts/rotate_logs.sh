#!/bin/bash
# Rotate logs daily. Keeps 7 days of history.
LOG_DIR="/Users/rory/Documents/Claude/moomoo-trader/logs"
DATE=$(date +%Y%m%d)

for logfile in system.log trader_stdout.log trader_stderr.log tunnel_stdout.log tunnel_stderr.log; do
    src="${LOG_DIR}/${logfile}"
    if [ -f "$src" ] && [ -s "$src" ]; then
        cp "$src" "${src}.${DATE}"
        : > "$src"  # truncate
    fi
done

# Remove backups older than 7 days
find "$LOG_DIR" -name "*.log.*" -mtime +7 -delete 2>/dev/null || true

echo "[$(date)] Log rotation complete"
