#!/usr/bin/env bash
set -euo pipefail

# OpenClaw Guard - EC2 base installer
#
# Validated path:
#   OpenClaw 2026.4.2 in NemoClaw sandbox
#     -> inference.local
#       -> OpenShell provider 'guard'
#         -> Guard gateway (:8090)
#           -> upstream model provider
#
# MCP is intentionally a separate phase. This script installs the base
# inference path only. Run install_mcp_bridge.sh afterwards if MCP bridge
# records should be activated.
#
# The blueprint base policy permanently allows sandbox access to
# host.openshell.internal:8090 so the Guard bridge is treated as core
# install-time infrastructure instead of a runtime-discovered destination.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
LOGS_DIR="$PROJECT_DIR/logs"

export PATH="$HOME/.local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

detect_docker_bridge_ip() {
  if [[ -n "${GUARD_BRIDGE_ALLOWED_IPS:-}" ]]; then
    printf '%s\n' "$GUARD_BRIDGE_ALLOWED_IPS"
    return 0
  fi
  local detected=""
  if command -v ip >/dev/null 2>&1; then
    detected="$(ip -4 addr show docker0 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -n 1)"
  fi
  if [[ -z "$detected" ]] && command -v docker >/dev/null 2>&1; then
    detected="$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null | head -n 1)"
  fi
  printf '%s\n' "$detected"
}

echo "[0/6] System pre-checks..."
df -h | grep '^/dev/' || true
free -m

echo "[1/6] Installing base dependencies..."
sudo apt-get update -y
sudo apt-get install -y \
  ca-certificates curl git jq lsof psmisc expect \
  python3 python3-pip python3-venv

echo "[2/6] Ensuring Docker..."
if ! command -v docker >/dev/null 2>&1; then
  echo "  Installing Docker..."
  sudo apt-get install -y -q docker.io
  sudo systemctl enable docker >/dev/null 2>&1 || true
fi
sudo systemctl start docker || true
sudo chmod 666 /var/run/docker.sock || true
sudo usermod -aG docker "$USER" 2>/dev/null || true
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker access failed."
  exit 1
fi

echo "[3/6] Python environment..."
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv --system-site-packages "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -e "$PROJECT_DIR"

echo "[4/6] Loading API keys..."
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  source "$PROJECT_DIR/.env"
  set +a
fi

echo "Configured keys:"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && echo "  OK OpenRouter" || echo "  MISSING OpenRouter"
[[ -n "${GITHUB_MCP_TOKEN:-}" ]] && echo "  OK GitHub MCP" || echo "  -- GitHub MCP (optional)"

if [[ -z "${GUARD_ADMIN_TOKEN:-}" ]]; then
  GUARD_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  echo "" >> "$PROJECT_DIR/.env"
  echo "GUARD_ADMIN_TOKEN=$GUARD_ADMIN_TOKEN" >> "$PROJECT_DIR/.env"
  export GUARD_ADMIN_TOKEN
fi

echo "[5/6] Starting Security Gateway on port $GATEWAY_PORT..."
mkdir -p "$LOGS_DIR"
sudo fuser -k "$GATEWAY_PORT/tcp" 2>/dev/null || true
nohup "$VENV_PYTHON" -m guard.gateway > "$LOGS_DIR/gateway.log" 2>&1 &

if ! grep -q "host.openshell.internal" /etc/hosts; then
  echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts >/dev/null
fi

BRIDGE_ALLOWED_IPS="$(detect_docker_bridge_ip)"
if [[ -n "$BRIDGE_ALLOWED_IPS" ]]; then
  export GUARD_BRIDGE_ALLOWED_IPS="$BRIDGE_ALLOWED_IPS"
  echo "  Guard bridge allowed IPs: $GUARD_BRIDGE_ALLOWED_IPS"
fi

for _ in {1..15}; do
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    echo "  OK Gateway ready."
    break
  fi
  sleep 1
done

echo "[6/6] Installing NemoClaw CLI..."
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:$GATEWAY_PORT/v1"
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_MODEL="${MODEL_ID:-nvidia/nemotron-3-super-120b-a12b:free}"
export COMPATIBLE_API_KEY="${OPENROUTER_API_KEY:-${OPENAI_API_KEY:-mock-key}}"
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1

SAVED_OPENAI_API_KEY="${OPENAI_API_KEY-}"
SAVED_ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY-}"
SAVED_NVIDIA_API_KEY="${NVIDIA_API_KEY-}"
SAVED_OPENROUTER_API_KEY="${OPENROUTER_API_KEY-}"
restore_provider_keys() {
  if [[ -n "${SAVED_OPENAI_API_KEY:-}" ]]; then export OPENAI_API_KEY="$SAVED_OPENAI_API_KEY"; fi
  if [[ -n "${SAVED_ANTHROPIC_API_KEY:-}" ]]; then export ANTHROPIC_API_KEY="$SAVED_ANTHROPIC_API_KEY"; fi
  if [[ -n "${SAVED_NVIDIA_API_KEY:-}" ]]; then export NVIDIA_API_KEY="$SAVED_NVIDIA_API_KEY"; fi
  if [[ -n "${SAVED_OPENROUTER_API_KEY:-}" ]]; then export OPENROUTER_API_KEY="$SAVED_OPENROUTER_API_KEY"; fi
}
trap restore_provider_keys EXIT
unset OPENAI_API_KEY ANTHROPIC_API_KEY NVIDIA_API_KEY OPENROUTER_API_KEY

