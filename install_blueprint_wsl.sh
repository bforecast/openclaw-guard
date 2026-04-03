#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: Simplified Blueprint Installer ==="

# ---------------------------------------------------------------------------
# 0. 环境初始化 (Step 0: Setup Environment)
# ---------------------------------------------------------------------------
# 优先加载 nvm 环境（如果存在）
if [ -s "$HOME/.nvm/nvm.sh" ]; then
    export NVM_DIR="$HOME/.nvm"
    \. "$NVM_DIR/nvm.sh"
fi
export PATH="$HOME/.local/bin:$PATH"

# 自动安装 Node.js/nvm
if ! command -v node >/dev/null 2>&1; then
    echo "[0/3] Installing Node.js via nvm..."
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    nvm install 22
    nvm use 22
fi

# 自动安装 NemoClaw (Option B: 编译安装)
NEMOCLAW_BROKEN=0
if command -v nemoclaw >/dev/null 2>&1; then
    if ! nemoclaw --help >/dev/null 2>&1; then
        echo "Found existing but BROKEN NemoClaw installation. Forcing reinstall..."
        NEMOCLAW_BROKEN=1
    fi
fi

if ! command -v nemoclaw >/dev/null 2>&1 || [ $NEMOCLAW_BROKEN -eq 1 ]; then
    echo "[0/3] Installing NemoClaw CLI from official source..."
    npm uninstall -g nemoclaw 2>/dev/null || true
    
    TEMP_DIR=$(mktemp -d)
    git clone --depth 1 https://github.com/NVIDIA/NemoClaw.git "$TEMP_DIR"
    cd "$TEMP_DIR"
    
    npm install --ignore-scripts
    echo "Compiling CLI..."
    npm run build:cli
    
    cd nemoclaw
    npm install --ignore-scripts
    npm run build
    cd ..
    
    echo "Installing NemoClaw globally..."
    npm install -g .
    export PATH="$(npm config get prefix)/bin:$PATH"
    
    cd "$PROJECT_DIR"
    rm -rf "$TEMP_DIR"
    echo "鉁 NemoClaw ready."
fi

# 初始化 Python 虚拟环境
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[0/3] Setting up Python venv..."
    python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/src/requirements.txt"

# ---------------------------------------------------------------------------
# 1. 后台启动网关 (Step 1: Start Gateway)
# ---------------------------------------------------------------------------
echo "[1/3] Starting Security Gateway..."
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &

echo "Waiting for gateway..."
for i in {1..10}; do
    if curl -s http://127.0.0.1:8090/health > /dev/null; then break; fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 2. 注入验证域名 (Step 2: DNS Map)
# ---------------------------------------------------------------------------
echo "[2/3] Mapping host.openshell.internal..."
if ! grep -q "host.openshell.internal" /etc/hosts; then
    echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts
fi

# ---------------------------------------------------------------------------
# 3. 纯 Blueprint 安装 (Step 3: Onboarding)
# ---------------------------------------------------------------------------
echo "[3/3] Running Pure Onboarding..."
unset NVIDIA_API_KEY 

mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/

export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"

nemoclaw onboard --non-interactive

echo "--- Verified! ---"
openshell inference get
