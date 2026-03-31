# OpenClaw Host Management System

This document describes the current runtime path and the blueprint target path.

## Current Runtime Path (Effective)

1. OpenClaw in sandbox sends model requests to `https://inference.local/v1`.
2. OpenShell gateway resolves `inference.local` using active provider/inference configuration.
3. Provider `guard-gateway` forwards to `http://host.openshell.internal:8090/v1`.
4. Host-side `src/gateway.py` performs security scan + provider routing, then calls upstream providers.

In this path, the decisive runtime forwarding behavior is controlled by:

- `openshell provider create ... --config OPENAI_BASE_URL=http://host.openshell.internal:8090/v1`
- `openshell inference set --provider guard-gateway --model ...`

## Current Role Of Project Blueprint

`src/onboard.py` still generates project-local artifacts:

- `nemoclaw-blueprint/policies/openclaw-sandbox.yaml`
- `sandbox_workspace/openclaw/openclaw.json`
- `sandbox_workspace/openclaw/agents/main/agent/auth-profiles.json`
- `sandbox_workspace/openclaw-data/`

But `wsl_start.sh` currently runs `nemoclaw onboard` without setting `NEMOCLAW_BLUEPRINT_PATH`, so project-local blueprint is not automatically guaranteed to be the runtime blueprint source.

## Blueprint-Driven Recommended Procedure

Run from WSL:

```bash
cd /mnt/d/ag-projects/guard
./.venv/bin/python src/cli.py onboard --workspace /mnt/d/ag-projects/guard
rsync -a --delete /mnt/d/ag-projects/guard/nemoclaw-blueprint/ ~/.nemoclaw/source/nemoclaw-blueprint/
nemoclaw onboard
```

This ensures project blueprint changes are actually consumed by NemoClaw onboarding.
