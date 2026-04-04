#!/bin/bash
set -e

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: Official Native Wrapper Installer (Full Permission) ==="
echo "Project Path: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# 0. 系统基础依赖 (System Dependencies)
# ---------------------------------------------------------------------------
echo "[0/5] Checking system dependencies..."
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
echo "[1/5] Preparing Python Environment..."

# 初始化 Python 环境
echo "Setting up Python virtual environment..."
rm -rf "$PROJECT_DIR/.venv"
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$PROJECT_DIR/.venv/bin/python" -m pip install -q -r "$PROJECT_DIR/src/requirements.txt"

# ---------------------------------------------------------------------------
# 1b. 交互式模型选择 (Model Setup Wizard)
# ---------------------------------------------------------------------------
echo "[1b/5] Running Model Setup Wizard..."
if [ -f "$PROJECT_DIR/.env" ]; then
    # 加载 .env 供 setup.py 读取（仅在当前子 shell）
    set -a && source "$PROJECT_DIR/.env" && set +a
fi
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/setup.py" --non-interactive --project-dir "$PROJECT_DIR"

# 重新加载 .env（setup.py 可能已写入 MODEL_ID）
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a && source "$PROJECT_DIR/.env" && set +a
fi

# ---------------------------------------------------------------------------
# 2. 启动网关 (Start Security Gateway)
# ---------------------------------------------------------------------------
echo "[2/5] Starting Security Gateway..."
lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
if [ -f "$PROJECT_DIR/.env" ]; then
    env $(grep -v '^#' "$PROJECT_DIR/.env" | xargs) \
        nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &
else
    nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &
fi

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
# 3. 调用官方安装程序 (Official Engine Setup)
# ---------------------------------------------------------------------------
echo "[3/5] Invoking official NVIDIA NemoClaw installer..."

# 导出 NemoClaw 环境变量（使用 setup.py 选择的模型）
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="${MODEL_ID:-openrouter/stepfun/step-3.5-flash:free}"
export COMPATIBLE_API_KEY="guard-managed"
export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
unset NVIDIA_API_KEY

# 绕过官方 bootstrap 包装器（nemoclaw.sh）的 bug：
# bootstrap 将仓库 clone 到临时目录，install.sh 检测到本地 package.json
# 后走 "from source" 路径（npm link 指向临时目录），但 bootstrap 退出时
# trap 'rm -rf tmpdir' 删除了该目录，导致所有符号链接断裂。
#
# 修复：自己 clone 到持久目录，直接运行 scripts/install.sh。
# resolve_repo_root() 检测到 $NEMOCLAW_SRC/package.json → 走 "from source"
# 路径，npm link 指向持久目录，不会被清理。
NEMOCLAW_SRC="$HOME/.nemoclaw/source"
if [ ! -f "$NEMOCLAW_SRC/package.json" ]; then
    echo "Cloning NemoClaw to persistent source directory..."
    rm -rf "$NEMOCLAW_SRC"
    git clone --depth 1 https://github.com/NVIDIA/NemoClaw.git "$NEMOCLAW_SRC"
fi

# 从持久源码目录运行官方安装器（跳过 bootstrap 包装器）
bash "$NEMOCLAW_SRC/scripts/install.sh"

# 确保加载 nvm 环境
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
export PATH="$HOME/.local/bin:$PATH"

# 验证 nemoclaw 命令可用
if ! command -v nemoclaw &>/dev/null; then
    echo "ERROR: nemoclaw CLI is not available after installation."
    echo "       Check npm link output and PATH settings."
    exit 1
fi
echo "✓ nemoclaw CLI verified: $(nemoclaw --version 2>/dev/null || echo 'ok')"

# ---------------------------------------------------------------------------
# 4. 同步 Blueprint (Guard Customization)
# ---------------------------------------------------------------------------
echo "[4/5] Synchronizing Guard Blueprint..."

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
# 5. 持久化环境变量 (Persistence)
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
