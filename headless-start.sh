#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

GO2_SSID="Go2_55149"
SCAN_TIMEOUT=120
SCAN_INTERVAL=5
VENV=".venv/bin"

echo "──────────────────────────────────────────────"
echo "  Go2 ReBot — headless boot"
echo "──────────────────────────────────────────────"

echo "  Scanning for ${GO2_SSID} AP (up to ${SCAN_TIMEOUT}s)..."
DEADLINE=$((SECONDS + SCAN_TIMEOUT))
FOUND=false

while [ $SECONDS -lt $DEADLINE ]; do
    if nmcli -t -f SSID device wifi list --rescan yes 2>/dev/null | grep -qx "${GO2_SSID}"; then
        FOUND=true
        break
    fi
    REMAINING=$((DEADLINE - SECONDS))
    echo "  ${GO2_SSID} not visible yet... (${REMAINING}s remaining)"
    sleep "$SCAN_INTERVAL"
done

if [ "$FOUND" = true ]; then
    echo "  ${GO2_SSID} found! Switching WiFi..."
    if nmcli connection up "${GO2_SSID}" 2>&1; then
        echo "  Connected to ${GO2_SSID} — starting Go2 ReBot (AP mode)"
        exec "${VENV}/go2-rebot" --connection-mode ap --wait-for-gamepad 0
    else
        echo "  ERROR: Failed to connect to ${GO2_SSID} — exiting (systemd will retry)"
        exit 1
    fi
else
    echo "  ${GO2_SSID} not found after ${SCAN_TIMEOUT}s — exiting (systemd will retry)"
    exit 1
fi
