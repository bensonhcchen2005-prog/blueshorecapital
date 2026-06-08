#!/bin/bash
# Run thesis monitor + regenerate dashboard + push to GitHub.
# Designed to run on cron every 30 min during market hours,
# every 4h overnight.
#
# Cron lines to add:
#   */30 9-22 * * 1-5  /Users/rory/Documents/Claude/moomoo-trader/scripts/monitor_and_publish.sh >> /Users/rory/Documents/Claude/moomoo-trader/logs/monitor_cron.log 2>&1
#   0    23,3 * * *    /Users/rory/Documents/Claude/moomoo-trader/scripts/monitor_and_publish.sh >> /Users/rory/Documents/Claude/moomoo-trader/logs/monitor_cron.log 2>&1

set -euo pipefail

PROJECT_DIR="/Users/rory/Documents/Claude/moomoo-trader"
GH_REPO="bensonhcchen2005-prog/blueshorecapital"
PAPER_START="1000000"

cd "$PROJECT_DIR"
export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$PATH"

echo "[$(date)] === Monitor + Publish cycle ==="

# 1. Run thesis monitor (writes logs/thesis_alerts_latest.json)
echo "[$(date)] Running thesis monitor..."
./venv/bin/python -m monitoring.thesis_monitor 2>&1 | grep -vE "open_context_base|^2026-|NotOpenSSL|urllib3" | tail -20

# 2. Make sure dashboard is up
if ! curl -sf -o /dev/null --max-time 5 http://localhost:8877/api/data; then
    echo "[$(date)] Dashboard down — restarting"
    pkill -f "dashboard.web_server" 2>/dev/null || true
    sleep 2
    nohup ./venv/bin/python -m dashboard.web_server > logs/dashboard.out 2>&1 &
    sleep 4
fi

# 3. Regenerate static dashboard
echo "[$(date)] Regenerating static dashboard..."
./venv/bin/python -m scripts.export_static --gh-repo "$GH_REPO" --paper-start "$PAPER_START"

# 4. Push to GitHub if changed
git add docs/index.html logs/thesis_alerts_latest.json 2>/dev/null || true
if git diff --cached --quiet; then
    echo "[$(date)] No change — skipping push"
else
    git commit -m "monitor snapshot $(date '+%Y-%m-%d %H:%M')" --quiet
    git push origin main --quiet
    echo "[$(date)] Published"
fi

echo "[$(date)] === Cycle done ==="
