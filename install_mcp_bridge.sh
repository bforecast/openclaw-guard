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

# Read the complete list of registered bridges from state.  This is used for
# the combined bundle staging (always merges all active bridges, regardless of
# which names the user passed on the CLI — otherwise passing a single name
# would shrink the bundle to just that one server and evict the others).
ALL_BRIDGES=()
if [[ -f "$BRIDGE_STATE" ]]; then
  mapfile -t ALL_BRIDGES < <("$VENV_PYTHON" - <<'PY' "$BRIDGE_STATE" "$SANDBOX_NAME"
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

if [[ "$MODE" == "all" ]]; then
  if [[ ! -f "$BRIDGE_STATE" ]]; then
    echo "No bridge state found at $BRIDGE_STATE"
    echo "Nothing to activate."
    exit 0
  fi
  BRIDGES=("${ALL_BRIDGES[@]}")
fi

if [[ "${#BRIDGES[@]}" -eq 0 ]]; then
  echo "No MCP bridges selected for sandbox '$SANDBOX_NAME'."
  exit 0
fi

# Stage set is always every registered bridge — this is what ends up in the
# combined bundle. User-supplied names only scope the activate/verify loop.
STAGE_BRIDGES=("${ALL_BRIDGES[@]}")
if [[ "${#STAGE_BRIDGES[@]}" -eq 0 ]]; then
  STAGE_BRIDGES=("${BRIDGES[@]}")
fi

BUNDLE_OUTPUT_DIR="$WORKSPACE/sandbox_workspace/openclaw-data/extensions/$BUNDLE_PLUGIN_ID"

# Fail fast if any selected bridge's upstream MCP server declares a
# credential_env that is unset on the host.  Catches "forgot to source .env"
# before we burn ~60s on activate/verify/stage only to have mcporter 401 in
# the sandbox.
MISSING_CREDS=$(
  "$VENV_PYTHON" - "$GATEWAY_URL" "${BRIDGES[@]}" <<'PY'
import json, os, sys, urllib.request
gateway_url = sys.argv[1].rstrip('/')
names = set(sys.argv[2:])
token = os.environ.get("GUARD_ADMIN_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
req = urllib.request.Request(f"{gateway_url}/v1/mcp/servers")
if token:
    req.add_header("Authorization", f"Bearer {token}")
try:
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
except Exception:
    sys.exit(0)  # activate will surface real error
servers = payload.get("servers") if isinstance(payload, dict) else payload
for server in servers or []:
    name = server.get("name")
    if name not in names:
        continue
    cred_env = server.get("credential_env") or ""
    if cred_env and not os.environ.get(cred_env):
        print(f"{name}:{cred_env}")
PY
)
if [[ -n "$MISSING_CREDS" ]]; then
  echo "ERROR: required credential env vars are unset on the host:"
  while IFS= read -r line; do
    name="${line%%:*}"
    var="${line##*:}"
    echo "  - bridge '$name' needs \$$var (from gateway.yaml credential_env)"
  done <<< "$MISSING_CREDS"
  echo ""
  echo "Fix: set -a; source .env; set +a   (then rerun)"
  exit 2
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

  echo ""
  echo "Optional mcporter debug command:"
  "$VENV_PYTHON" -m guard.cli bridge render-mcporter-add "$BRIDGE_NAME" \
    --sandbox "$SANDBOX_NAME" \
    --workspace "$WORKSPACE" \
    --gateway-port "$BRIDGE_PORT"
done

# ---------------------------------------------------------------------------
# Combined bundle staging: stage each bridge into a temp dir, merge all
# mcpServers entries into one .mcp.json, then deploy once.
# This avoids pod restarts between bridges and keeps the bundle consistent.
# ---------------------------------------------------------------------------
echo ""
echo "Staging combined native MCP bundle (${#STAGE_BRIDGES[@]} bridge(s) from state)..."
MERGED_MCP='{"mcpServers":{}}'
FIRST_STAGED=""
for BRIDGE_NAME in "${STAGE_BRIDGES[@]}"; do
  TMPBUNDLE="$(mktemp -d)"
  "$VENV_PYTHON" -m guard.cli bridge stage-openclaw-bundle "$BRIDGE_NAME" \
    --sandbox "$SANDBOX_NAME" \
    --workspace "$WORKSPACE" \
    --gateway-port "$BRIDGE_PORT" \
    --plugin-id "$BUNDLE_PLUGIN_ID" \
    --output-dir "$TMPBUNDLE" 2>&1
  # Merge this bridge's mcpServers into the running MERGED_MCP
  MERGED_MCP="$("$VENV_PYTHON" -c "
import json, pathlib, sys
merged = json.loads('''$MERGED_MCP''')
bridge = json.loads(pathlib.Path('$TMPBUNDLE/.mcp.json').read_text())
merged['mcpServers'].update(bridge.get('mcpServers', {}))
print(json.dumps(merged))
")"
  if [[ -z "$FIRST_STAGED" ]]; then
    FIRST_STAGED="$TMPBUNDLE"
  else
    rm -rf "$TMPBUNDLE"
  fi
done

# Write the merged bundle to the final output dir
mkdir -p "$BUNDLE_OUTPUT_DIR/.claude-plugin"
cp "$FIRST_STAGED/.claude-plugin/plugin.json" "$BUNDLE_OUTPUT_DIR/.claude-plugin/plugin.json"
"$VENV_PYTHON" -c "
import json, pathlib
pathlib.Path('$BUNDLE_OUTPUT_DIR/.mcp.json').write_text(
    json.dumps(json.loads('''$MERGED_MCP'''), indent=2, sort_keys=True) + '\n'
)
"
rm -rf "$FIRST_STAGED"
echo "  Combined bundle written to: $BUNDLE_OUTPUT_DIR"
echo "  Servers: $(echo "$MERGED_MCP" | "$VENV_PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(', '.join(sorted(d['mcpServers'])))")"

# Bundle-content hash guard: if the merged bundle is byte-for-byte identical
# to the last successfully-synced one, skip the WSL PVC sync + pod restart.
# This keeps idempotent reruns (e.g. "am I still in sync?") cheap and
# avoids killing any openclaw TUI session inside the pod for no reason.
BUNDLE_HASH_FILE="$BUNDLE_OUTPUT_DIR/.mcp.json.sha256"
NEW_BUNDLE_HASH=$("$VENV_PYTHON" -c "
import hashlib, pathlib
print(hashlib.sha256(
    pathlib.Path('$BUNDLE_OUTPUT_DIR/.mcp.json').read_bytes()
).hexdigest())
")
SKIP_POD_RESTART=0
if [[ -f "$BUNDLE_HASH_FILE" ]] \
   && [[ "$(cat "$BUNDLE_HASH_FILE")" == "$NEW_BUNDLE_HASH" ]]; then
  SKIP_POD_RESTART=1
  echo "  Bundle unchanged (sha256 match); will skip PVC sync + pod restart."
fi

# WSL + Docker Desktop: the sandbox runs inside a k3s cluster container
# whose PVC lives in a Docker volume, not on the WSL filesystem.  The
# host-side BUNDLE_OUTPUT_DIR is not visible inside the pod, so we copy
# the staged files into the Docker volume and restart the pod once.
if [[ "$SKIP_POD_RESTART" -eq 0 ]] && \
   grep -qi "microsoft" /proc/version 2>/dev/null && \
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
    # Record the synced bundle hash so a subsequent idempotent rerun can skip
    # the PVC copy + pod restart.
    echo "$NEW_BUNDLE_HASH" > "$BUNDLE_HASH_FILE"
  else
    echo "  WARN: PVC for sandbox '$SANDBOX_NAME' not found in $CLUSTER_VOL"
  fi
fi

if [[ "$PRINT_SANDBOX_STEPS" -eq 1 ]]; then
  echo ""
  echo "Sandbox-side next steps (native MCP first):"
  "$VENV_PYTHON" -m guard.cli bridge print-sandbox-steps \
    --sandbox "$SANDBOX_NAME" \
    --workspace "$WORKSPACE" \
    --gateway-port "$BRIDGE_PORT"
fi

echo ""
echo "Done. Base install stays separate; this script only handles MCP bridge rollout."
