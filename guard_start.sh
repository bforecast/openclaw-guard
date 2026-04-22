#!/usr/bin/env bash
# OpenClaw Guard - Start all services (Mac Mini)
# Resumes from a previous guard_stop.sh; does NOT reinstall.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
LOGS_DIR="$PROJECT_DIR/logs"
SANDBOX="${1:-my-assistant}"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a; source "$PROJECT_DIR/.env"; set +a
fi

echo "=== Starting OpenClaw Guard Stack ==="

# 1. Colima / Docker
echo "[1/3] Starting Docker runtime..."
if command -v colima >/dev/null 2>&1; then
  if docker ps >/dev/null 2>&1; then
    echo "  Docker already running."
  else
    COLIMA_CPU="${COLIMA_CPU:-4}"
    COLIMA_MEMORY="${COLIMA_MEMORY:-8}"
    COLIMA_DISK="${COLIMA_DISK:-60}"
    colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEMORY" --disk "$COLIMA_DISK" 2>&1
    echo "  OK Colima started."
  fi
elif [[ -d /Applications/Docker.app ]]; then
  if docker ps >/dev/null 2>&1; then
    echo "  Docker already running."
  else
    open -a Docker
    attempts=0
    while ! docker ps >/dev/null 2>&1 && (( attempts < 45 )); do
      sleep 2; (( attempts++ ))
    done
    docker ps >/dev/null 2>&1 && echo "  OK Docker Desktop started." \
      || { echo "  ERROR: Docker did not start."; exit 1; }
  fi
else
  echo "  ERROR: No Docker runtime found."; exit 1
fi

# 2. Guard gateway
echo "[2/3] Starting Guard gateway on :$GATEWAY_PORT..."
mkdir -p "$LOGS_DIR"
if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
  echo "  Gateway already running."
else
  lsof -ti :"$GATEWAY_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
  nohup "$VENV_PYTHON" -m guard.gateway > "$LOGS_DIR/gateway.log" 2>&1 &
  # Wait for gateway
  for _ in {1..15}; do
    if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
      echo "  OK Gateway ready."; break
    fi
    sleep 2
  done
  curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null || {
    echo "  ERROR: Gateway did not start. Check $LOGS_DIR/gateway.log"
    exit 1
  }
fi

# 3. NemoClaw sandbox
echo "[3/3] Starting sandbox '$SANDBOX'..."
if command -v nemoclaw >/dev/null 2>&1; then
  nemoclaw "$SANDBOX" start 2>/dev/null && echo "  OK sandbox started." \
    || echo "  WARN: sandbox start returned non-zero (may already be running)"
else
  echo "  WARN: nemoclaw not in PATH. Try: source ~/.zshrc"
fi

echo ""
echo "=========================================="
echo " Stack Ready"
echo "=========================================="
echo ""
echo "Verify:"
echo "  curl http://127.0.0.1:$GATEWAY_PORT/health"
echo "  nemoclaw $SANDBOX status"
echo "  openshell inference get"
echo ""
echo "Stop: bash $PROJECT_DIR/guard_stop.sh"
