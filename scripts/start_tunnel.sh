#!/bin/bash
# Start a Cloudflare Quick Tunnel to expose the dashboard publicly.
# The URL changes each time — copy it from the output and share with investors.
# Requires: /tmp/cloudflared (or install via brew install cloudflared)

CLOUDFLARED="/tmp/cloudflared"
DASHBOARD_PORT=8877

if [ ! -f "$CLOUDFLARED" ]; then
    echo "Downloading cloudflared..."
    curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz -o /tmp/cloudflared.tgz
    tar -xzf /tmp/cloudflared.tgz -C /tmp/
    chmod +x "$CLOUDFLARED"
fi

echo "Starting tunnel to localhost:$DASHBOARD_PORT..."
echo "Look for the *.trycloudflare.com URL below — share that with investors."
echo "Press Ctrl+C to stop."
echo ""

$CLOUDFLARED tunnel --url http://localhost:$DASHBOARD_PORT
