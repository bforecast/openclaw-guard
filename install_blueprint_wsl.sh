#!/usr/bin/env bash
set -euo pipefail

# OpenClaw Guard - WSL Ubuntu installer
#
# Mirrors the validated ec2_ubuntu_start.sh flow with WSL-specific handling:
#   - Docker Desktop detection (skips docker.io install if socket already works)
#   - systemd fallback (uses `service` when `systemctl` is not available)
#   - NemoClaw source tarball path (bypasses the official nemoclaw.sh temp-dir/npm-link bug)
#   - blueprint pre-merge before install.sh (no second onboard cycle needed)
#
# After this script: run install_mcp_bridge.sh --all for MCP rollout.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
LOGS_DIR="$PROJECT_DIR/logs"

export PATH="$HOME/.local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

is_wsl() {
  grep -qi "microsoft" /proc/version 2>/dev/null
}

has_systemd() {
  [[ "$(cat /proc/1/comm 2>/dev/null)" == "systemd" ]]
}

start_service() {
  local svc="$1"
  if has_systemd; then
    sudo systemctl enable "$svc" >/dev/null 2>&1 || true
    sudo systemctl start  "$svc" || true
  else
    sudo service "$svc" start 2>/dev/null || true
  fi
}

detect_docker_bridge_ip() {
  if [[ -n "${GUARD_BRIDGE_ALLOWED_IPS:-}" ]]; then
    printf '%s\n' "$GUARD_BRIDGE_ALLOWED_IPS"; return
  fi
  local ip=""

  # Universal: Docker's special "host-gateway" value resolves to the IP that
  # containers use to reach the host — docker0 IP on Docker CE, Docker
  # Desktop VM gateway (192.168.65.254) on Docker Desktop / WSL.
  # Using --add-host forces the resolution without relying on DNS injection,
  # and works before NemoClaw is installed (no cluster container required).
  if command -v docker >/dev/null 2>&1; then
    ip="$(docker run --rm --pull missing \
            --add-host gateway-probe:host-gateway \
            alpine \
            sh -c 'getent ahostsv4 gateway-probe 2>/dev/null \
                   | grep -oE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | head -n1' \
          2>/dev/null)"
  fi

  # Fallback (post-install): probe from the running NemoClaw cluster container.
  if [[ -z "$ip" ]] && command -v docker >/dev/null 2>&1; then
    local cluster
    cluster="$(docker ps --format '{{.Names}}' 2>/dev/null \
               | grep openshell-cluster | head -n1)"
    if [[ -n "$cluster" ]]; then
      ip="$(docker exec "$cluster" getent ahostsv4 host.openshell.internal \
            2>/dev/null | awk '{print $1}' | head -n1)"
    fi
  fi

  printf '%s\n' "$ip"
}

# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------

if ! is_wsl; then
  echo "ERROR: This script is for WSL Ubuntu. For EC2 use ec2_ubuntu_start.sh."
  exit 1
fi

echo "=== OpenClaw Guard: WSL Installer ==="
echo "Project: $PROJECT_DIR"
echo

# ---------------------------------------------------------------------------
# [0/6] Dependencies
# ---------------------------------------------------------------------------

echo "[0/6] Installing base dependencies..."
sudo apt-get update -y -q
sudo apt-get install -y -q \
  ca-certificates curl git jq lsof psmisc expect rsync \
  python3 python3-pip python3-venv

# ---------------------------------------------------------------------------
# [1/6] Docker
# ---------------------------------------------------------------------------

echo "[1/6] Ensuring Docker..."

if docker ps >/dev/null 2>&1; then
  echo "  Docker already reachable (Docker Desktop or running daemon)."
else
  # Install docker.io only if not yet available
  if ! command -v docker >/dev/null 2>&1; then
    sudo apt-get install -y -q docker.io
  fi
  start_service docker
  sudo chmod 666 /var/run/docker.sock 2>/dev/null || true
  sudo usermod -aG docker "$USER" 2>/dev/null || true
  if ! docker ps >/dev/null 2>&1; then
    echo "ERROR: Docker socket still inaccessible."
    echo "  If using Docker Desktop, enable WSL integration in Docker Desktop settings."
    echo "  If using native dockerd, run: sudo chmod 666 /var/run/docker.sock"
    exit 1
  fi
fi

echo "  OK Docker ready."

# ---------------------------------------------------------------------------
# [2/6] Python venv + Guard install
# ---------------------------------------------------------------------------

echo "[2/6] Python environment..."
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv --system-site-packages "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q --upgrade pip setuptools
"$VENV_DIR/bin/pip" install -q -e "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# [3/6] API keys + Guard admin token
# ---------------------------------------------------------------------------

echo "[3/6] Loading API keys..."
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a; source "$PROJECT_DIR/.env"; set +a
fi

