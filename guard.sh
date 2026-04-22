#!/usr/bin/env bash
# OpenClaw Guard - Service control (Mac Mini)
# Usage: bash guard.sh start|stop|status [sandbox-name]

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
LOGS_DIR="$PROJECT_DIR/logs"
ACTION="${1:-status}"
SANDBOX="${2:-my-assistant}"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

# ---------------------------------------------------------------------------

do_stop() {
  echo "=== Stopping OpenClaw Guard Stack ==="

  if command -v nemoclaw >/dev/null 2>&1; then
    echo "[1/3] Stopping sandbox '$SANDBOX'..."
    nemoclaw "$SANDBOX" stop 2>/dev/null && echo "  OK" || echo "  (not running)"
  fi

  echo "[2/3] Stopping Guard gateway..."
  pkill -f "guard.gateway" 2>/dev/null && echo "  OK" || echo "  (not running)"

  if command -v colima >/dev/null 2>&1; then
    echo "[3/3] Stopping Colima VM..."
    colima stop 2>/dev/null && echo "  OK" || echo "  (not running)"
  elif [[ -d /Applications/Docker.app ]]; then
    echo "[3/3] Stopping Docker Desktop..."
    osascript -e 'quit app "Docker"' 2>/dev/null || true
    echo "  OK"
  fi

  echo ""
  echo "All stopped. Restart: bash $0 start"
}

do_start() {
  set -e
  [[ -f "$PROJECT_DIR/.env" ]] && { set -a; source "$PROJECT_DIR/.env"; set +a; }

  echo "=== Starting OpenClaw Guard Stack ==="

  # Docker
  echo "[1/3] Docker runtime..."
  if docker ps >/dev/null 2>&1; then
    echo "  Already running."
  elif command -v colima >/dev/null 2>&1; then
    colima start --cpu "${COLIMA_CPU:-4}" --memory "${COLIMA_MEMORY:-8}" --disk "${COLIMA_DISK:-60}" 2>&1
    echo "  OK Colima started."
  elif [[ -d /Applications/Docker.app ]]; then
    open -a Docker
    attempts=0
    while ! docker ps >/dev/null 2>&1 && (( attempts < 45 )); do sleep 2; (( attempts++ )); done
    docker ps >/dev/null 2>&1 || { echo "  ERROR: Docker did not start."; exit 1; }
    echo "  OK Docker Desktop started."
  else
    echo "  ERROR: No Docker runtime found."; exit 1
  fi

  # Gateway
  echo "[2/3] Guard gateway on :$GATEWAY_PORT..."
  mkdir -p "$LOGS_DIR"
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    echo "  Already running."
  else
    lsof -ti :"$GATEWAY_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
    nohup "$VENV_PYTHON" -m guard.gateway > "$LOGS_DIR/gateway.log" 2>&1 &
    for _ in {1..15}; do
      curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1 && break; sleep 2
    done
    curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null || { echo "  ERROR: check $LOGS_DIR/gateway.log"; exit 1; }
    echo "  OK"
  fi

  # Sandbox
  echo "[3/3] Sandbox '$SANDBOX'..."
  if command -v nemoclaw >/dev/null 2>&1; then
    nemoclaw "$SANDBOX" start 2>/dev/null && echo "  OK" || echo "  WARN: may already be running"
  else
    echo "  WARN: nemoclaw not in PATH"
  fi

  echo ""
  echo "Ready. Stop: bash $0 stop"
}

do_status() {
  echo "=== OpenClaw Guard Status ==="
  echo ""

  # Docker
  printf "Docker:   "
  if docker ps >/dev/null 2>&1; then
    if command -v colima >/dev/null 2>&1 && colima status 2>/dev/null | grep -q Running; then
      echo "✓ Colima running"
    else
      echo "✓ running"
    fi
  else
    echo "✗ stopped"
  fi

  # Gateway
  printf "Gateway:  "
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    echo "✓ healthy (:$GATEWAY_PORT)"
  else
    echo "✗ not responding"
  fi

  # Sandbox
  printf "Sandbox:  "
  if command -v nemoclaw >/dev/null 2>&1; then
    nemoclaw "$SANDBOX" status 2>/dev/null || echo "✗ unknown"
  else
    echo "? nemoclaw not in PATH"
  fi
}

# ---------------------------------------------------------------------------

case "$ACTION" in
  start)  do_start ;;
  stop)   do_stop  ;;
  status) do_status ;;
  *)
    echo "Usage: bash $0 {start|stop|status} [sandbox-name]"
    exit 1
    ;;
esac
