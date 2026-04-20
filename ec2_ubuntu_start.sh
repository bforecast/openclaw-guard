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

# ---------------------------------------------------------------------------
# Parallelize long-running prep: Docker setup, Python venv+pip, and NemoClaw
# source download are independent and together dominate prefix wall clock.
# ---------------------------------------------------------------------------

echo "[2/6] Ensuring Docker... (background)"
(
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
) &
PID_DOCKER=$!

echo "[3/6] Python environment... (background)"
(
  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install -q -e "$PROJECT_DIR"
) &
PID_VENV=$!

# Kick off NemoClaw source refresh decision + download in parallel as well.
# R3: Only parse Dockerfile.base when user explicitly overrode OPENCLAW_VERSION;
# otherwise trust the cache and the package.json presence check.
OPENCLAW_VERSION_EXPLICIT="${OPENCLAW_VERSION+set}"
NEMOCLAW_SRC="$HOME/.nemoclaw/source"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.4.2}"
REFRESH_NEMOCLAW_SOURCE="${REFRESH_NEMOCLAW_SOURCE:-auto}"

need_refresh=0
if [[ ! -f "$NEMOCLAW_SRC/package.json" ]]; then
  need_refresh=1
elif [[ "$REFRESH_NEMOCLAW_SOURCE" == "always" ]]; then
  need_refresh=1
elif [[ "$REFRESH_NEMOCLAW_SOURCE" == "auto" && -n "$OPENCLAW_VERSION_EXPLICIT" ]]; then
  CURRENT_SRC_OPENCLAW_VERSION=""
  if [[ -f "$NEMOCLAW_SRC/Dockerfile.base" ]]; then
    CURRENT_SRC_OPENCLAW_VERSION="$(sed -n 's/^ARG OPENCLAW_VERSION=//p' "$NEMOCLAW_SRC/Dockerfile.base" | head -n 1)"
  fi
  if [[ -n "$CURRENT_SRC_OPENCLAW_VERSION" && "$CURRENT_SRC_OPENCLAW_VERSION" != "$OPENCLAW_VERSION" ]]; then
    echo "  Cached NemoClaw source is OpenClaw $CURRENT_SRC_OPENCLAW_VERSION; refreshing to $OPENCLAW_VERSION..."
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

echo "[4/6] Loading API keys..."
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cat >&2 <<EOF
ERROR: $PROJECT_DIR/.env not found.

The installer needs upstream provider credentials BEFORE install.sh runs;
otherwise the script's L178 snapshot captures empty values and the tail
step 'openshell inference set --credential OPENROUTER_API_KEY' fails with
"requires local env var to be set to a non-empty value".

Copy .env.example to .env and fill at least OPENROUTER_API_KEY, then rerun.
If you already have .env on another machine, upload it first:
  cat /path/to/.env | ssh <host> 'cat > ~/guard/.env && chmod 600 ~/guard/.env'
EOF
  exit 1
fi
set -a
source "$PROJECT_DIR/.env"
set +a

echo "Configured keys:"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && echo "  OK OpenRouter" || echo "  MISSING OpenRouter"
[[ -n "${GITHUB_MCP_TOKEN:-}" ]] && echo "  OK GitHub MCP" || echo "  -- GitHub MCP (optional)"

# Fail-fast: at least one upstream provider key must be set. Without it,
# install.sh phase 4 gateway inference config still succeeds (uses
# COMPATIBLE_API_KEY=mock-key fallback on L171), sandbox is still created,
# but the post-install 'openshell inference set --credential OPENROUTER_API_KEY'
# step fails, and every later agent call returns 401 from the upstream. Catch
# it upfront rather than after 9 minutes of install.
if [[ -z "${OPENROUTER_API_KEY:-}${OPENAI_API_KEY:-}${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: no upstream provider key set in $PROJECT_DIR/.env" >&2
  echo "  Need at least one of: OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY" >&2
  exit 1
fi

if [[ -z "${GUARD_ADMIN_TOKEN:-}" ]]; then
  GUARD_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  echo "" >> "$PROJECT_DIR/.env"
  echo "GUARD_ADMIN_TOKEN=$GUARD_ADMIN_TOKEN" >> "$PROJECT_DIR/.env"
  export GUARD_ADMIN_TOKEN
fi

echo "[5/6] Starting Security Gateway on port $GATEWAY_PORT..."
echo "  Waiting for background Docker + venv jobs to complete..."
wait "$PID_DOCKER" || { echo "ERROR: Docker setup failed."; exit 1; }
wait "$PID_VENV"   || { echo "ERROR: Python venv/pip install failed."; exit 1; }
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

# Wait for the background NemoClaw tarball download (kicked off after step 1).
if [[ -n "${PID_TARBALL:-}" ]]; then
  echo "  Waiting for NemoClaw source download..."
  wait "$PID_TARBALL" || { echo "ERROR: NemoClaw source download failed."; exit 1; }
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

# ---------------------------------------------------------------------------
# Post-install policy apply:
# install.sh only reads the official nemoclaw-blueprint tree, so the
# Guard policy that `guard.cli onboard` wrote to
# $PROJECT_DIR/policies/openclaw-sandbox.yaml never reached the sandbox.
# Re-detect the bridge IP (cluster container is now running, so the more
# reliable host-gateway probe works), regenerate the policy with
# allowed_ips populated, and push it via `openshell policy set`. Without
# this step the sandbox -> Guard bridge is blocked by OpenShell's SSRF
# guard (which rejects any destination resolving to a private IP unless
# the IP is explicitly allow-listed in the policy).
# ---------------------------------------------------------------------------
echo "Applying network policy with correct allowed_ips..."
BRIDGE_IP="$(detect_docker_bridge_ip)"
if [[ -n "$BRIDGE_IP" ]]; then
  export GUARD_BRIDGE_ALLOWED_IPS="$BRIDGE_IP"
  echo "  Bridge IP: $BRIDGE_IP"
  "$VENV_PYTHON" -m guard.cli onboard \
    --workspace "$PROJECT_DIR" \
    --sandbox-name openclaw-sandbox \
    --gateway-port "$GATEWAY_PORT"
  SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-my-assistant}"
  # OpenShell 2026.4.2 rejects live-sandbox mutations to filesystem_policy
  # (include_workdir toggle, /sandbox moving between read_only/read_write,
  # etc.). Recent NemoClaw install.sh (post 2026-04-17) ships phase-8
  # policy presets (npm/pypi/huggingface/brew/brave) that replace the
  # blueprint policy with a minimal default whose filesystem section
  # diverges from Guard's onboard template, so `openshell policy set`
  # then fails 'InvalidArgument: cannot be changed on a live sandbox'.
  #
  # Fix: preserve whatever filesystem_policy install.sh left in live, and
  # only land our network_policies (bridge host + allowed_ips + MCP egress
  # hosts). `openshell policy get --full` prints a metadata header + a
  # YAML separator + the policy body, so we load_all and take the last
  # document.
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
# `install_mcp_bridge.sh <name>` needs a bridge record before it will
# activate; pre-registering here makes the MCP rollout phase a single
# command per bridge (`install_mcp_bridge.sh context7`) instead of two
# (`guard bridge add context7 && install_mcp_bridge.sh context7`).
# Idempotent: `guard bridge add` on an existing name is a no-op.
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
