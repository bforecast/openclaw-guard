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
echo "[1/4] Preparing Python Environment..."

# 初始化 Python 环境
# --system-site-packages 让 venv 继承 python3-bpfcc (eBPF 只有 apt 包)
echo "Setting up Python virtual environment..."
rm -rf "$PROJECT_DIR/.venv"
python3 -m venv --system-site-packages "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/python" -m pip install -q --upgrade pip setuptools
# 安装 guard package (editable) — 自动从 pyproject.toml 拉依赖
"$PROJECT_DIR/.venv/bin/python" -m pip install -q -e "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# 1b. 交互式模型选择 (Model Setup Wizard)
# ---------------------------------------------------------------------------
echo "[1b/4] Running Model Setup Wizard..."
if [ -f "$PROJECT_DIR/.env" ]; then
    # 加载 .env 供 wizard 读取（仅在当前子 shell）
    set -a && source "$PROJECT_DIR/.env" && set +a
fi
"$PROJECT_DIR/.venv/bin/python" -m guard.wizard --project-dir "$PROJECT_DIR"

# 重新加载 .env（wizard 可能已写入 MODEL_ID）
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a && source "$PROJECT_DIR/.env" && set +a
fi

# ---------------------------------------------------------------------------
# 2. 启动网关 (Start Security Gateway)
# ---------------------------------------------------------------------------
echo "[2/4] Starting Security Gateway..."
lsof -t -i :8090 | xargs kill -9 2>/dev/null || true
mkdir -p "$PROJECT_DIR/logs"
if [ -f "$PROJECT_DIR/.env" ]; then
    env $(grep -v '^#' "$PROJECT_DIR/.env" | xargs) \
        nohup "$PROJECT_DIR/.venv/bin/python" -m guard.gateway > "$PROJECT_DIR/logs/gateway.log" 2>&1 &
else
    nohup "$PROJECT_DIR/.venv/bin/python" -m guard.gateway > "$PROJECT_DIR/logs/gateway.log" 2>&1 &
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
# 2b. 启动内核层网络抓取守护 (Kernel Network Capture)
# ---------------------------------------------------------------------------
echo "[2b/4] Starting kernel network capture daemon..."
nohup "$PROJECT_DIR/.venv/bin/python" -m guard.network_capture \
    --config "$PROJECT_DIR/gateway.yaml" \
    --audit-db "$PROJECT_DIR/logs/security_audit.db" \
    > "$PROJECT_DIR/logs/network_capture.log" 2>&1 &
NETWORK_CAPTURE_PID=$!

# ---------------------------------------------------------------------------
# 2c. 启动安装期网络授权代理 (Install-time Authorization Proxy)
# ---------------------------------------------------------------------------
echo "[2c/4] Starting install-time network proxy on 127.0.0.1:8091..."
nohup "$PROJECT_DIR/.venv/bin/python" -m guard.install_proxy \
    --config "$PROJECT_DIR/gateway.yaml" \
    --audit-db "$PROJECT_DIR/logs/security_audit.db" \
    > "$PROJECT_DIR/logs/install_proxy.log" 2>&1 &
INSTALL_PROXY_PID=$!
trap '[ -n "${INSTALL_PROXY_PID:-}" ] && kill $INSTALL_PROXY_PID 2>/dev/null || true' EXIT
sleep 1

# 强制让 curl / pip / npm / git 走授权代理
export http_proxy="http://127.0.0.1:8091"
export https_proxy="http://127.0.0.1:8091"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export NO_PROXY="127.0.0.1,localhost,host.openshell.internal,host.docker.internal,::1"
export no_proxy="$NO_PROXY"

# ---------------------------------------------------------------------------
# 3. 调用官方安装程序 (Official Engine Setup)
# ---------------------------------------------------------------------------
echo "[3/4] Invoking official NVIDIA NemoClaw installer..."

# 导出 NemoClaw 环境变量（使用 setup.py 选择的模型）
export NEMOCLAW_NON_INTERACTIVE=1
export NEMOCLAW_PROVIDER="custom"
export NEMOCLAW_ENDPOINT_URL="http://host.openshell.internal:8090/v1"
export NEMOCLAW_MODEL="${MODEL_ID:-nvidia/nemotron-3-super-120b-a12b:free}"
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
    echo "Downloading NemoClaw source..."
    rm -rf "$NEMOCLAW_SRC"
    mkdir -p "$NEMOCLAW_SRC"
    curl -fsSL https://github.com/NVIDIA/NemoClaw/archive/refs/heads/main.tar.gz \
        | tar xz --strip-components=1 -C "$NEMOCLAW_SRC"
fi

# 在 install.sh 运行前，先把我们的自定义 Blueprint 合入源码树
# 这样第一次 onboard 就直接使用，省掉二次 onboard
echo "[3b/4] Pre-merging Guard Blueprint into source tree..."
OFFICIAL_POLICIES="$NEMOCLAW_SRC/nemoclaw-blueprint/policies"
if [ -d "$OFFICIAL_POLICIES" ]; then
    mkdir -p "$PROJECT_DIR/nemoclaw-blueprint/policies"
    cp -rn "$OFFICIAL_POLICIES/"* "$PROJECT_DIR/nemoclaw-blueprint/policies/" 2>/dev/null || true