echo "Configured keys:"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && echo "  OK OpenRouter"   || echo "  MISSING OpenRouter (required)"
[[ -n "${OPENAI_API_KEY:-}" ]]     && echo "  OK OpenAI"       || echo "  -- OpenAI (optional)"
[[ -n "${ANTHROPIC_API_KEY:-}" ]]  && echo "  OK Anthropic"    || echo "  -- Anthropic (optional)"
[[ -n "${GITHUB_MCP_TOKEN:-}" ]]   && echo "  OK GitHub MCP"   || echo "  -- GitHub MCP (optional)"

if [[ -z "${OPENROUTER_API_KEY:-}${OPENAI_API_KEY:-}${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: At least one upstream API key is required in .env"
  exit 1
fi

if [[ -z "${GUARD_ADMIN_TOKEN:-}" ]]; then
  GUARD_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  { echo ""; echo "GUARD_ADMIN_TOKEN=$GUARD_ADMIN_TOKEN"; } >> "$PROJECT_DIR/.env"
  export GUARD_ADMIN_TOKEN
fi

# ---------------------------------------------------------------------------
# [4/6] Guard Gateway
# ---------------------------------------------------------------------------

echo "[4/6] Starting Security Gateway on port $GATEWAY_PORT..."
mkdir -p "$LOGS_DIR"
lsof -t -i :"$GATEWAY_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
nohup "$VENV_PYTHON" -m guard.gateway > "$LOGS_DIR/gateway.log" 2>&1 &

if ! grep -q "host.openshell.internal" /etc/hosts; then
  echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts >/dev/null
fi

for _ in {1..30}; do
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    echo "  OK Gateway ready."; break
  fi
  sleep 2
done

curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null || {
  echo "ERROR: Gateway did not start. Check $LOGS_DIR/gateway.log"
  exit 1
}

# ---------------------------------------------------------------------------
# [5/6] NemoClaw source tarball + blueprint pre-merge + install.sh
# ---------------------------------------------------------------------------

echo "[5/6] Installing NemoClaw (source tarball path)..."

# Mask direct provider keys during install so NemoClaw picks the custom endpoint.
SAVED_OPENAI="${OPENAI_API_KEY-}"
SAVED_ANTHROPIC="${ANTHROPIC_API_KEY-}"
SAVED_NVIDIA="${NVIDIA_API_KEY-}"
SAVED_OPENROUTER="${OPENROUTER_API_KEY-}"
restore_keys() {
  [[ -n "${SAVED_OPENAI:-}" ]]     && export OPENAI_API_KEY="$SAVED_OPENAI"
  [[ -n "${SAVED_ANTHROPIC:-}" ]]  && export ANTHROPIC_API_KEY="$SAVED_ANTHROPIC"
  [[ -n "${SAVED_NVIDIA:-}" ]]     && export NVIDIA_API_KEY="$SAVED_NVIDIA"
  [[ -n "${SAVED_OPENROUTER:-}" ]] && export OPENROUTER_API_KEY="$SAVED_OPENROUTER"
}
trap restore_keys EXIT
unset OPENAI_API_KEY ANTHROPIC_API_KEY NVIDIA_API_KEY OPENROUTER_API_KEY

export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:${GATEWAY_PORT}/v1"
export NEMOCLAW_MODEL="${MODEL_ID:-nvidia/nemotron-3-super-120b-a12b:free}"
export COMPATIBLE_API_KEY="${SAVED_OPENROUTER:-${SAVED_OPENAI:-mock-key}}"
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1

NEMOCLAW_SRC="$HOME/.nemoclaw/source"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.4.2}"
REFRESH_NEMOCLAW_SOURCE="${REFRESH_NEMOCLAW_SOURCE:-auto}"

current_src_openclaw_version() {
  [[ -f "$NEMOCLAW_SRC/Dockerfile.base" ]] && \
    sed -n 's/^ARG OPENCLAW_VERSION=//p' "$NEMOCLAW_SRC/Dockerfile.base" | head -n1 || true
}

need_refresh=0
[[ ! -f "$NEMOCLAW_SRC/package.json" ]] && need_refresh=1
[[ "$REFRESH_NEMOCLAW_SOURCE" == "always" ]] && need_refresh=1
if [[ "$REFRESH_NEMOCLAW_SOURCE" == "auto" && "$need_refresh" -eq 0 ]]; then
  CUR="$(current_src_openclaw_version)"
  if [[ -n "$CUR" && "$CUR" != "$OPENCLAW_VERSION" ]]; then
    echo "  Refreshing source ($CUR -> $OPENCLAW_VERSION)..."
    need_refresh=1
  fi
fi

if [[ "$need_refresh" -eq 1 ]]; then
  echo "  Downloading NemoClaw source tarball..."
  rm -rf "$NEMOCLAW_SRC"
  mkdir -p "$NEMOCLAW_SRC"
  curl -fsSL https://github.com/NVIDIA/NemoClaw/archive/refs/heads/main.tar.gz \
    | tar xz --strip-components=1 -C "$NEMOCLAW_SRC"
fi

# Pre-merge Guard blueprint
echo "  Pre-merging Guard blueprint..."
OFFICIAL_POLICIES="$NEMOCLAW_SRC/nemoclaw-blueprint/policies"
if [[ -d "$OFFICIAL_POLICIES" ]]; then
  mkdir -p "$PROJECT_DIR/nemoclaw-blueprint/policies"
  cp -rn "$OFFICIAL_POLICIES/"* "$PROJECT_DIR/nemoclaw-blueprint/policies/" 2>/dev/null || true
fi
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" "$NEMOCLAW_SRC/nemoclaw-blueprint/" 2>/dev/null || \
  cp -r "$PROJECT_DIR/nemoclaw-blueprint/"* "$NEMOCLAW_SRC/nemoclaw-blueprint/"

# Stage host-side OpenClaw config artifacts
echo "  Staging OpenClaw config artifacts..."
"$VENV_PYTHON" -m guard.cli onboard \
  --workspace "$PROJECT_DIR" \
  --sandbox-name openclaw-sandbox \
  --gateway-port "$GATEWAY_PORT"

export NEMOCLAW_REPO_ROOT="$NEMOCLAW_SRC"
bash "$NEMOCLAW_SRC/scripts/install.sh"

restore_keys
trap - EXIT

# Post-install: cluster container now exists — re-detect bridge IP and
# apply a corrected policy so the sandbox can reach the Guard gateway.
# On Docker Desktop (WSL), the k3s pods use 192.168.65.254 (Docker Desktop
# host-gateway), which differs from docker0 (172.17.0.1) and can only be
# reliably detected after the cluster container is running.
echo "  Applying network policy with correct allowed_ips..."
BRIDGE_IP="$(detect_docker_bridge_ip)"
if [[ -n "$BRIDGE_IP" ]]; then
  export GUARD_BRIDGE_ALLOWED_IPS="$BRIDGE_IP"
  echo "  Bridge IP: $BRIDGE_IP"
  "$VENV_PYTHON" -m guard.cli onboard \
    --workspace "$PROJECT_DIR" \
    --sandbox-name openclaw-sandbox \
    --gateway-port "$GATEWAY_PORT"
  SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-my-assistant}"
  if openshell policy set \
      --policy "$PROJECT_DIR/policies/openclaw-sandbox.yaml" \
      "$SANDBOX_NAME" --wait 2>&1; then
    echo "  OK Policy updated: allowed_ips=[$BRIDGE_IP]"
  else
    echo "  WARN: openshell policy set failed — approve host.openshell.internal:$GATEWAY_PORT manually."
  fi
