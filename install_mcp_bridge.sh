#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="${VENV_PYTHON:-$VENV_DIR/bin/python}"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
BRIDGE_PORT="${GUARD_BRIDGE_PORT:-$GATEWAY_PORT}"
BRIDGE_HOST="${GUARD_BRIDGE_HOST:-}"
SANDBOX_NAME="my-assistant"
WORKSPACE="$PROJECT_DIR"
GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}"
GATEWAY_NAME="nemoclaw"
OPENSHELL_BIN="${OPENSHELL_BIN:-openshell}"
BUNDLE_PLUGIN_ID="${BUNDLE_PLUGIN_ID:-guard-mcp-bundle}"
AUTO_DETECT=1
RUN_VERIFY=1
PRINT_SANDBOX_STEPS=1
MODE="all"
BRIDGES=()

usage() {
  cat <<'EOF'
Usage:
  bash install_mcp_bridge.sh --all [options]
  bash install_mcp_bridge.sh <bridge-name> [<bridge-name> ...] [options]

Options:
  --all                    Activate all saved bridge records for the sandbox
  --sandbox NAME           Sandbox name (default: my-assistant)
  --workspace PATH         Guard workspace path (default: project root)
  --gateway URL            Guard gateway/admin URL (default: http://127.0.0.1:8090)
  --gateway-port PORT      Guard port used in rendered instructions (default: 8090)
  --bridge-host HOST       Sandbox-visible external bridge host/domain
                           (default: GUARD_BRIDGE_HOST)
  --bridge-port PORT       Sandbox-visible bridge port (default: GUARD_BRIDGE_PORT or gateway port)
  --gateway-name NAME      OpenShell gateway name (default: nemoclaw)
  --openshell-bin PATH     OpenShell binary name/path (default: openshell)
  --plugin-id ID           OpenClaw native MCP bundle plugin id
                           (default: guard-mcp-bundle)
  --no-auto-detect         Do not auto-detect a sandbox-reachable host alias
  --skip-verify            Skip `guard bridge verify-runtime`
  --skip-sandbox-steps     Skip rendered sandbox-side next steps
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      MODE="all"
      shift
      ;;
    --sandbox)
      SANDBOX_NAME="$2"
      shift 2
      ;;
    --workspace)
      WORKSPACE="$2"
      shift 2
      ;;
    --gateway)
      GATEWAY_URL="$2"
      shift 2
      ;;
    --gateway-port)
      GATEWAY_PORT="$2"
      BRIDGE_PORT="${GUARD_BRIDGE_PORT:-$GATEWAY_PORT}"
      GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}"
      shift 2
      ;;
    --bridge-host)
      BRIDGE_HOST="$2"
      shift 2
      ;;
    --bridge-port)
      BRIDGE_PORT="$2"
      shift 2
      ;;
    --gateway-name)
      GATEWAY_NAME="$2"
      shift 2
      ;;
    --openshell-bin)
      OPENSHELL_BIN="$2"
      shift 2
      ;;
    --plugin-id)
      BUNDLE_PLUGIN_ID="$2"
      shift 2
      ;;
    --no-auto-detect)
      AUTO_DETECT=0
      shift
      ;;
    --skip-verify)
      RUN_VERIFY=0
      shift
      ;;
    --skip-sandbox-steps)
      PRINT_SANDBOX_STEPS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      MODE="named"
      BRIDGES+=("$1")
      shift
      ;;
  esac
done

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "ERROR: virtualenv Python not found at $VENV_PYTHON" >&2
  echo "Run the base installer first." >&2
  exit 1
fi

WORKSPACE="$(cd "$WORKSPACE" && pwd)"
BRIDGE_STATE="$WORKSPACE/.guard/mcp-bridges.json"

if [[ "$MODE" == "all" ]]; then
  if [[ ! -f "$BRIDGE_STATE" ]]; then
    echo "No bridge state found at $BRIDGE_STATE"
    echo "Nothing to activate."
    exit 0
  fi

  mapfile -t BRIDGES < <("$VENV_PYTHON" - <<'PY' "$BRIDGE_STATE" "$SANDBOX_NAME"
import json
import sys
from pathlib import Path

state_path = Path(sys.argv[1])
sandbox_name = sys.argv[2]
try:
    data = json.loads(state_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)

sandboxes = data.get("sandboxes", {})
sandbox = sandboxes.get(sandbox_name, {}) if isinstance(sandboxes, dict) else {}
bridges = sandbox.get("bridges", {}) if isinstance(sandbox, dict) else {}

if not isinstance(bridges, dict):
    sys.exit(0)

for name in sorted(bridges):
    if name:
        print(name)
PY
  )
fi

if [[ "${#BRIDGES[@]}" -eq 0 ]]; then
  echo "No MCP bridges selected for sandbox '$SANDBOX_NAME'."
  exit 0
fi

