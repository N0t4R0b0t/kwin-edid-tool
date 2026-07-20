#!/bin/bash
# Installs the EDID helper daemon as a root-owned systemd service.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo ./install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_DIR=/opt/sunshine-edid-helper

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/daemon.py" "$REPO_ROOT/edid_lib.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/daemon.py"

cp "$SCRIPT_DIR/sunshine-edid-helper.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sunshine-edid-helper.service

echo
echo "Installed. Check status with:"
echo "  systemctl status sunshine-edid-helper"
echo "  journalctl -u sunshine-edid-helper -f"
echo
echo "Now add this under Sunshine's Advanced tab, 'Command Preparations' (Do command,"
echo "leave Undo blank) - NOT the General tab:"
if command -v nc >/dev/null; then
  echo '  sh -c "echo connect,${SUNSHINE_CLIENT_WIDTH},${SUNSHINE_CLIENT_HEIGHT},${SUNSHINE_CLIENT_FPS} | nc -U /run/sunshine-edid-helper.sock || true"'
else
  echo '  sh -c "echo connect,${SUNSHINE_CLIENT_WIDTH},${SUNSHINE_CLIENT_HEIGHT},${SUNSHINE_CLIENT_FPS} | socat - UNIX-CONNECT:/run/sunshine-edid-helper.sock || true"'
  echo "  (nc not found on this system - using socat, which is already installed)"
fi
echo "The trailing '|| true' is required - Sunshine aborts the entire stream launch"
echo "if any prep command exits non-zero, so this must never fail the launch just"
echo "because the daemon happens to be down."
echo
echo "This only enriches the EDID for NEXT time - the connection that triggers this"
echo "hook has already picked its resolution before the hook runs, so it streams at"
echo "Sunshine's own closest-match fallback. A reconnect or later session at the same"
echo "resolution will then find the exact match."
echo
echo "IMPORTANT: the 'newly synthesized custom mode actually gets accepted by the"
echo "driver once selected' path is NOT yet verified live (needs root, which wasn't"
echo "available while building this). Test manually first:"
echo "  sudo $REPO_ROOT/edid-custom-resolutions.py --connector HDMI-A-1 --add 1024x600@60"
echo "and check 'kscreen-doctor -o' actually shows it active (marked with *) before relying on the daemon."
