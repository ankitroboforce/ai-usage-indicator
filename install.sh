#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$HOME/.local/share/claude-usage-indicator"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "Installing gir1.2-ayatanaappindicator3-0.1 (requires sudo)..."
sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1

mkdir -p "$STATE_DIR" "$SERVICE_DIR"
cp "$SCRIPT_DIR/indicator.py" "$STATE_DIR/indicator.py"
cp "$SCRIPT_DIR/claude-usage-indicator.service" "$SERVICE_DIR/claude-usage-indicator.service"

systemctl --user daemon-reload
systemctl --user enable --now claude-usage-indicator.service

echo "Installed. Check status with:"
echo "  systemctl --user status claude-usage-indicator.service"
echo "  journalctl --user -u claude-usage-indicator.service -f"
