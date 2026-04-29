#!/bin/bash
# Regenerate the public dashboard HTML and push it to GitHub.
# Run manually (./scripts/publish_dashboard.sh) or via cron every 15 min.
#
# Cron setup:
#   crontab -e  # then add:
#   */15 * * * * /Users/rory/Documents/Claude/moomoo-trader/scripts/publish_dashboard.sh >> /Users/rory/Documents/Claude/moomoo-trader/logs/publish.log 2>&1

set -euo pipefail

PROJECT_DIR="/Users/rory/Documents/Claude/moomoo-trader"
GH_REPO="bensonhcchen2005-prog/blueshorecapital"
PAPER_START="1000000"

cd "$PROJECT_DIR"

# Cron's PATH is minimal — re-derive everything we need
export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$PATH"

# 1. Generate fresh HTML
./venv/bin/python -m scripts.export_static \
    --gh-repo "$GH_REPO" \
    --paper-start "$PAPER_START"

# 2. Push only if the file actually changed
git add docs/index.html
if git diff --cached --quiet; then
    echo "[$(date)] No change in dashboard HTML — skipping push"
    exit 0
fi

git commit -m "snapshot $(date '+%Y-%m-%d %H:%M')" --quiet
git push origin main --quiet
echo "[$(date)] Published dashboard snapshot"
