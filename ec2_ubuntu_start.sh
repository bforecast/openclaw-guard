#!/usr/bin/env bash
set -euo pipefail

# ===========================================================================
# OpenClaw Guard - EC2 Deployment Script
#
# Tested flow:
#   Sandbox (OpenClaw 2026.4.10)
#     -> inference.local (allowPrivateNetwork)
#       -> OpenShell provider "guard"
#         -> Guard Gateway (:8090)
#           -> OpenRouter
#
# MCP flow:
#   Sandbox -> direct HTTPS to MCP hosts (api.githubcopilot.com, etc.)
#   Network presets applied via `nemoclaw policy-add`
# ===========================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
LOGS_DIR="$PROJECT_DIR/logs"

export PATH="$HOME/.local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

echo "[0/9] System Pre-checks..."
df -h | grep '^/dev/' || true
free -m

# ── 1. Base Dependencies ─────────────────────────────────────────────────
echo "[1/9] Installing base dependencies..."
sudo apt-get update -y && sudo apt-get install -y \
  ca-certificates curl git jq lsof psmisc expect \
  python3 python3-pip python3-venv

# ── 2. Docker ─────────────────────────────────────────────────────────────
echo "[2/9] Ensuring Docker..."
sudo systemctl start docker || true
sudo chmod 666 /var/run/docker.sock || true
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker access failed."
  exit 1
fi

# ── 3. Python Environment ────────────────────────────────────────────────
echo "[3/9] Python environment..."
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv --system-site-packages "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -e "$PROJECT_DIR"

# ── 4. Load Keys ─────────────────────────────────────────────────────────
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a && source "$PROJECT_DIR/.env" && set +a
fi

echo "Configured Keys:"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && echo "  OK OpenRouter" || echo "  MISSING OpenRouter"
[[ -n "${GITHUB_MCP_TOKEN:-}" ]]   && echo "  OK GitHub MCP" || echo "  -- GitHub MCP (optional)"

# Generate admin token (needed for MCP registration)
if [[ -z "${GUARD_ADMIN_TOKEN:-}" ]]; then
    GUARD_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo "" >> "$PROJECT_DIR/.env"
    echo "GUARD_ADMIN_TOKEN=$GUARD_ADMIN_TOKEN" >> "$PROJECT_DIR/.env"
    export GUARD_ADMIN_TOKEN
fi

# ── 5. NemoClaw Installation ─────────────────────────────────────────────
echo "[4/9] Installing NemoClaw CLI..."
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1

NEMOCLAW_SRC="$HOME/.nemoclaw/source"
if [ ! -f "$NEMOCLAW_SRC/package.json" ]; then
    echo "Downloading NemoClaw source..."
    rm -rf "$NEMOCLAW_SRC"
    mkdir -p "$NEMOCLAW_SRC"
    curl -fsSL https://github.com/NVIDIA/NemoClaw/archive/refs/heads/main.tar.gz \
        | tar xz --strip-components=1 -C "$NEMOCLAW_SRC"
fi

# Pre-merge Guard blueprint
echo "Pre-merging Guard Blueprint..."
OFFICIAL_POLICIES="$NEMOCLAW_SRC/nemoclaw-blueprint/policies"
if [ -d "$OFFICIAL_POLICIES" ]; then
    mkdir -p "$PROJECT_DIR/nemoclaw-blueprint/policies"
    cp -rn "$OFFICIAL_POLICIES/"* "$PROJECT_DIR/nemoclaw-blueprint/policies/" 2>/dev/null || true
fi
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" "$NEMOCLAW_SRC/nemoclaw-blueprint/" 2>/dev/null || \
    cp -r "$PROJECT_DIR/nemoclaw-blueprint/"* "$NEMOCLAW_SRC/nemoclaw-blueprint/"

# OpenClaw 2026.4.10 base image
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.4.10}"
echo "Building sandbox base image with openclaw@${OPENCLAW_VERSION}..."
sudo apt-get install -y -q docker-buildx 2>/dev/null || true
export DOCKER_BUILDKIT=1
docker build --build-arg OPENCLAW_VERSION="$OPENCLAW_VERSION" \
    -f "$NEMOCLAW_SRC/Dockerfile.base" \
    -t ghcr.io/nvidia/nemoclaw/sandbox-base:latest \
    "$NEMOCLAW_SRC"

# Run official installer
export NEMOCLAW_REPO_ROOT="$NEMOCLAW_SRC"
bash "$NEMOCLAW_SRC/scripts/install.sh"

# Reload nvm
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "ERROR: NemoClaw not found in PATH."
  exit 1
fi

# ── 6. Start Security Gateway ─────────────────────────────────────────────
echo "[5/9] Starting Security Gateway on port $GATEWAY_PORT..."
mkdir -p "$LOGS_DIR"
sudo fuser -k "$GATEWAY_PORT/tcp" 2>/dev/null || true
nohup "$VENV_PYTHON" -m guard.gateway > "$LOGS_DIR/gateway.log" 2>&1 &

for _ in {1..15}; do
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    echo "  OK Gateway ready."
    break
  fi
  sleep 1
done

# ── 7. Register MCP Servers (BEFORE onboard, so openclaw.json includes them) ─
echo "[6/9] Registering MCP servers..."
if [[ -n "${GITHUB_MCP_TOKEN:-}" ]]; then
    curl -sf -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/mcp/servers" \
      -H "Authorization: Bearer $GUARD_ADMIN_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"name":"github","url":"https://api.githubcopilot.com/mcp/","transport":"streamable_http","credential_env":"GITHUB_MCP_TOKEN","purpose":"GitHub MCP (repos, issues, PRs, code search)"}' >/dev/null 2>&1 || true
    curl -sf -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/mcp/servers/github/approve" \
      -H "Authorization: Bearer $GUARD_ADMIN_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"actor":"admin"}' >/dev/null 2>&1 || true
    echo "  OK GitHub MCP registered + approved"
