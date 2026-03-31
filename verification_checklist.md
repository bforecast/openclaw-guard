# OpenClaw Guard Verification Checklist

This checklist captures the validated end-to-end flow for the current
runtime architecture (inference.local + OpenShell provider routing + host gateway):

- OpenClaw inside sandbox
- `inference.local` route via OpenShell provider `guard-gateway`
- host-side security gateway (`src/gateway.py`)
- upstream model provider (OpenRouter)

## Prerequisites

- Run from WSL-Ubuntu.
- OpenShell and NemoClaw installed and reachable in PATH.
- `.env` includes `OPENROUTER_API_KEY`.
- Host gateway running:

```bash
cd /mnt/d/ag-projects/guard
set -a; source .env; set +a
./.venv/bin/python src/gateway.py
```

- OpenShell inference points to `guard-gateway`:

```bash
openshell inference get
```

Expected key fields:

- `Provider: guard-gateway`
- `Model: openrouter/stepfun/step-3.5-flash:free` (or another explicitly configured model)

## Case 1: Normal Request (Expected 200)

Action:

- Send a simple prompt from OpenClaw, for example: `hi`

Expected host gateway log:

- `ALLOWED [openrouter/openrouter/auto] -> https://openrouter.ai/api/v1`
- `POST /v1/responses ... 200 OK`
- Upstream log line showing `HTTP/1.1 200 OK` from OpenRouter.

## Case 2: Insufficient Credit / Token Limit (Expected 402 from upstream)

Symptoms previously observed:

- Upstream `HTTP 402 Payment Required` from OpenRouter when requests used
  very large output token defaults.

Current mitigation:

- `src/gateway.py` normalizes Responses API token limits for OpenRouter.
- Default cap is `1024`, configurable via `GATEWAY_MAX_OUTPUT_TOKENS`.

Optional override:

```bash
export GATEWAY_MAX_OUTPUT_TOKENS=2048
```

## Case 3: Dangerous Prompt Blocking (Expected 403)

Action:

- Send prompt containing dangerous command text, for example: `rm -rf`

Expected host gateway log:

- `BLOCKED [openrouter/openrouter/auto]: Blocked: dangerous pattern 'rm -rf' detected`
- `POST /v1/responses ... 403 Forbidden`

This confirms policy enforcement is active in the host gateway.

## Quick Health Commands

Host gateway:

```bash
curl -s http://127.0.0.1:8090/health
```

OpenShell status:

```bash
openshell status
openshell provider list
openshell inference get
```

NemoClaw:

```bash
nemoclaw status
nemoclaw list
```
