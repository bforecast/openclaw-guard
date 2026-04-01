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
head -n 8 "$POLICY_PATH"

echo
echo "[4/7] Ensuring OpenShell gateway reset for onboarding..."
openshell gateway destroy --name openshell >/dev/null 2>&1 || true

echo
echo "[5/7] Running NemoClaw onboarding..."
cd "$PROJECT_DIR"
if [[ -n "${NVIDIA_API_KEY:-}" ]]; then
  nemoclaw onboard --non-interactive
else
  echo "NVIDIA_API_KEY is not set. Running interactive onboard..."
  nemoclaw onboard
fi

echo
echo "[6/7] Forcing inference route to guard-gateway (no injection)..."
OPENSHELL_GATEWAY="$OPENSHELL_GATEWAY_NAME" openshell provider delete guard-gateway >/dev/null 2>&1 || true
OPENSHELL_GATEWAY="$OPENSHELL_GATEWAY_NAME" openshell provider create \
  --name guard-gateway \
  --type openai \
  --credential OPENAI_API_KEY=guard-managed \
  --config OPENAI_BASE_URL="http://host.openshell.internal:${GATEWAY_PORT}/v1"

OPENSHELL_GATEWAY="$OPENSHELL_GATEWAY_NAME" openshell inference set \
  --provider guard-gateway \
  --model "$MODEL_ID" \
  --no-verify
OPENSHELL_GATEWAY="$OPENSHELL_GATEWAY_NAME" openshell inference get || true

echo
echo "[7/7] Final status..."
nemoclaw status || true
nemoclaw list || true

echo
echo "Done."
echo "Gateway log: $PROJECT_DIR/logs/gateway.log"
echo "Connect sandbox: nemoclaw <sandbox-name> connect"
echo "Verify route: send 'hi' in openclaw tui, then check gateway log for POST /v1/responses"