else
    echo "  SKIP: GITHUB_MCP_TOKEN not set"
fi

# ── 8. Guard Onboard (generates openclaw.json + auth + policy) ────────────
echo "[7/9] Generating sandbox config (guard onboard)..."
export NEMOCLAW_ENDPOINT_URL="http://localhost:$GATEWAY_PORT/v1"
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_MODEL="${MODEL_ID:-nvidia/nemotron-3-super-120b-a12b:free}"
export COMPATIBLE_API_KEY="${OPENROUTER_API_KEY:-${OPENAI_API_KEY:-mock-key}}"

# guard onboard writes:
#   sandbox_workspace/openclaw/openclaw.json   (ro mount: apiKey + allowPrivateNetwork + MCP)
#   sandbox_workspace/openclaw-data/agents/    (rw mount: auth-profiles.json)
#   policies/openclaw-sandbox.yaml             (network policy)
"$VENV_PYTHON" -m guard onboard --workspace "$PROJECT_DIR" --gateway-port "$GATEWAY_PORT"

# ── 8b. NemoClaw Onboard (creates sandbox, mounts the dirs above) ────────
echo "Running NemoClaw onboard..."
nemoclaw onboard --non-interactive --yes-i-accept-third-party-software

# ── 8c. Upload auth data to rw mount ─────────────────────────────────────
# The ro mount (openclaw.json) is set at sandbox creation from sandbox_workspace/openclaw/.
# The rw mount symlink targets need the agents data uploaded post-creation.
openshell sandbox upload my-assistant \
    "$PROJECT_DIR/sandbox_workspace/openclaw-data/agents" \
    /sandbox/.openclaw-data/agents 2>&1 || true
echo "  OK Auth data uploaded to sandbox"

# ── 8d. Inference Route ──────────────────────────────────────────────────
echo "[8/9] Setting inference route..."
BEST_CRED="OPENROUTER_API_KEY"
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    if [[ -n "${OPENAI_API_KEY:-}" ]]; then BEST_CRED="OPENAI_API_KEY";
    elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then BEST_CRED="ANTHROPIC_API_KEY";
    fi
fi

HOST_ADDR="host.openshell.internal"
openshell provider create --name guard --type openai \
    --credential "$BEST_CRED" \
    --config OPENAI_BASE_URL=http://${HOST_ADDR}:${GATEWAY_PORT}/v1 || \
openshell provider update guard \
    --credential "$BEST_CRED" \
    --config OPENAI_BASE_URL=http://${HOST_ADDR}:${GATEWAY_PORT}/v1

FINAL_MODEL="${NEMOCLAW_MODEL:-nvidia/nemotron-3-super-120b-a12b:free}"
openshell inference set --provider guard --model "$FINAL_MODEL" --no-verify || true
echo "  OK Inference: $FINAL_MODEL via guard gateway"

# ── 8e. Apply base network policy ────────────────────────────────────────
openshell policy set --policy "$PROJECT_DIR/policies/openclaw-sandbox.yaml" \
    my-assistant --wait 2>&1 || true
echo "  OK Base network policy applied"

# ── 8f. Install MCP network presets via nemoclaw policy-add ──────────────
# openshell policy set silently drops `access:full` entries.
# NemoClaw presets are the correct way to allowlist MCP hosts.
echo "Installing MCP network presets..."
PRESET_DIR="$HOME/.nemoclaw/source/nemoclaw-blueprint/policies/presets"
mkdir -p "$PRESET_DIR"

if [[ -n "${GITHUB_MCP_TOKEN:-}" ]]; then
    # Write mcp_github preset (if the official one is broken or missing)
    cat > "$PRESET_DIR/mcp_github.yaml" << 'PRESETEOF'
network_policies:
  mcp_github:
    name: MCP GitHub upstream
    endpoints:
    - host: api.githubcopilot.com
      port: 443
      access: full
    - host: api.github.com
      port: 443
      access: full
    binaries:
    - path: /usr/local/bin/openclaw
    - path: /usr/local/bin/node
PRESETEOF

    # Find the preset number dynamically and apply via expect
    PRESET_NUM=$(nemoclaw my-assistant policy-list 2>&1 \
        | grep -n "mcp_github" | head -1 | sed 's/^.*\b\([0-9]\+\)) .* mcp_github.*/\1/' || true)

    if [[ -n "$PRESET_NUM" ]]; then
        expect -c "
            spawn nemoclaw my-assistant policy-add
            expect \"Choose preset\"
            send \"$PRESET_NUM\r\"
            expect {
                \"Apply\" { send \"y\r\"; exp_continue }
                \"already applied\" { }
                eof
            }
        " 2>&1 || true
        echo "  OK mcp_github preset applied"
    else
        echo "  WARN: mcp_github preset not found in list"
    fi
fi

# ── 9. Completion ─────────────────────────────────────────────────────────
echo "[9/9] Deployment Complete."
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
echo "Test GitHub MCP:"
echo "  openshell sandbox exec --name my-assistant --no-tty --timeout 60 -- \\"
echo "    openclaw infer model run --prompt 'Use github MCP to describe torvalds/linux' --model openrouter/auto --json"
echo ""
echo "Monitor: tail -f $LOGS_DIR/gateway.log"