fi
rsync -a --delete "$PROJECT_DIR/nemoclaw-blueprint/" "$NEMOCLAW_SRC/nemoclaw-blueprint/"

# 可选：覆盖 OpenClaw 版本 — 本地构建基础镜像替换 GHCR 预构建版本
# 直接在 Dockerfile 注入 npm install 会导致镜像膨胀 +1.7GB（npm 缓存 + 双份 openclaw），
# 因此改为本地构建 Dockerfile.base 并标记为 ghcr 镜像名，让 sandbox Dockerfile FROM 使用本地版本
if [ -n "${OPENCLAW_VERSION:-}" ]; then
    echo "Overriding OpenClaw version to: $OPENCLAW_VERSION"
    sed -i "s|openclaw@[0-9][0-9.]*|openclaw@${OPENCLAW_VERSION}|g" "$NEMOCLAW_SRC/Dockerfile.base"
    echo "Building local base image with openclaw@${OPENCLAW_VERSION}..."
    docker build -f "$NEMOCLAW_SRC/Dockerfile.base" -t ghcr.io/nvidia/nemoclaw/sandbox-base:latest "$NEMOCLAW_SRC"
    echo "✓ Local base image built with openclaw@${OPENCLAW_VERSION}"
fi

# 从持久源码目录运行官方安装器（跳过 bootstrap 包装器）
# NEMOCLAW_REPO_ROOT 告诉 install.sh 的 is_source_checkout() 这是持久源码，
# 避免它重新 git clone 覆盖我们的修改（如 OPENCLAW_VERSION、blueprint 预合并）
export NEMOCLAW_REPO_ROOT="$NEMOCLAW_SRC"

# 暂时禁用安装期代理 — install_proxy 的 CONNECT 隧道在 npm 高并发下会
# ECONNRESET。npm install 走直连更稳定；安装期审计由 network_capture 覆盖。
_saved_http_proxy="${http_proxy:-}"
_saved_https_proxy="${https_proxy:-}"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
bash "$NEMOCLAW_SRC/scripts/install.sh"
# 恢复代理设置（后续步骤可能仍需要）
export http_proxy="$_saved_http_proxy"
export https_proxy="$_saved_https_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"

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
# 4. 持久化 (Persistence: bashrc + systemd gateway)
# ---------------------------------------------------------------------------

# 4a. bashrc PATH 持久化
if ! grep -q "NemoClaw PATH setup" "$HOME/.bashrc"; then
    echo "" >> "$HOME/.bashrc"
    echo "# NemoClaw PATH setup" >> "$HOME/.bashrc"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo '[ -s "$HOME/.nvm/nvm.sh" ] && \. "$HOME/.nvm/nvm.sh"' >> "$HOME/.bashrc"
    echo "# end NemoClaw PATH setup" >> "$HOME/.bashrc"
fi

# 4b. systemd 服务：gateway 开机自启 + 崩溃自动重启
echo "Setting up gateway systemd service..."
# 先停掉之前 nohup 启动的 gateway（systemd 接管）
lsof -t -i :8090 | xargs kill -9 2>/dev/null || true

sudo tee /etc/systemd/system/guard-gateway.service > /dev/null <<UNIT
[Unit]
Description=OpenClaw Security Gateway
After=network.target docker.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$PROJECT_DIR/.venv/bin/python -m guard.gateway
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# 4c. systemd 服务：network-capture 持久化
echo "Setting up network-capture systemd service..."
sudo tee /etc/systemd/system/guard-network-capture.service > /dev/null <<UNIT
[Unit]
Description=OpenClaw Guard Kernel Network Capture
After=network.target docker.service guard-gateway.service

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=-$PROJECT_DIR/.env
ExecStart=$PROJECT_DIR/.venv/bin/python -m guard.network_capture --config $PROJECT_DIR/gateway.yaml --audit-db $PROJECT_DIR/logs/security_audit.db
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# 卸载安装期临时 daemon (systemd 接管 network-capture)
[ -n "${NETWORK_CAPTURE_PID:-}" ] && kill $NETWORK_CAPTURE_PID 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable guard-gateway >/dev/null 2>&1
sudo systemctl start guard-gateway
sudo systemctl enable guard-network-capture >/dev/null 2>&1 || true
sudo systemctl start guard-network-capture || true

# 等待 systemd gateway 就绪
for i in {1..10}; do
    if curl -s http://127.0.0.1:8090/health > /dev/null; then break; fi
    sleep 1
done

if curl -s http://127.0.0.1:8090/health > /dev/null; then
    echo "✓ Gateway systemd service active (auto-start on reboot)"
else
    echo "WARN: Gateway service may not be ready yet. Check: sudo systemctl status guard-gateway"
fi

echo ""
echo "=== Installation Successful ==="
nemoclaw status
echo "--------------------------------"
echo "To connect: nemoclaw my-assistant connect"