NEMOCLAW_SRC="$HOME/.nemoclaw/source"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.4.2}"
REFRESH_NEMOCLAW_SOURCE="${REFRESH_NEMOCLAW_SOURCE:-auto}"

download_nemoclaw_source() {
  echo "Downloading NemoClaw source..."
  rm -rf "$NEMOCLAW_SRC"
  mkdir -p "$NEMOCLAW_SRC"
  curl -fsSL https://github.com/NVIDIA/NemoClaw/archive/refs/heads/main.tar.gz \
    | tar xz --strip-components=1 -C "$NEMOCLAW_SRC"
}

current_source_openclaw_version() {
  if [[ -f "$NEMOCLAW_SRC/Dockerfile.base" ]]; then
    sed -n 's/^ARG OPENCLAW_VERSION=//p' "$NEMOCLAW_SRC/Dockerfile.base" | head -n 1
  fi
}

need_refresh=0
if [[ ! -f "$NEMOCLAW_SRC/package.json" ]]; then
  need_refresh=1
elif [[ "$REFRESH_NEMOCLAW_SOURCE" == "always" ]]; then
  need_refresh=1
elif [[ "$REFRESH_NEMOCLAW_SOURCE" == "auto" ]]; then
  CURRENT_SRC_OPENCLAW_VERSION="$(current_source_openclaw_version || true)"
  if [[ -n "$CURRENT_SRC_OPENCLAW_VERSION" && "$CURRENT_SRC_OPENCLAW_VERSION" != "$OPENCLAW_VERSION" ]]; then
    echo "Refreshing cached NemoClaw source (OpenClaw $CURRENT_SRC_OPENCLAW_VERSION -> $OPENCLAW_VERSION)..."
    need_refresh=1
  fi
fi

if [[ "$need_refresh" -eq 1 ]]; then
  download_nemoclaw_source
fi

echo "Pre-merging Guard blueprint..."
OFFICIAL_POLICIES="$NEMOCLAW_SRC/nemoclaw-blueprint/policies"
if [[ -d "$OFFICIAL_POLICIES" ]]; then
  mkdir -p "$PROJECT_DIR/nemoclaw-blueprint/policies"
  cp -rn "$OFFICIAL_POLICIES/"* "$PROJECT_DIR/nemoclaw-blueprint/policies/" 2>/dev/null || true
fi
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" "$NEMOCLAW_SRC/nemoclaw-blueprint/" 2>/dev/null || \
  cp -r "$PROJECT_DIR/nemoclaw-blueprint/"* "$NEMOCLAW_SRC/nemoclaw-blueprint/"

echo "Staging host-side OpenClaw config..."
"$VENV_PYTHON" -m guard.cli onboard --workspace "$PROJECT_DIR" --sandbox-name openclaw-sandbox --gateway-port "$GATEWAY_PORT"

export NEMOCLAW_REPO_ROOT="$NEMOCLAW_SRC"
bash "$NEMOCLAW_SRC/scripts/install.sh"

restore_provider_keys
trap - EXIT

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "ERROR: NemoClaw not found in PATH."
  exit 1
fi

echo "Setting inference route..."
BEST_CRED="OPENROUTER_API_KEY"
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  if [[ -n "${OPENAI_API_KEY:-}" ]]; then
    BEST_CRED="OPENAI_API_KEY"
  elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    BEST_CRED="ANTHROPIC_API_KEY"
  fi
fi

HOST_ADDR="host.openshell.internal"
openshell provider create --name guard --type openai \
  --credential "$BEST_CRED" \
  --config OPENAI_BASE_URL=http://${HOST_ADDR}:${GATEWAY_PORT}/v1 || \
openshell provider update guard \
  --credential "$BEST_CRED" \
  --config OPENAI_BASE_URL=http://${HOST_ADDR}:${GATEWAY_PORT}/v1

echo "Setting final inference route..."
FINAL_MODEL="${MODEL_ID:-nvidia/nemotron-3-super-120b-a12b:free}"
openshell inference set --provider guard --model "$FINAL_MODEL" --no-verify || true
echo "  OK Inference: $FINAL_MODEL via guard gateway"

echo "[done] Deployment Complete."
if ! grep -q ".local/bin" "$HOME/.bashrc"; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

echo ""
echo "============================================="
echo " Deployment Finished!"
echo "============================================="
echo ""
echo "Verify:"
echo "  curl http://127.0.0.1:$GATEWAY_PORT/health"
echo "  openshell inference get"
echo "  nemoclaw my-assistant status"
echo ""
echo "Test inference from sandbox:"
echo "  openshell sandbox exec --name my-assistant --no-tty --timeout 30 -- \\"
echo "    openclaw infer model run --prompt 'Say hello' --model openrouter/auto --json"
echo ""
echo "MCP phase (run separately after base install):"
echo "  bash install_mcp_bridge.sh --all"
echo "  bash install_mcp_bridge.sh github --sandbox my-assistant"
echo "  The base installer no longer auto-activates MCP bridges."
echo ""
echo "Monitor: tail -f $LOGS_DIR/gateway.log"