echo "Activating MCP bridges for sandbox '$SANDBOX_NAME'..."
for BRIDGE_NAME in "${BRIDGES[@]}"; do
  echo ""
  echo "==> $BRIDGE_NAME"
  ACTIVATE_CMD=(
    "$VENV_PYTHON" -m guard.cli bridge activate "$BRIDGE_NAME"
    --sandbox "$SANDBOX_NAME"
    --workspace "$WORKSPACE"
    --gateway "$GATEWAY_URL"
    --gateway-name "$GATEWAY_NAME"
    --openshell-bin "$OPENSHELL_BIN"
    --gateway-port "$BRIDGE_PORT"
  )
  if [[ -n "$BRIDGE_HOST" ]]; then
    ACTIVATE_CMD+=(--host-alias "$BRIDGE_HOST")
  fi
  if [[ "$AUTO_DETECT" -eq 1 ]]; then
    ACTIVATE_CMD+=(--auto-detect-host-alias)
  fi
  "${ACTIVATE_CMD[@]}"

  if [[ "$RUN_VERIFY" -eq 1 ]]; then
    echo ""
    echo "Runtime verification:"
    "$VENV_PYTHON" -m guard.cli bridge verify-runtime "$BRIDGE_NAME" \
      --sandbox "$SANDBOX_NAME" \
      --workspace "$WORKSPACE"
  fi

  echo ""
  echo "OpenClaw native MCP bundle files:"
  "$VENV_PYTHON" -m guard.cli bridge render-openclaw-bundle "$BRIDGE_NAME" \
    --sandbox "$SANDBOX_NAME" \
    --workspace "$WORKSPACE" \
    --gateway-port "$BRIDGE_PORT" \
    --plugin-id "$BUNDLE_PLUGIN_ID"

  BUNDLE_OUTPUT_DIR="$WORKSPACE/sandbox_workspace/openclaw-data/extensions/$BUNDLE_PLUGIN_ID"
  echo ""
  echo "Staging native MCP bundle into host-mapped sandbox extension dir:"
  "$VENV_PYTHON" -m guard.cli bridge stage-openclaw-bundle "$BRIDGE_NAME" \
    --sandbox "$SANDBOX_NAME" \
    --workspace "$WORKSPACE" \
    --gateway-port "$BRIDGE_PORT" \
    --plugin-id "$BUNDLE_PLUGIN_ID" \
    --output-dir "$BUNDLE_OUTPUT_DIR"

  # WSL + Docker Desktop: the sandbox runs inside a k3s cluster container
  # whose PVC lives in a Docker volume, not on the WSL filesystem.  The
  # host-side BUNDLE_OUTPUT_DIR is not visible inside the pod, so we copy
  # the staged files into the Docker volume and restart the pod.
  if grep -qi "microsoft" /proc/version 2>/dev/null && \
     docker ps --format '{{.Names}}' 2>/dev/null | grep -q openshell-cluster; then
    CLUSTER_VOL="openshell-cluster-$GATEWAY_NAME"
    echo ""
    echo "WSL/Docker Desktop: syncing bundle into k3s PVC ($CLUSTER_VOL)..."
    PVC_ROOT=$(docker run --rm -v "${CLUSTER_VOL}:/cluster" alpine \
      find /cluster/storage -maxdepth 1 -name "*${SANDBOX_NAME}*" -type d 2>/dev/null \
      | head -n1)
    if [[ -n "$PVC_ROOT" ]]; then
      PVC_DEST="$PVC_ROOT/.openclaw-data/extensions/$BUNDLE_PLUGIN_ID"
      docker run --rm \
        -v "${CLUSTER_VOL}:/cluster" \
        -v "${BUNDLE_OUTPUT_DIR}:/bundle:ro" \
        alpine \
        sh -c "mkdir -p \"${PVC_DEST}/.claude-plugin\" \
               && cp /bundle/.mcp.json \"${PVC_DEST}/.mcp.json\" \
               && cp /bundle/.claude-plugin/plugin.json \
                    \"${PVC_DEST}/.claude-plugin/plugin.json\" \
               && echo done" 2>&1
      # Restart the sandbox pod so OpenClaw picks up the new bundle
      POD=$(docker exec "openshell-cluster-${GATEWAY_NAME}" \
              kubectl -n openshell get pods --no-headers 2>/dev/null \
              | grep "^${SANDBOX_NAME} " | awk '{print $1}' | head -n1)
      if [[ -n "$POD" ]]; then
        echo "  Restarting pod $POD..."
        docker exec "openshell-cluster-${GATEWAY_NAME}" \
          kubectl -n openshell delete pod "$POD" 2>/dev/null || true
      fi
      echo "  OK bundle synced to PVC"
    else
      echo "  WARN: PVC for sandbox '$SANDBOX_NAME' not found in $CLUSTER_VOL"
    fi
  fi

  echo ""
  echo "Optional mcporter debug command:"
  "$VENV_PYTHON" -m guard.cli bridge render-mcporter-add "$BRIDGE_NAME" \
    --sandbox "$SANDBOX_NAME" \
    --workspace "$WORKSPACE" \
    --gateway-port "$BRIDGE_PORT"

  if [[ "$PRINT_SANDBOX_STEPS" -eq 1 ]]; then
    echo ""
    echo "Sandbox-side next steps (native MCP first):"
    "$VENV_PYTHON" -m guard.cli bridge print-sandbox-steps \
      --sandbox "$SANDBOX_NAME" \
      --workspace "$WORKSPACE" \
      --gateway-port "$BRIDGE_PORT"
  fi
done

echo ""
echo "Done. Base install stays separate; this script only handles MCP bridge rollout."
