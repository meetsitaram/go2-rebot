#!/usr/bin/env bash
set -e

SERVICE="go2-rebot"

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    echo "  Stopping ${SERVICE} service..."
    sudo systemctl stop "$SERVICE"
    echo "  Stopped."
else
    echo "  Service not running."
fi
