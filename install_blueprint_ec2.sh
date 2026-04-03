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
# 确保在创建 venv 前安装了 python3-venv
sudo apt-get update -y -q
sudo apt-get install -y -q \
  ca-certificates curl git jq lsof psmisc \
  python3 python3-pip python3-venv docker.io

# ---------------------------------------------------------------------------
# 1. 预备环境：网关与 DNS (Custom Guard Prep)
# ---------------------------------------------------------------------------
echo "[1/4] Preparing Security Gateway and DNS..."

# 初始化 Python 环境
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/src/requirements.txt"

# 启动网关
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi
lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &

# 等待网关就绪（这一步至关重要，因为官方脚本会执行探测）
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

# 导出环境变量，让官方脚本在安装后自动执行正确的 Onboarding
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"
unset NVIDIA_API_KEY # 强制隔离

# 直接运行官方 hosted script
# 如果脚本有变动，我们将自动继承其逻辑
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash

# ---------------------------------------------------------------------------
# 3. 同步 Blueprint (Guard Customization)
# ---------------------------------------------------------------------------
echo "[3/4] Synchronizing Guard Blueprint..."
# 此时官方引擎已安装在 ~/.nemoclaw/source/nemoclaw-repo
# 我们确保全局 source 路径下使用的是本项目的 blueprint.yaml
mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/

# 重新触发一次 onboard 以确保 Blueprint 中的额外配置（如 mappings）被加载
export PATH="$HOME/.local/bin:$PATH"
nemoclaw onboard --non-interactive

# ---------------------------------------------------------------------------
# 4. 完成 (Final Verification)
# ---------------------------------------------------------------------------
echo "[4/4] Installation Complete."
nemoclaw status
echo "--------------------------------"
echo "Verification: openshell inference get"
openshell inference get
