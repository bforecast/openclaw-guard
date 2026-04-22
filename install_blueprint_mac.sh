#!/usr/bin/env bash
set -euo pipefail

# OpenClaw Guard - macOS (Mac Mini) installer
#
# Adapted from the validated install_blueprint_wsl.sh with macOS-specific handling:
#   - Homebrew for dependency management (no apt-get)
#   - Colima (headless) or Docker Desktop for Mac as Docker runtime
#   - No systemd — Colima/Docker Desktop manages the daemon
#   - NemoClaw source tarball path (bypasses the official nemoclaw.sh temp-dir/npm-link bug)
#   - blueprint pre-merge before install.sh (no second onboard cycle needed)
#   - PATH persistence in ~/.zshrc (macOS default shell since Catalina)
#
# After this script: run install_mcp_bridge.sh --all for MCP rollout.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
LOGS_DIR="$PROJECT_DIR/logs"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

# ---------------------------------------------------------------------------
# Sudo: verify passwordless sudo works (SSH sessions don't cache reliably).
# ---------------------------------------------------------------------------
if ! sudo -n true 2>/dev/null; then
  echo ""
  echo "ERROR: This installer needs passwordless sudo (macOS SSH sessions"
  echo "       don't cache sudo credentials reliably due to tty_tickets)."
  echo ""
  echo "Fix — run this once, then re-run the installer:"
  echo "  sudo bash -c \"echo '$(whoami) ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/$(whoami) && chmod 440 /etc/sudoers.d/$(whoami)\""
  echo ""
  echo "Or set NOPASSWD for specific commands only (more restrictive):"
  echo "  sudo visudo -f /etc/sudoers.d/$(whoami)"
  exit 1
fi

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

is_macos() {
  [[ "$(uname -s)" == "Darwin" ]]
}

