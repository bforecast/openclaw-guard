#!/bin/bash
set -e

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: Official Pattern Wrapper Installer ==="
echo "Project Path: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# 0. 系统基础依赖 (System Dependencies)
# ---------------------------------------------------------------------------
echo "[0/4] Checking system dependencies..."
sudo apt-get update -y -q
sudo apt-get install -y -q \
  ca-certificates curl git jq lsof psmisc \
  python3 python3-pip python3-venv docker.io

# 确保 Docker 运行并具备权限
echo "Starting and enabling Docker..."
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker
if ! groups "$USER" | grep -q '\bdocker\b'; then
    sudo usermod -aG docker "$USER"
    echo "鉁 Added user to docker group."
fi

# 等待 Docker 守护进程就绪
echo "Waiting for Docker socket..."
for i in {1..10}; do
    if sudo docker ps > /dev/null 2>&1; then
        echo "鉁 Docker is ready."
        break
    fi
    sleep 2
done

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

# 导出环境变量，驱动官方脚本自动完成 Onboarding
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
unset NVIDIA_API_KEY 

# 下载官方脚本并使用 sg docker 运行，以解决权限延迟生效问题
curl -fsSL https://www.nvidia.com/nemoclaw.sh -o /tmp/nemoclaw_install.sh
# 即使当前会话没刷新组，sg docker 也能让子进程立刻拥有 docker 权限
sg docker -c "bash /tmp/nemoclaw_install.sh"

# ---------------------------------------------------------------------------
# 3. 同步 Blueprint (Guard Customization)
# ---------------------------------------------------------------------------
echo "[3/4] Synchronizing Guard Blueprint..."
mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/

# 重新触发一次 onboard 以加载我们的 mappings
export PATH="$HOME/.local/bin:$PATH"
# 同样使用 sg docker 保证最后的 onboard 命令也能访问 Docker
sg docker -c "nemoclaw onboard --non-interactive"

# ---------------------------------------------------------------------------
# 4. 完成 (Final Verification)
# ---------------------------------------------------------------------------
echo "[4/4] Installation Complete."
sg docker -c "nemoclaw status"
echo "--------------------------------"
echo "To start chatting: nemoclaw my-assistant connect"
