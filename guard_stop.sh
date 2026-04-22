#!/usr/bin/env bash
# OpenClaw Guard - Stop all services (Mac Mini)
# Frees CPU and memory; disk state preserved for fast restart.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Stopping OpenClaw Guard Stack ==="

# 1. NemoClaw sandbox
if command -v nemoclaw >/dev/null 2>&1; then
  SANDBOX="${1:-my-assistant}"
  echo "[1/3] Stopping sandbox '$SANDBOX'..."
  nemoclaw "$SANDBOX" stop 2>/dev/null && echo "  OK sandbox stopped." \
    || echo "  (sandbox was not running)"
else
  echo "[1/3] nemoclaw not in PATH, skipping."
fi

# 2. Guard gateway
echo "[2/3] Stopping Guard gateway..."
if pkill -f "guard.gateway" 2>/dev/null; then
  echo "  OK gateway stopped."
else
  echo "  (gateway was not running)"
fi

# 3. Colima VM
if command -v colima >/dev/null 2>&1; then
  echo "[3/3] Stopping Colima VM..."
  colima stop 2>/dev/null && echo "  OK Colima stopped." \
    || echo "  (Colima was not running)"
elif [[ -d /Applications/Docker.app ]]; then
  echo "[3/3] Stopping Docker Desktop..."
  osascript -e 'quit app "Docker"' 2>/dev/null || true
  echo "  OK Docker Desktop quit."
else
  echo "[3/3] No Docker runtime to stop."
fi

echo ""
echo "All services stopped. Resources freed."
echo "Restart: bash $PROJECT_DIR/guard_start.sh"
