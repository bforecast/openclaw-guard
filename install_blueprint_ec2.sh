#!/bin/bash
set -e

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: EC2 Blueprint Installer ==="
echo "Operating Path: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# 0. 基础环境初始化 (Infrastructure Setup)
# ---------------------------------------------------------------------------
export PATH="$HOME/.local/bin:$PATH"

# 0.1 安装系统级依赖 (来自 ec2_bootstrap.sh)
echo "[0/4] Installing system dependencies (Docker, Python, Node)..."
sudo apt-get update -y -q
sudo apt-get install -y -q \
  ca-certificates curl git jq lsof psmisc \
  python3 python3-pip python3-venv docker.io

# 0.2 配置 Docker 权限
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker
if ! groups "$USER" | grep -q '\bdocker\b'; then
    sudo usermod -aG docker "$USER"
    echo "鉁 Added user to docker group. Note: You may need to run 'newgrp docker' if this script fails on docker commands."
fi

# 0.3 自动安装 Node.js (来自 install_blueprint_wsl.sh)
if ! command -v node >/dev/null 2>&1; then
    echo "Installing Node.js via nvm..."
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    nvm install 22 --silent
    nvm use 22 --silent
fi

# 0.4 安装 NemoClaw (编译安装以确保 dist 目录存在)
if ! command -v nemoclaw >/dev/null 2>&1; then
    echo "NemoClaw not found. Installing from source..."
    TEMP_DIR=$(mktemp -d)
    git clone --depth 1 https://github.com/NVIDIA/NemoClaw.git "$TEMP_DIR"
    cd "$TEMP_DIR"
    
    echo "Building NemoClaw..."
    npm install --ignore-scripts
    # 编译 CLI 和 插件
    npm run build:cli || tsc -p tsconfig.src.json
    cd nemoclaw && npm install --ignore-scripts && npm run build
    cd ..
    
    echo "Installing globally..."
    npm link
    cd "$PROJECT_DIR"
    rm -rf "$TEMP_DIR"
fi

# 0.5 初始化网关 Python 环境
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "Setting up Python virtual environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/src/requirements.txt"

# ---------------------------------------------------------------------------
# 1. 后台启动安全网关 (Gateway Bootstrap)
# ---------------------------------------------------------------------------
echo "[1/3] Starting Security Gateway..."
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

# 杀死旧进程并启动
lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &

echo "Waiting for gateway to respond..."
for i in {1..15}; do
    if curl -s http://127.0.0.1:8090/health > /dev/null; then
        echo "鉁 Gateway is online."
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 2. 注入验证域名 (DNS Hack for EC2 Host)
# ---------------------------------------------------------------------------
echo "[2/3] Mapping host.openshell.internal to loopback..."
if ! grep -q "host.openshell.internal" /etc/hosts; then
    echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts
else
    echo "鉁 Mapping already present."
fi

# ---------------------------------------------------------------------------
# 3. 纯 Blueprint 安装 (Declarative Onboarding)
# ---------------------------------------------------------------------------
echo "[3/3] Executing Blueprint Onboarding..."
unset NVIDIA_API_KEY # 禁用默认云端路径

# 同步项目配置到全局源
mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/

# 定义部署环境变量 (驱动 NemoClaw 消费 Blueprint 配置)
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"

# 执行静默安装
nemoclaw onboard --non-interactive

echo ""
echo "=== EC2 Installation Successful ==="
echo "Status:"
nemoclaw status
echo "--------------------------------"
echo "To connect to your agent: nemoclaw my-assistant connect"
