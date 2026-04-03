#!/bin/bash
set -e

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: Official Native Wrapper Installer (Full Permission) ==="

# ---------------------------------------------------------------------------
# 0. 基础权限与依赖 (Step 0: Prerequisites & Docker Fix)
# ---------------------------------------------------------------------------
echo "[0/3] Preparing system dependencies and Docker permissions..."
sudo apt-get update -y -q
sudo apt-get install -y -q \
  ca-certificates curl git jq lsof psmisc \
  python3 python3-pip python3-venv docker.io

# 启动 Docker 并在当前会话中强行开放权限
echo "Unlocking Docker socket for current session..."
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker
sudo chmod 666 /var/run/docker.sock || true

# 验证 Docker 权限
if ! docker ps > /dev/null 2>&1; then
    echo "Error: Docker socket still inaccessible. Please check 'sudo systemctl status docker'."
    exit 1
fi
echo "鉁 Docker is ready and accessible."

# ---------------------------------------------------------------------------
# 1. 启动安全网关 (Step 1: Guard Bootstrap)
# ---------------------------------------------------------------------------
echo "[1/3] Bootstrapping Security Gateway..."

# 初始化 Python 虚拟环境 (仅针对网关)
rm -rf "$PROJECT_DIR/.venv"
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/python" -m pip install -q -r "$PROJECT_DIR/src/requirements.txt"

# 启动网关
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi
lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &

# 等待就绪
for i in {1..10}; do
    if curl -s http://127.0.0.1:8090/health > /dev/null; then break; fi
    sleep 1
done

# DNS 映射 (用于探测)
if ! grep -q "host.openshell.internal" /etc/hosts; then
    echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts
fi

# ---------------------------------------------------------------------------
# 2. 调用原生安装程序 (Step 2: Native Script Execution)
# ---------------------------------------------------------------------------
echo "[2/3] Invoking NATIVE NVIDIA NemoClaw installer..."

# 注入自动化变量，驱动官方脚本内置的 onboard 流程
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"
unset NVIDIA_API_KEY 

# 直接执行官方原生脚本
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash

# ---------------------------------------------------------------------------
# 3. 同步 Blueprint 策略 (Step 3: Final Alignment)
# ---------------------------------------------------------------------------
echo "[3/3] Synchronizing Guard Blueprint..."

# 确保加载 nvm 环境以找到 nemoclaw
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
export PATH="$HOME/.local/bin:$PATH"

# 强制将本项目定义的 Blueprint 同步到官方加载路径
mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/

# 重新触发一次 onboard 以激活 Blueprint 特有的挂载与策略
nemoclaw onboard --non-interactive

echo ""
echo "=== Installation Successful ==="
nemoclaw status
echo "--------------------------------"
echo "To connect: nemoclaw my-assistant connect"
