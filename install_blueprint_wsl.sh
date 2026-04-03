#!/bin/bash
set -e

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: Official Native Wrapper Installer ==="

# ---------------------------------------------------------------------------
# 1. 启动安全网关 (Guard Bootstrap)
# ---------------------------------------------------------------------------
# 在安装 NemoClaw 前启动网关，确保官方的 onboard 探测能通过
echo "[1/3] Bootstrapping Security Gateway..."

# 创建虚拟环境（仅针对网关）
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/python" -m pip install -q -r "$PROJECT_DIR/src/requirements.txt"

# 导出 .env 变量并启动网关
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

# DNS 映射 (确保宿主机能解析 host.openshell.internal)
if ! grep -q "host.openshell.internal" /etc/hosts; then
    echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts
fi

# ---------------------------------------------------------------------------
# 2. 调用原生安装程序 (Native Script Execution)
# ---------------------------------------------------------------------------
echo "[2/3] Invoking NATIVE NVIDIA NemoClaw installer..."

# 注入我们的网关配置变量，让原生脚本内部调用的 onboard 自动匹配
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"
unset NVIDIA_API_KEY 

# 直接运行官方原生脚本
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash

# ---------------------------------------------------------------------------
# 3. 同步 Blueprint 策略 (Final Sync)
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
