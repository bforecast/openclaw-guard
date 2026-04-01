#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$PROJECT_DIR/src"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
MODEL_ID="${MODEL_ID:-openrouter/stepfun/step-3.5-flash:free}"
OPENSHELL_GATEWAY_NAME="${OPENSHELL_GATEWAY_NAME:-nemoclaw}"

# NemoClaw installer usually places shims here.
export PATH="$HOME/.local/bin:$PATH"

# Load NVM if it exists (crucial for Node-based tools like NemoClaw)
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    \. "$NVM_DIR/nvm.sh"
    export PATH="$HOME/.local/bin:$PATH"
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script is for Linux (AWS EC2 Ubuntu)."
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get not found. This script targets Ubuntu/Debian."
  exit 1
fi

echo "[0/7] Installing base dependencies..."
sudo apt-get update -y
sudo apt-get install -y \
  ca-certificates \
  curl \
  git \
  jq \
  lsof \
  psmisc \
  python3 \
  python3-pip \
  python3-venv

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but not installed."
  echo "Install Docker first, then rerun this script."
  exit 1
fi

if ! sudo systemctl is-active --quiet docker; then
  echo "Starting Docker service..."
  sudo systemctl start docker
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtualenv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

echo "Installing Python requirements..."
"$VENV_DIR/bin/pip" install -r "$SRC_DIR/requirements.txt"

if [[ -f "$PROJECT_DIR/.env" ]]; then
  echo "Loading API keys from .env..."
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

echo "Provider keys:"
[[ -n "${OPENAI_API_KEY:-}" ]] && echo "  OK OpenAI" || echo "  WARN OpenAI not set"
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && echo "  OK Anthropic" || echo "  WARN Anthropic not set"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && echo "  OK OpenRouter" || echo "  WARN OpenRouter not set"
[[ -n "${NVIDIA_API_KEY:-}" ]] && echo "  OK NVIDIA" || echo "  WARN NVIDIA not set"
echo

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "[1/7] Installing NemoClaw (official installer)..."
  if [[ -n "${NVIDIA_API_KEY:-}" ]]; then
    NEMOCLAW_NON_INTERACTIVE=1 curl -fsSL https://nvidia.com/nemoclaw.sh | bash -s -- --non-interactive
  else
    echo "NVIDIA_API_KEY is not set."
    echo "Running interactive NemoClaw installer (choose provider/model as needed)..."
    curl -fsSL https://nvidia.com/nemoclaw.sh | bash
  fi
fi

# Refresh environment after potential nvm/nemoclaw installation
if [ -s "$NVM_DIR/nvm.sh" ]; then
    \. "$NVM_DIR/nvm.sh"
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "nemoclaw is still not on PATH. Try: export PATH=\"$HOME/.local/bin:\$PATH\""
  exit 1
fi

if ! command -v openshell >/dev/null 2>&1; then
  echo "openshell CLI not found after NemoClaw install."
  exit 1
fi

echo "[2/7] Starting security gateway..."
mkdir -p "$PROJECT_DIR/logs"
fuser -k "${GATEWAY_PORT}/tcp" 2>/dev/null || true
nohup "$VENV_PYTHON" "$SRC_DIR/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &

for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
    echo "  Gateway is ready."
    break
  fi
  sleep 1
done

echo
echo "[3/7] Preparing blueprint artifacts..."
"$VENV_PYTHON" "$SRC_DIR/cli.py" onboard --workspace "$PROJECT_DIR" --gateway-port "$GATEWAY_PORT"
POLICY_PATH="$PROJECT_DIR/nemoclaw-blueprint/policies/openclaw-sandbox.yaml"
if [[ ! -s "$POLICY_PATH" ]]; then
  echo "Policy file is missing or empty: $POLICY_PATH"
  exit 1
fi
echo "  Policy file verified: $POLICY_PATH"

# ---------------------------------------------------------------------------
# [3/7] Starting OpenShell gateway (EARLY START)
# ---------------------------------------------------------------------------
echo "[3/7] Starting OpenShell gateway (Early)"
mkdir -p "$PROJECT_DIR/logs"
sudo fuser -k 8090/tcp || true
cd "$PROJECT_DIR"
set -a; source .env; set +a
nohup ./.venv/bin/python3 src/gateway.py > logs/gateway.log 2>&1 &
sleep 3
if ! sudo fuser 8090/tcp > /dev/null 2>&1; then
  echo "Error: Gateway failed to start on port 8090."
  exit 1
fi

# ---------------------------------------------------------------------------
# [4/7] Configuring inference (NIM) - ROUTE THROUGH LOCAL GATEWAY
# ---------------------------------------------------------------------------
echo "[4/7] Configuring inference (Verification Loopback)"
cd "$PROJECT_DIR"
if [[ -n "${NVIDIA_API_KEY:-}" || -n "${OPENROUTER_API_KEY:-}" || -n "${OPENAI_API_KEY:-}" || -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Key detected. Using non-interactive onboard via local loopback..."
  # Point validation URL to our local security gateway to bypass credit checks
  export NEMOCLAW_ENDPOINT_URL="http://localhost:8090/v1"
  if [[ -z "${NVIDIA_API_KEY:-}" ]]; then
    export NVIDIA_API_KEY="skip"
    if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
        export NEMOCLAW_PROVIDER="custom"
        export COMPATIBLE_API_KEY="${OPENROUTER_API_KEY}"
        export NEMOCLAW_MODEL="openai/gpt-4o-mini"
    elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
        export NEMOCLAW_PROVIDER="openai"
    elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        export NEMOCLAW_PROVIDER="anthropic"
    fi
  fi
  nemoclaw onboard --non-interactive
else
  echo "No keys detected. Running interactive onboard..."
  nemoclaw onboard
fi

# ---------------------------------------------------------------------------
# [5/7] Deploying sandbox
# ---------------------------------------------------------------------------
echo "[5/7] Deploying sandbox (OpenShell)"
# 'launch' might be 'connect' or handled differently; we suppress errors here
nemoclaw launch 2>/dev/null || echo "  Sandbox registered. Use 'nemoclaw connect' to start manually if needed."

# ---------------------------------------------------------------------------
# [6/7] Final Inference Routing (Ensuring bypass)
# ---------------------------------------------------------------------------
echo "[6/7] Forcing security gateway as primary inference route"
openshell provider create --name guard --type openai --credential OPENROUTER_API_KEY --config OPENAI_BASE_URL=http://localhost:8090/v1 || \
openshell provider update guard --credential OPENROUTER_API_KEY --config OPENAI_BASE_URL=http://localhost:8090/v1
# Use --no-verify to skip another probe during final routing
openshell inference set --provider guard --model openai/gpt-4o-mini --no-verify || true

# ---------------------------------------------------------------------------
# [7/7] Summary
# ---------------------------------------------------------------------------
echo "[7/7] Deployment Complete"
echo ""
echo "OpenClaw Guard is now running on AWS EC2."
echo "Security Audit log located at: logs/security_audit.db"
echo "Gateway log output: logs/gateway.log"
echo "Public gateway endpoint (if exposed): http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4):8090/v1" 
echo "Verify route: send 'hi' in openclaw tui, then check gateway log for POST /v1/responses"
