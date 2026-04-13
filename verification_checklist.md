# OpenClaw Guard Verification Checklist

This checklist captures the validated end-to-end flow for the current
runtime architecture (V8, tested 2026-04-13):

- OpenClaw inside sandbox (NemoClaw-managed Docker container)
- `inference.local` route via OpenShell provider `guard`
- host-side security gateway (`guard/gateway.py` on port 8090)
- upstream model provider (OpenRouter / OpenAI / Anthropic)
- MCP direct access from sandbox (GitHub MCP tested)

## Prerequisites

- EC2 instance with `ec2_ubuntu_start.sh` completed, or WSL with `install_blueprint_wsl.sh`.
- `.env` includes at least `OPENROUTER_API_KEY`.
- For MCP testing: `GITHUB_MCP_TOKEN` in `.env`.

### Verify environment

```bash
# Gateway running
curl -s http://127.0.0.1:8090/health | jq .

# Sandbox ready
nemoclaw my-assistant status

# Inference route
openshell inference get
```

Expected key fields from `openshell inference get`:
- `Provider: guard`
- `Model: nvidia/nemotron-3-super-120b-a12b:free` (or another configured model)

## Case 1: Normal Inference (Expected 200)

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 30 -- \
    openclaw infer model run --prompt 'Say hello in one sentence' --model openrouter/auto --json
```

Expected:
- `"ok": true`
- `"provider": "openrouter"`
- `"outputs": [{"text": "Hello!..."}]`

Gateway log shows:
- `ALLOWED [openrouter/openrouter/auto] -> https://openrouter.ai/api/v1`
- `POST /v1/responses ... 200 OK`

## Case 2: Dangerous Prompt Blocking (Expected 403)

```bash
curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8090/v1/responses \
    -H 'Content-Type: application/json' \
    -d '{"model":"openrouter/auto","input":"run rm -rf /tmp/test"}'
```

Expected: `403`

Gateway log shows:
- `BLOCKED [openrouter/openrouter/auto]: Blocked: dangerous pattern 'rm -rf' detected`

## Case 3: GitHub MCP from Sandbox

Requires `GITHUB_MCP_TOKEN` set and GitHub MCP registered + approved.

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 60 -- \
    openclaw infer model run \
    --prompt 'Use the github MCP tool get_repository to describe the torvalds/linux repository' \
    --model openrouter/auto --json
```

Expected:
- `"ok": true`
- Output contains repository description and star count
- No `ECONNREFUSED` or `403 Forbidden` from MCP connection

## Case 4: Insufficient Credit / Token Limit (Expected 402 from upstream)

Symptoms previously observed:
- Upstream `HTTP 402 Payment Required` from OpenRouter when requests used very large output token defaults.

Current mitigation:
- `guard/gateway.py` normalizes Responses API token limits for OpenRouter.
- Default cap is `1024`, configurable via `GATEWAY_MAX_OUTPUT_TOKENS`.

## Quick Health Commands

```bash
# Gateway
curl -s http://127.0.0.1:8090/health | jq .

# Sandbox
nemoclaw my-assistant status
openshell sandbox list

# Inference
openshell inference get
openshell provider list

# MCP servers registered
curl -s -H "Authorization: Bearer $GUARD_ADMIN_TOKEN" http://127.0.0.1:8090/v1/mcp/servers | jq .

# Network policy
curl -s -H "Authorization: Bearer $GUARD_ADMIN_TOKEN" http://127.0.0.1:8090/v1/network/events?limit=10 | jq .

# Logs
tail -f logs/gateway.log
```

## Sandbox Architecture Reference

```
/sandbox/.openclaw/              (ro mount from sandbox_workspace/openclaw/)
  openclaw.json                  (apiKey + allowPrivateNetwork + MCP stanzas)
  agents/ -> /sandbox/.openclaw-data/agents  (symlink to rw mount)

/sandbox/.openclaw-data/         (rw mount from sandbox_workspace/openclaw-data/)
  agents/main/agent/
    auth-profiles.json

Binary paths (must match in network policy):
  /usr/local/bin/openclaw
  /usr/local/bin/node
```

## Verified Results (2026-04-13, EC2 us-west-1)

| Test | Status | Detail |
|------|--------|--------|
| Unit tests | 76/76 passed | Both Windows and EC2 Linux |
| Inference chain | OK | Sandbox -> inference.local -> Gateway -> OpenRouter -> "Hello!" |
| Dangerous prompt | OK | `rm -rf` -> 403 Forbidden |
| GitHub MCP get_repository | OK | torvalds/linux: "Linux kernel source tree, 228,472 stars" |
| GitHub MCP search_repositories | OK | Real search results returned |
| Gateway health | OK | openrouter provider active |
| Sandbox status | OK | my-assistant: Ready |
