#!/usr/bin/env bash
SERVICE="go2-rebot"

case "${1:-follow}" in
    follow|f)   journalctl -u "$SERVICE" -f ;;
    all|a)      journalctl -u "$SERVICE" --no-pager ;;
    recent|r)   journalctl -u "$SERVICE" --no-pager -n "${2:-50}" ;;
    boot|b)     journalctl -u "$SERVICE" -b --no-pager ;;
    *)
        echo "Usage: ./logs.sh [follow|all|recent [N]|boot]"
        echo "  follow  (default) live tail"
        echo "  all     full history"
        echo "  recent  last N lines (default 50)"
        echo "  boot    current boot only"
        ;;
esac
