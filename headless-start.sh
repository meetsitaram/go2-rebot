#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Primary target — the Go2 dog's AP (full robot+arm control).
GO2_SSID="Go2_55149"

# Fallback wifi — when the dog isn't around. Run arm-only here so the
# system is still useful (the dog won't be directly reachable on this
# network without an --ip override).
FALLBACK_SSID="Frontier:Robots"

SCAN_TIMEOUT=60        # how long to wait for the Go2 AP before giving up
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
        echo "  Connected to ${GO2_SSID} — starting Go2 ReBot (AP mode, full)"
        exec "${VENV}/go2-rebot" --connection-mode ap --wait-for-gamepad 0
    fi
    echo "  WARN: Failed to bring up ${GO2_SSID}, will try fallback."
fi

echo "  ${GO2_SSID} not available — falling back to ${FALLBACK_SSID}..."
if nmcli connection up "${FALLBACK_SSID}" 2>&1; then
    echo "  Connected to ${FALLBACK_SSID} — starting arm-only mode"
    exec "${VENV}/go2-rebot-arm" --wait-for-gamepad 0
fi
echo "  ERROR: Failed to bring up ${FALLBACK_SSID} — exiting (systemd will retry)"
exit 1
