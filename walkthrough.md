# OpenClaw Host Management System

This document outlines the components developed to securely orchestrate OpenClaw sandboxes using NVIDIA OpenShell on your WSL-Ubuntu host.

## Components Built

### 1. The LLM Security Gateway ([`gateway.py`](file:///d:/ag-projects/guard/src/gateway.py))
A fast, lightweight Python service built on `FastAPI`. 
It intercepts LLM traffic destined for the OpenClaw agent. We incorporated a `security_review()` hook that currently checks for basic command injections (like `rm -rf`) before relaying traffic to the upstream provider (e.g., OpenAI/Anthropic). 

### 2. The Sandbox CLI Manager ([`cli.py`](file:///d:/ag-projects/guard/src/cli.py))
A robust command-line tool built using `Typer` that wraps the underlying `openshell` binary.
It automates:
- Starting the OpenShell Gateway.
- Dynamically generating strict YAML policies that map your host workspace directly into the container and expose only the FastAPI proxy.
- Provisioning the sandboxed OpenClaw container.

### 3. Dependencies ([`requirements.txt`](file:///d:/ag-projects/guard/src/requirements.txt))
Contains all necessary dependencies: `typer`, `fastapi`, `uvicorn`, `pydantic`, `httpx`, `PyYAML`.

---

## Verification Instructions (Manual Checklist)

Since OpenShell requires a Linux runtime with Docker, and you specified a WSL-Ubuntu Host on Windows 11, you must perform these verification steps from within your WSL terminal.

1. **Open your WSL-Ubuntu Terminal**.
2. **Navigate to the Source Directory**:
   ```bash
   cd /mnt/d/ag-projects/guard/src/
   ```
3. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Start the LLM Security Gateway**:
   ```bash
   python gateway.py
   ```
   *(The gateway will listen on `0.0.0.0:8000` for security inference proxying).*
5. **Install OpenShell (if not already installed)**:
   *(Ensure Docker daemon is active on WSL)*
   ```bash
   curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
   ```
6. **Launch the Managed OpenClaw Agent**:
   *(In a new WSL tab)*
   ```bash
   # Provide a path to a workspace folder you wish to safely mount
   python cli.py start --workspace /mnt/d/ag-projects/guard/test-workspace --agent openclaw
   ```

*You can also test the dynamic policy update by running `python cli.py set-policy openclaw-sandbox --domain api.github.com`.*
