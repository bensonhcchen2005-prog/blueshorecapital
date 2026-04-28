#!/bin/bash
# Stop the entire trading system gracefully.

echo "Stopping trading system..."

# Stop watchdogs (they'll stop their children)
for pidfile in /tmp/moomoo_watchdog.pid /tmp/moomoo_tunnel_watchdog.pid; do
    if [ -f "$pidfile" ]; then
        kill "$(cat "$pidfile")" 2>/dev/null
        rm -f "$pidfile"
    fi
done

# Stop trader and tunnel directly (in case watchdog didn't clean up)
for pidfile in /tmp/moomoo_trader.pid /tmp/moomoo_tunnel.pid; do
    if [ -f "$pidfile" ]; then
        kill "$(cat "$pidfile")" 2>/dev/null
        rm -f "$pidfile"
    fi
done

# Stop caffeinate and tunnel processes
pkill -f "caffeinate -si" 2>/dev/null
pkill -f "ngrok http" 2>/dev/null
pkill -f "cloudflared tunnel" 2>/dev/null

sleep 1

# Verify
remaining=$(ps aux | grep -E "watchdog\.sh|auto_trader\.py|cloudflared tunnel|ngrok http|caffeinate -si" | grep -v grep | wc -l)
if [ "$remaining" -eq 0 ]; then
    echo "All processes stopped."
else
    echo "WARNING: $remaining processes still running:"
    ps aux | grep -E "watchdog\.sh|auto_trader\.py|cloudflared tunnel|ngrok http|caffeinate -si" | grep -v grep
fi
