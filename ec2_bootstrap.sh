#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SCRIPT="$PROJECT_DIR/ec2_ubuntu_start.sh"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script is for Linux (AWS EC2 Ubuntu)."
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get not found. This script targets Ubuntu/Debian."
  exit 1
fi

echo "[1/4] Installing base packages..."
sudo apt-get update -y
sudo apt-get install -y \
  ca-certificates \
  curl \
  git \
  jq \
  lsof \
  psmisc \
  python3 \
  python3-pip \
  python3-venv \
  docker.io

echo "[2/4] Ensuring Docker service..."
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker

if ! groups "$USER" | grep -q '\bdocker\b'; then
  echo "[3/4] Adding user '$USER' to docker group..."
  sudo usermod -aG docker "$USER"
  echo
  echo "Docker group updated. Please re-login (or run: newgrp docker), then rerun:"
  echo "  $START_SCRIPT"
  exit 0
fi

echo "[3/4] Verifying Docker access..."
if ! docker info >/dev/null 2>&1; then
  echo "Current shell still has no Docker permission."
  echo "Run: newgrp docker"
  echo "Then rerun: $START_SCRIPT"
  exit 1
fi

if [[ ! -x "$START_SCRIPT" ]]; then
  chmod +x "$START_SCRIPT"
fi

echo "[4/4] Running project installer..."
"$START_SCRIPT"
