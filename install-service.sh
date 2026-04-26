#!/usr/bin/env bash
set -e

SERVICE_NAME="go2-rebot"
SERVICE_FILE="$(cd "$(dirname "$0")" && pwd)/${SERVICE_NAME}.service"
DEST="/etc/systemd/system/${SERVICE_NAME}.service"
POLKIT_FILE="/etc/polkit-1/localauthority/50-local.d/allow-$(whoami)-network.pkla"

usage() {
    echo "Usage: $0 [--install | --update | --uninstall | --status]"
    echo ""
    echo "  --install     Install and enable the systemd service"
    echo "  --update      git pull, sync deps, refresh service file, restart"
    echo "  --uninstall   Stop, disable, and remove the systemd service"
    echo "  --status      Show service status"
    echo ""
    echo "After installing, the service starts automatically on boot."
    echo "The Xbox controller connects via Bluetooth when you press the Xbox button."
}

do_install() {
    if [ ! -f "$SERVICE_FILE" ]; then
        echo "ERROR: Service file not found: $SERVICE_FILE"
        exit 1
    fi

    echo "Installing ${SERVICE_NAME} service..."
    sudo cp "$SERVICE_FILE" "$DEST"
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}.service"

    if [ ! -f "$POLKIT_FILE" ]; then
        echo "Adding polkit rule for headless WiFi switching..."
        LOCAL_USER="$(whoami)"
        sudo tee "$POLKIT_FILE" > /dev/null << PKLA
[Allow ${LOCAL_USER} to manage NetworkManager]
Identity=unix-user:${LOCAL_USER}
Action=org.freedesktop.NetworkManager.*
ResultAny=yes
ResultInactive=yes
ResultActive=yes
PKLA
    fi

    echo ""
    echo "Service installed and enabled (will start on next boot)."
    echo ""
    echo "Commands:"
    echo "  sudo systemctl start ${SERVICE_NAME}    # start now"
    echo "  sudo systemctl stop ${SERVICE_NAME}     # stop"
    echo "  sudo systemctl restart ${SERVICE_NAME}  # restart"
    echo "  sudo systemctl status ${SERVICE_NAME}   # status"
    echo "  journalctl -u ${SERVICE_NAME} -f        # follow logs"
    echo ""
    echo "To start it right now:"
    echo "  sudo systemctl start ${SERVICE_NAME}"
}

do_update() {
    REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
    echo "Updating ${SERVICE_NAME} from ${REPO_DIR}..."

    cd "$REPO_DIR"

    echo "  → git pull"
    git pull --ff-only

    if command -v uv >/dev/null 2>&1; then
        echo "  → uv sync"
        uv sync
    elif [ -x "$HOME/.local/bin/uv" ]; then
        echo "  → uv sync"
        "$HOME/.local/bin/uv" sync
    else
        echo "  WARN: uv not found, skipping dep sync"
    fi

    if [ -f "$SERVICE_FILE" ]; then
        if ! sudo cmp -s "$SERVICE_FILE" "$DEST" 2>/dev/null; then
            echo "  → refreshing service unit"
            sudo cp "$SERVICE_FILE" "$DEST"
            sudo systemctl daemon-reload
        else
            echo "  → service unit unchanged"
        fi
    fi

    echo "  → restart ${SERVICE_NAME}"
    sudo systemctl restart "${SERVICE_NAME}.service"

    echo ""
    echo "Updated. Tail logs with:"
    echo "  journalctl -u ${SERVICE_NAME} -f"
}

do_uninstall() {
    echo "Uninstalling ${SERVICE_NAME} service..."
    sudo systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
    sudo systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
    sudo rm -f "$DEST"
    sudo rm -f "$POLKIT_FILE"
    sudo systemctl daemon-reload

    echo "Service and polkit rule removed."
}

do_status() {
    sudo systemctl status "${SERVICE_NAME}.service" 2>/dev/null || true
    echo ""
    echo "Recent logs:"
    journalctl -u "${SERVICE_NAME}" --no-pager -n 20 2>/dev/null || true
}

case "${1:-}" in
    --install)
        do_install
        ;;
    --update)
        do_update
        ;;
    --uninstall)
        do_uninstall
        ;;
    --status)
        do_status
        ;;
    *)
        usage
        exit 1
        ;;
esac