detect_docker_bridge_ip() {
  if [[ -n "${GUARD_BRIDGE_ALLOWED_IPS:-}" ]]; then
    printf '%s\n' "$GUARD_BRIDGE_ALLOWED_IPS"; return
  fi
  local ip=""

  # On Docker Desktop for Mac, host.docker.internal resolves to the
  # VM gateway that containers use to reach the host.
  # We probe it the same way as WSL — using --add-host with host-gateway.
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

if ! is_macos; then
  echo "ERROR: This script is for macOS. For WSL use install_blueprint_wsl.sh."
  exit 1
fi

echo "=== OpenClaw Guard: macOS (Mac Mini) Installer ==="
echo "Project: $PROJECT_DIR"
echo

# ---------------------------------------------------------------------------
# [0/6] Dependencies (Homebrew)
# ---------------------------------------------------------------------------

echo "[0/6] Installing base dependencies..."

# Install Homebrew if not present
if ! command -v brew >/dev/null 2>&1; then
  echo "  Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add Homebrew to PATH for this session
  if [[ -f /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -f /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
fi

brew install curl git jq rsync expect || true
# Ensure Python 3 is available
if ! command -v python3 >/dev/null 2>&1; then
  brew install python@3
fi

# ---------------------------------------------------------------------------
# [1/6] Docker (Colima for headless SSH, Docker Desktop as fallback)
# ---------------------------------------------------------------------------

echo "[1/6] Ensuring Docker... (background)"

# Colima resource defaults (override via env if needed)
COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEMORY="${COLIMA_MEMORY:-8}"
COLIMA_DISK="${COLIMA_DISK:-60}"

(
  if docker ps >/dev/null 2>&1; then
    echo "  Docker already reachable."
  else
    # Path A: Colima installed → start it
    if command -v colima >/dev/null 2>&1; then
      echo "  Starting Colima..."
      colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEMORY" --disk "$COLIMA_DISK" 2>&1 || true
    # Path B: Docker Desktop installed → try to launch it
    elif [[ -d /Applications/Docker.app ]]; then
      echo "  Docker Desktop found. Attempting to start..."
      open -a Docker 2>/dev/null || true
    # Path C: Nothing installed → install Colima (headless-friendly)
    else
      echo "  No Docker runtime found. Installing Colima (headless Docker for macOS)..."
      brew install colima docker docker-compose
      echo "  Starting Colima..."
      colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEMORY" --disk "$COLIMA_DISK" 2>&1
    fi

    # Wait for Docker to become available (up to 90s)
    attempts=0
    while ! docker ps >/dev/null 2>&1 && (( attempts < 45 )); do
      sleep 2
      (( attempts++ ))
    done
    if ! docker ps >/dev/null 2>&1; then
      echo "ERROR: Docker daemon did not start within 90 seconds."
      if command -v colima >/dev/null 2>&1; then
        echo "  Try manually: colima start --cpu $COLIMA_CPU --memory $COLIMA_MEMORY --disk $COLIMA_DISK"
        echo "  Debug: colima status; colima log"
      else
        echo "  Install Colima (no GUI needed): brew install colima docker && colima start"
        echo "  Or install Docker Desktop (requires GUI): brew install --cask docker"
      fi
      exit 1
    fi
  fi
  echo "  OK Docker ready."
) &
PID_DOCKER=$!

# ---------------------------------------------------------------------------
# [2/6] Python venv + Guard install (parallel with Docker)
# ---------------------------------------------------------------------------

echo "[2/6] Python environment... (background)"
(
  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install -q --upgrade pip setuptools
  "$VENV_DIR/bin/pip" install -q -e "$PROJECT_DIR"
) &
PID_VENV=$!

# Kick off NemoClaw source refresh decision + download in parallel as well.
# R3: Only parse Dockerfile.base when user explicitly overrode OPENCLAW_VERSION.
OPENCLAW_VERSION_EXPLICIT="${OPENCLAW_VERSION+set}"
NEMOCLAW_SRC="$HOME/.nemoclaw/source"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.4.2}"
REFRESH_NEMOCLAW_SOURCE="${REFRESH_NEMOCLAW_SOURCE:-auto}"

need_refresh=0
[[ ! -f "$NEMOCLAW_SRC/package.json" ]] && need_refresh=1
[[ "$REFRESH_NEMOCLAW_SOURCE" == "always" ]] && need_refresh=1
if [[ "$REFRESH_NEMOCLAW_SOURCE" == "auto" && "$need_refresh" -eq 0 && -n "$OPENCLAW_VERSION_EXPLICIT" ]]; then
  CUR=""
  [[ -f "$NEMOCLAW_SRC/Dockerfile.base" ]] && \
    CUR="$(sed -n 's/^ARG OPENCLAW_VERSION=//p' "$NEMOCLAW_SRC/Dockerfile.base" | head -n1 || true)"
  if [[ -n "$CUR" && "$CUR" != "$OPENCLAW_VERSION" ]]; then
    echo "  Cached NemoClaw source is OpenClaw $CUR; refreshing to $OPENCLAW_VERSION..."
    need_refresh=1
  fi
fi

PID_TARBALL=""
if [[ "$need_refresh" -eq 1 ]]; then
  echo "  Downloading NemoClaw source tarball... (background)"
  (
    rm -rf "$NEMOCLAW_SRC"
    mkdir -p "$NEMOCLAW_SRC"
    curl -fsSL https://github.com/NVIDIA/NemoClaw/archive/refs/heads/main.tar.gz \
      | tar xz --strip-components=1 -C "$NEMOCLAW_SRC"
  ) &
  PID_TARBALL=$!
fi

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
echo "  Waiting for background Docker + venv jobs to complete..."
wait "$PID_DOCKER" || { echo "ERROR: Docker setup failed."; exit 1; }
wait "$PID_VENV"   || { echo "ERROR: Python venv/pip install failed."; exit 1; }
mkdir -p "$LOGS_DIR"

# Kill any existing process on the gateway port (macOS: use lsof instead of fuser)
lsof -ti :"$GATEWAY_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true

# macOS Application Firewall: allow inbound connections to the Guard Gateway
# python process. Without this, Colima VM (or Docker Desktop VM) connections
# to host:8090 may be silently blocked by the firewall.
GATEWAY_PYTHON_BIN="$(readlink -f "$VENV_PYTHON" 2>/dev/null || echo "$VENV_PYTHON")"
if command -v /usr/libexec/ApplicationFirewall/socketfilterfw >/dev/null 2>&1; then
  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$GATEWAY_PYTHON_BIN" 2>/dev/null || true
  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$GATEWAY_PYTHON_BIN" 2>/dev/null || true
fi

nohup "$VENV_PYTHON" -m guard.gateway > "$LOGS_DIR/gateway.log" 2>&1 &

if ! grep -q "host.openshell.internal" /etc/hosts; then
  echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts >/dev/null
  # Flush macOS DNS cache so the new entry is immediately visible
  sudo dscacheutil -flushcache 2>/dev/null || true
  sudo killall -HUP mDNSResponder 2>/dev/null || true
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

# Wait for the background NemoClaw tarball download (kicked off after step 0).
if [[ -n "${PID_TARBALL:-}" ]]; then
  echo "  Waiting for NemoClaw source download..."
  wait "$PID_TARBALL" || { echo "ERROR: NemoClaw source download failed."; exit 1; }
fi

# Pre-merge Guard blueprint
echo "  Pre-merging Guard blueprint..."
OFFICIAL_POLICIES="$NEMOCLAW_SRC/nemoclaw-blueprint/policies"
if [[ -d "$OFFICIAL_POLICIES" ]]; then
  mkdir -p "$PROJECT_DIR/nemoclaw-blueprint/policies"
  # macOS cp: -n means no-clobber (same as GNU cp -n, supported since macOS 10.6)
  cp -Rn "$OFFICIAL_POLICIES/"* "$PROJECT_DIR/nemoclaw-blueprint/policies/" 2>/dev/null || true
fi
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" "$NEMOCLAW_SRC/nemoclaw-blueprint/" 2>/dev/null || \
  cp -R "$PROJECT_DIR/nemoclaw-blueprint/"* "$NEMOCLAW_SRC/nemoclaw-blueprint/"

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
# On Docker Desktop for Mac, the k3s pods use the Docker Desktop VM gateway
# (typically 192.168.65.254), which can only be reliably detected after the
# cluster container is running.
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
  # See ec2_ubuntu_start.sh for rationale. Recent NemoClaw install.sh
  # (post 2026-04-17) phase-8 presets diverge live filesystem_policy from
  # Guard's onboard template, and OpenShell 2026.4.2 rejects live
  # mutations to filesystem_policy. Preserve live filesystem, splice
  # Guard network_policies only.
  LIVE_POLICY_FILE="$PROJECT_DIR/policies/_live_policy.yaml"
  GUARD_POLICY_FILE="$PROJECT_DIR/policies/openclaw-sandbox.yaml"
  if openshell policy get "$SANDBOX_NAME" --full > "$LIVE_POLICY_FILE" 2>/dev/null; then
    "$VENV_PYTHON" - "$LIVE_POLICY_FILE" "$GUARD_POLICY_FILE" <<'PYEOF'
import sys, yaml
live_path, guard_path = sys.argv[1], sys.argv[2]
docs = list(yaml.safe_load_all(open(live_path)))
live = next((d for d in reversed(docs) if isinstance(d, dict) and "filesystem_policy" in d), None)
guard = yaml.safe_load(open(guard_path))
if live is not None:
    guard["filesystem_policy"] = live["filesystem_policy"]
    with open(guard_path, "w") as f:
        yaml.safe_dump(guard, f, sort_keys=False)
    print(f"  Spliced live filesystem_policy (include_workdir={live['filesystem_policy'].get('include_workdir')})")
PYEOF
  fi
  if openshell policy set \
      --policy "$GUARD_POLICY_FILE" \
      "$SANDBOX_NAME" --wait 2>&1; then
    echo "  OK Policy updated: allowed_ips=[$BRIDGE_IP]"
  else
    echo "  WARN: openshell policy set failed — approve host.openshell.internal:$GATEWAY_PORT manually."
  fi
else
  echo "  WARN: Bridge IP not detected — skipping policy update."
fi

# ---------------------------------------------------------------------------
# Pre-register bridge records for every approved MCP server in gateway.yaml.
# See ec2_ubuntu_start.sh for rationale.
# ---------------------------------------------------------------------------
echo "Pre-registering bridge records for approved MCP servers..."
SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-my-assistant}"
APPROVED_MCPS="$(
  "$VENV_PYTHON" - "$PROJECT_DIR/gateway.yaml" <<'PYEOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
for s in (cfg.get("mcp", {}) or {}).get("servers", []) or []:
    if (s.get("status") or "").lower() == "approved" and s.get("name"):
        print(s["name"])
PYEOF
)"
if [[ -n "$APPROVED_MCPS" ]]; then
  for MCP_NAME in $APPROVED_MCPS; do
    "$VENV_PYTHON" -m guard.cli bridge add "$MCP_NAME" \
      --sandbox "$SANDBOX_NAME" \
      --workspace "$PROJECT_DIR" \
      --gateway "http://127.0.0.1:$GATEWAY_PORT" >/dev/null 2>&1 \
      && echo "  OK registered: $MCP_NAME" \
      || echo "  WARN: could not register $MCP_NAME (check gateway or token)"
  done
else
  echo "  (no approved MCP servers in gateway.yaml)"
fi

# Reload PATH after install
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "ERROR: nemoclaw not found in PATH after install."
  echo "  Try: source ~/.zshrc && nemoclaw status"
  exit 1
fi

# Persist PATH in ~/.zshrc (macOS default shell since Catalina)
SHELL_RC="$HOME/.zshrc"
# Also handle users who use bash
if [[ "$(basename "$SHELL")" == "bash" ]]; then
  SHELL_RC="$HOME/.bash_profile"
fi

if ! grep -q "NemoClaw PATH setup" "$SHELL_RC" 2>/dev/null; then
  {
    echo ""
    echo "# NemoClaw PATH setup"
    echo 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"'
    echo '[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"'
    echo "# end NemoClaw PATH setup"
  } >> "$SHELL_RC"
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
echo " macOS (Mac Mini) Installation Complete!"
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
