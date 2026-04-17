#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
LOGS_DIR="$PROJECT_DIR/logs"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"

export PATH="$HOME/.local/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

echo "[0/4] Checking system dependencies..."
sudo apt-get update -y -q
sudo apt-get install -y -q \
  ca-certificates curl git jq lsof psmisc \
  python3 python3-pip python3-venv docker.io

echo "[1/4] Ensuring Docker..."
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker
sudo chmod 666 /var/run/docker.sock || true
if ! docker ps >/dev/null 2>&1; then
  echo "ERROR: Docker socket still inaccessible."
  exit 1
fi

echo "[2/4] Preparing Guard gateway..."
rm -rf "$VENV_DIR"
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_PYTHON" -m pip install -q --upgrade pip setuptools
"$VENV_PYTHON" -m pip install -q -e "$PROJECT_DIR"

mkdir -p "$LOGS_DIR"
lsof -t -i :$GATEWAY_PORT | xargs kill -9 2>/dev/null || true
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  source "$PROJECT_DIR/.env"
  set +a
fi
nohup "$VENV_PYTHON" -m guard.gateway > "$LOGS_DIR/gateway.log" 2>&1 &

for _ in {1..15}; do
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! grep -q "host.openshell.internal" /etc/hosts; then
  echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts >/dev/null
fi

echo "[3/4] Running NemoClaw installer..."
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:${GATEWAY_PORT}/v1"
export NEMOCLAW_MODEL="${MODEL_ID:-nvidia/nemotron-3-super-120b-a12b:free}"
export COMPATIBLE_API_KEY="${OPENROUTER_API_KEY:-guard-managed}"
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
unset NVIDIA_API_KEY
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash

echo "[4/4] Synchronizing blueprint..."
mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint
OFFICIAL_SOURCE="$HOME/.nemoclaw/source"
if [[ -d "$OFFICIAL_SOURCE/nemoclaw-blueprint/policies/presets" ]]; then
  mkdir -p "$PROJECT_DIR/nemoclaw-blueprint/policies/presets"
  cp -r "$OFFICIAL_SOURCE/nemoclaw-blueprint/policies/presets/"* "$PROJECT_DIR/nemoclaw-blueprint/policies/presets/" 2>/dev/null || true
fi
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/
nemoclaw onboard --non-interactive

if ! grep -q "NemoClaw PATH setup" "$HOME/.bashrc"; then
  {
    echo ""
    echo "# NemoClaw PATH setup"
    echo 'export PATH="$HOME/.local/bin:$PATH"'
    echo '[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"'
    echo "# end NemoClaw PATH setup"
  } >> "$HOME/.bashrc"
fi

echo ""
echo "=== Installation Successful ==="
nemoclaw status
echo "--------------------------------"
echo "To connect: nemoclaw my-assistant connect"
echo "MCP phase is separate: run 'bash install_mcp_bridge.sh --all' after base install."