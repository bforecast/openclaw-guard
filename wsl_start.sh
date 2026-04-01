#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$PROJECT_DIR/src"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
MODEL_ID="${MODEL_ID:-openrouter/stepfun/step-3.5-flash:free}"
OPENSHELL_GATEWAY_NAME="${OPENSHELL_GATEWAY_NAME:-nemoclaw}"

# NemoClaw installer places the CLI in ~/.local/bin by default.
export PATH="$HOME/.local/bin:$PATH"

if ! grep -qi "microsoft" /proc/version 2>/dev/null; then
  echo "This script is WSL-Ubuntu only."
  echo "Open a WSL terminal and run it there."
  exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Virtualenv Python not found at $VENV_PYTHON"
  echo "Create the venv in WSL first, then install dependencies:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r src/requirements.txt"
  exit 1
fi

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "nemoclaw is not installed or not on PATH."
  echo "Install it first, then rerun this script."
  exit 1
fi

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
echo

echo "[1/5] Starting security gateway..."
mkdir -p "$PROJECT_DIR/logs"
fuser -k "${GATEWAY_PORT}/tcp" 2>/dev/null || true
nohup "$VENV_PYTHON" "$SRC_DIR/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &
GATEWAY_PID=$!

for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
    echo "  Gateway is ready."
    break
  fi
  sleep 1
done

echo
echo "[2/5] Preparing blueprint artifacts..."
"$VENV_PYTHON" "$SRC_DIR/cli.py" onboard --workspace "$PROJECT_DIR" --gateway-port "$GATEWAY_PORT"
POLICY_PATH="$PROJECT_DIR/nemoclaw-blueprint/policies/openclaw-sandbox.yaml"
if [[ ! -s "$POLICY_PATH" ]]; then
  echo "Policy file is missing or empty: $POLICY_PATH"
  exit 1
fi
echo "  Policy file verified: $POLICY_PATH"
head -n 8 "$POLICY_PATH"

echo
echo "[3/5] Resetting OpenShell gateway for NemoClaw onboarding..."
openshell gateway destroy --name openshell >/dev/null 2>&1 || true
echo
echo "[4/5] Running NemoClaw onboarding..."
# Ensure the project blueprint is seen by the onboard command
if [[ ! -f "blueprint.yaml" && -f "nemoclaw-blueprint/blueprint.yaml" ]]; then
  ln -sf "nemoclaw-blueprint/blueprint.yaml" .
fi
cd "$PROJECT_DIR"
if [[ -n "${NVIDIA_API_KEY:-}" ]]; then
  nemoclaw onboard --non-interactive
else
  echo "NVIDIA_API_KEY is not set."
  echo "Run this manually and choose your provider (for example OpenRouter):"
  echo "  nemoclaw onboard"
  echo
  echo "After onboarding completes, rerun ./wsl_start.sh"
  exit 1
fi

echo
echo "[5/6] Switching OpenShell inference to guard-gateway..."
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
echo "[6/6] Checking NemoClaw status..."
nemoclaw status || true
nemoclaw list || true

echo
echo "Done."
echo "Gateway log: $PROJECT_DIR/logs/gateway.log"
echo "Sandbox connect: nemoclaw <sandbox-name> connect"
