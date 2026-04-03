#!/bin/bash
set -e

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: Official Native Wrapper Installer (Full Permission) ==="
echo "Project Path: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# 0. 系统基础依赖 (System Dependencies)
# ---------------------------------------------------------------------------
echo "[0/4] Checking system dependencies..."
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
# 1. 预备环境：网关与 DNS (Custom Guard Prep)
# ---------------------------------------------------------------------------
echo "[1/4] Preparing Security Gateway and DNS..."

# 初始化 Python 环境
echo "Setting up Python virtual environment..."
rm -rf "$PROJECT_DIR/.venv"
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$PROJECT_DIR/.venv/bin/python" -m pip install -q -r "$PROJECT_DIR/src/requirements.txt"

# 启动网关
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi
lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &

# 等待网关就绪
for i in {1..15}; do
    if curl -s http://127.0.0.1:8090/health > /dev/null; then break; fi
    sleep 1
done

# DNS 映射
if ! grep -q "host.openshell.internal" /etc/hosts; then
    echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts
fi

# ---------------------------------------------------------------------------
# 2. 调用官方安装程序 (Official Engine Setup)
# ---------------------------------------------------------------------------
echo "[2/4] Invoking official NVIDIA NemoClaw installer..."

# 导出环境变量
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
unset NVIDIA_API_KEY 

# 直接执行官方脚本
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash

# ---------------------------------------------------------------------------
# 3. 同步 Blueprint (Guard Customization)
# ---------------------------------------------------------------------------
echo "[3/4] Synchronizing Guard Blueprint..."

# 确保加载 nvm 环境
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
export PATH="$HOME/.local/bin:$PATH"

# 同步项目 Blueprint
mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint

# 补全缺失的 Presets
echo "Compiling official policy presets..."
OFFICIAL_SOURCE="$HOME/.nemoclaw/source"
if [ -d "$OFFICIAL_SOURCE/nemoclaw-blueprint/policies/presets" ]; then
    mkdir -p "$PROJECT_DIR/nemoclaw-blueprint/policies/presets"
    cp -r "$OFFICIAL_SOURCE/nemoclaw-blueprint/policies/presets/"* "$PROJECT_DIR/nemoclaw-blueprint/policies/presets/"
fi

rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/

# 重新触发一次 onboard
nemoclaw onboard --non-interactive

# ---------------------------------------------------------------------------
# 4. 持久化环境变量 (Step 4: Persistence)
# ---------------------------------------------------------------------------
if ! grep -q "NemoClaw PATH setup" "$HOME/.bashrc"; then
    echo "" >> "$HOME/.bashrc"
    echo "# NemoClaw PATH setup" >> "$HOME/.bashrc"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo '[ -s "$HOME/.nvm/nvm.sh" ] && \. "$HOME/.nvm/nvm.sh"' >> "$HOME/.bashrc"
    echo "# end NemoClaw PATH setup" >> "$HOME/.bashrc"
    echo "鉁 Environment variables persisted to ~/.bashrc"
fi

echo ""
echo "=== Installation Successful ==="
nemoclaw status
echo "--------------------------------"
echo "To connect: nemoclaw my-assistant connect"
