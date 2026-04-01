#!/usr/bin/env bash
set -euo pipefail

# ===========================================================================
# OpenClaw Guard - Robust EC2 Deployment Script (Clean Version)
# ===========================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$PROJECT_DIR/src"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
LOGS_DIR="$PROJECT_DIR/logs"

# Ensure NemoClaw and Node are on PATH
export PATH="$HOME/.local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

echo "[0/7] System Pre-checks & Resource Audit..."
df -h | grep '^/dev/' || true
free -m

# 1. Install Dependencies
echo "[1/7] Installing base dependencies..."
sudo apt-get update -y && sudo apt-get install -y \
  ca-certificates curl git jq lsof psmisc python3 python3-pip python3-venv

# 2. Docker Setup & Permission Fix
echo "[2/7] Ensuring Docker service and permissions..."
sudo systemctl start docker || true
sudo chmod 666 /var/run/docker.sock || true
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker access failed. Check 'sudo systemctl status docker'"
  exit 1
fi

# 3. Python Environment
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -r "$SRC_DIR/requirements.txt"

# 4. Load & Validate Keys
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a && source "$PROJECT_DIR/.env" && set +a
fi

echo "Configured Keys:"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && echo "  ✔ OpenRouter" || echo "  ✖ OpenRouter MISSING"
[[ -n "${OPENAI_API_KEY:-}" ]] && echo "  ✔ OpenAI" || echo "  ✖ OpenAI MISSING"

# 5. NemoClaw Installation
if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "[3/7] Installing NemoClaw CLI..."
  # Use non-interactive simple pass for installation
  curl -fsSL https://nvidia.com/nemoclaw.sh | bash -s -- --non-interactive || \
  curl -fsSL https://nvidia.com/nemoclaw.sh | bash
fi

# Final PATH check
export PATH="$HOME/.local/bin:$PATH"
if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "ERROR: NemoClaw not found in PATH."
  exit 1
fi

# 6. Start Security Gateway (Early)
echo "[4/7] Starting Security Gateway on port $GATEWAY_PORT..."
mkdir -p "$LOGS_DIR"
sudo fuser -k "$GATEWAY_PORT/tcp" 2>/dev/null || true
nohup "$VENV_PYTHON" "$SRC_DIR/gateway.py" > "$LOGS_DIR/gateway.log" 2>&1 &

echo "Waiting for gateway health check..."
for i in {1..15}; do
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    echo "  ✔ Gateway Ready."
    break
  fi
  sleep 1
done

# 7. Non-Interactive Onboarding (Loopback)
echo "[5/7] Running NemoClaw Onboarding (Loopback Bypass)..."
# Route validation through our gateway to avoid credit errors
export NEMOCLAW_ENDPOINT_URL="http://localhost:$GATEWAY_PORT/v1"
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_MODEL="stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="${OPENROUTER_API_KEY:-${OPENAI_API_KEY:-mock-key}}"

# Generate blueprint
"$VENV_PYTHON" "$SRC_DIR/cli.py" onboard --workspace "$PROJECT_DIR" --gateway-port "$GATEWAY_PORT"

# Run onboarding
nemoclaw onboard --non-interactive --workspace "$PROJECT_DIR"

# 8. Final Inference Setup (The "Golden" Route)
echo "[6/7] Final Inference Routing (Ensuring bypass)..."
BEST_CRED="OPENROUTER_API_KEY"
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    if [[ -n "${OPENAI_API_KEY:-}" ]]; then BEST_CRED="OPENAI_API_KEY";
    elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then BEST_CRED="ANTHROPIC_API_KEY";
    fi
fi

# Use host.openshell.internal for Docker-to-Host bridge
HOST_ADDR="host.openshell.internal"
openshell provider create --name guard --type openai --credential "$BEST_CRED" --config OPENAI_BASE_URL=http://${HOST_ADDR}:${GATEWAY_PORT}/v1 || \
openshell provider update guard --credential "$BEST_CRED" --config OPENAI_BASE_URL=http://${HOST_ADDR}:${GATEWAY_PORT}/v1

# THE FINAL MODEL SETTING (Hardcoded StepFun to avoid GPT-4o-mini residue)
FINAL_MODEL="stepfun/step-3.5-flash:free"
if [[ "$BEST_CRED" == "ANTHROPIC_API_KEY" ]]; then FINAL_MODEL="claude-3-5-sonnet-20240620"; fi
# Overwrite if user explicitly provided a model in ENV
if [[ -n "${NEMOCLAW_MODEL:-}" ]]; then FINAL_MODEL="$NEMOCLAW_MODEL"; fi

echo "  Setting inference route to: $FINAL_MODEL"
openshell inference set --provider guard --model "$FINAL_MODEL" --no-verify || true

# 9. Completion & Persistence
echo "[7/7] Deployment Complete."
# Add NemoClaw to .bashrc if not already there for interactive use
if ! grep -q ".local/bin" "$HOME/.bashrc"; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

echo ""
echo "Deployment Finished!"
echo "Run 'source ~/.bashrc' or logout/login to use 'nemoclaw' directly."
echo "Test connection: nemoclaw connect"
echo "Monitor logs: tail -f logs/gateway.log"
