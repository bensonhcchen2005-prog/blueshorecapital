#!/bin/bash
# Outputs the current investor-ready dashboard URL
URL=$(grep trycloudflare /Users/rory/Documents/Claude/moomoo-trader/logs/tunnel_stderr.log 2>/dev/null | tail -1 | grep -oE 'https://[^ |]+')
if [ -n "$URL" ]; then
    echo "$URL"
    echo "$URL" > /Users/rory/Documents/Claude/moomoo-trader/logs/current_tunnel_url.txt
else
    echo "Tunnel not ready yet. Wait a few seconds and retry."
fi
