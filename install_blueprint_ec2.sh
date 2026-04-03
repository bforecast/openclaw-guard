#!/bin/bash
set -e

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== OpenClaw Guard: EC2 Blueprint Installer (Official Pattern) ==="
echo "Operating Path: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# 0. 环境初始化 (Step 0: Infrastructure Setup)
# ---------------------------------------------------------------------------
# 优先加载 nvm 环境
if [ -s "$HOME/.nvm/nvm.sh" ]; then
    export NVM_DIR="$HOME/.nvm"
    \. "$NVM_DIR/nvm.sh"
fi
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

# 0.1 安装系统级依赖
echo "[0/4] Checking system dependencies..."
sudo apt-get update -y -q
sudo apt-get install -y -q \
  ca-certificates curl git jq lsof psmisc \
  python3 python3-pip python3-venv docker.io

# 0.2 配置 Docker
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker
if ! groups "$USER" | grep -q '\bdocker\b'; then
    sudo usermod -aG docker "$USER"
fi

# 0.3 自动安装 Node.js (官方推荐 v22)
if ! command -v node >/dev/null 2>&1; then
    echo "Installing Node.js via nvm..."
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    nvm install 22 --silent
    nvm use 22 --silent
fi

# 0.4 安装 NemoClaw (参考官方模式: Clone to source + Shim)
INSTALL_DIR="$HOME/.nemoclaw/source/nemoclaw-repo"
NEMOCLAW_BIN="$HOME/.local/bin/nemoclaw"

# 探测是否需要安装/修复
NEED_INSTALL=0
if ! command -v nemoclaw >/dev/null 2>&1; then
    NEED_INSTALL=1
elif ! nemoclaw --help >/dev/null 2>&1; then
    echo "NemoClaw found but broken. Reinstalling..."
    NEED_INSTALL=1
fi

if [ $NEED_INSTALL -eq 1 ]; then
    echo "[0/4] Installing NemoClaw using official source pattern..."
    mkdir -p "$HOME/.nemoclaw/source"
    rm -rf "$INSTALL_DIR" # 清理旧的尝试
    
    git clone --depth 1 https://github.com/NVIDIA/NemoClaw.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    
    echo "Building core components..."
    npm install --ignore-scripts
    npm run build:cli
    
    cd nemoclaw
    npm install --ignore-scripts
    npm run build
    cd ..
    
    # 创建官方风格的 Shim (代替 npm link)
    echo "Creating command shim at $NEMOCLAW_BIN..."
    cat > "$NEMOCLAW_BIN" <<EOF
#!/bin/bash
export NVM_DIR="\$HOME/.nvm"
[ -s "\$NVM_DIR/nvm.sh" ] && \. "\$NVM_DIR/nvm.sh"
node "$INSTALL_DIR/bin/nemoclaw.js" "\$@"
EOF
    chmod +x "$NEMOCLAW_BIN"
    
    cd "$PROJECT_DIR"
    echo "鉁 NemoClaw CLI is ready at $NEMOCLAW_BIN"
fi

# 0.5 初始化网关 Python 环境
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "Setting up Python virtual environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/src/requirements.txt"

# ---------------------------------------------------------------------------
# 1. 后台启动安全网关 (Step 1: Start Gateway)
# ---------------------------------------------------------------------------
echo "[1/3] Starting Security Gateway..."
if [ -f "$PROJECT_DIR/.env" ]; then
    # 过滤掉注释，精确导出
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/src/gateway.py" > "$PROJECT_DIR/logs/gateway.log" 2>&1 &

# 等待网关就绪
for i in {1..15}; do
    if curl -s http://127.0.0.1:8090/health > /dev/null; then
        echo "鉁 Gateway is online."
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 2. 注入验证域名 (Step 2: DNS Map)
# ---------------------------------------------------------------------------
echo "[2/3] Mapping host.openshell.internal to loopback..."
if ! grep -q "host.openshell.internal" /etc/hosts; then
    echo "127.0.0.1 host.openshell.internal" | sudo tee -a /etc/hosts
fi

# ---------------------------------------------------------------------------
# 3. 纯 Blueprint 安装 (Step 3: Onboarding)
# ---------------------------------------------------------------------------
echo "[3/3] Executing Blueprint Onboarding..."
unset NVIDIA_API_KEY 

# 同步项目 Blueprint
mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" ~/.nemoclaw/source/nemoclaw-blueprint/

# 定义部署变量
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="openrouter/stepfun/step-3.5-flash:free"
export COMPATIBLE_API_KEY="guard-managed"

# 运行 Onboarding
nemoclaw onboard --non-interactive

echo ""
echo "=== Installation Successful ==="
nemoclaw status
echo "--------------------------------"
echo "To connect: nemoclaw my-assistant connect"