else
  echo "  WARN: Bridge IP not detected — skipping policy update."
fi

# Reload PATH after install
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "ERROR: nemoclaw not found in PATH after install."
  echo "  Try: source ~/.bashrc && nemoclaw status"
  exit 1
fi

# Persist PATH
if ! grep -q "NemoClaw PATH setup" "$HOME/.bashrc"; then
  {
    echo ""
    echo "# NemoClaw PATH setup"
    echo 'export PATH="$HOME/.local/bin:$PATH"'
    echo '[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"'
    echo "# end NemoClaw PATH setup"
  } >> "$HOME/.bashrc"
fi

# ---------------------------------------------------------------------------
# [6/6] Set inference route to Guard
# ---------------------------------------------------------------------------

echo "[6/6] Setting inference route..."

BEST_CRED="OPENROUTER_API_KEY"
[[ -z "${OPENROUTER_API_KEY:-}" && -n "${OPENAI_API_KEY:-}" ]]    && BEST_CRED="OPENAI_API_KEY"
[[ -z "${OPENROUTER_API_KEY:-}" && -n "${ANTHROPIC_API_KEY:-}" ]] && BEST_CRED="ANTHROPIC_API_KEY"

openshell provider create --name guard --type openai \
  --credential "$BEST_CRED" \
  --config OPENAI_BASE_URL="http://host.openshell.internal:${GATEWAY_PORT}/v1" 2>/dev/null || \
openshell provider update guard \
  --credential "$BEST_CRED" \
  --config OPENAI_BASE_URL="http://host.openshell.internal:${GATEWAY_PORT}/v1"

FINAL_MODEL="${MODEL_ID:-nvidia/nemotron-3-super-120b-a12b:free}"
openshell inference set --provider guard --model "$FINAL_MODEL" --no-verify || true
echo "  OK Inference: $FINAL_MODEL via guard gateway"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "=========================================="
echo " WSL Installation Complete!"
echo "=========================================="
echo ""
echo "Verify:"
echo "  curl http://127.0.0.1:$GATEWAY_PORT/health"
echo "  openshell inference get"
echo "  nemoclaw my-assistant status"
echo ""
echo "Test inference:"
echo "  openshell sandbox exec --name my-assistant --no-tty --timeout 30 -- \\"
echo "    openclaw infer model run --prompt 'Say hello' --model openrouter/auto --json"
echo ""
echo "MCP rollout (separate phase):"
echo "  export GITHUB_MCP_TOKEN=<your-token>"
echo "  bash install_mcp_bridge.sh github --sandbox my-assistant"
echo ""
echo "Monitor: tail -f $LOGS_DIR/gateway.log"
