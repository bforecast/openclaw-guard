# OpenClaw Guard Verification Checklist

This checklist captures the validated end-to-end flow for the current
runtime architecture (V8, updated through 2026-04-16):

- OpenClaw inside sandbox (NemoClaw-managed Docker container)
- `inference.local` route via OpenShell provider `guard`
- host-side security gateway (`guard/gateway.py` on port 8090)
- upstream model provider (OpenRouter / OpenAI / Anthropic)
- MCP via Guard bridge plus OpenClaw native bundle consumption (GitHub MCP tested)

## Prerequisites

- EC2 instance with `ec2_ubuntu_start.sh` completed, WSL with `install_blueprint_wsl.sh`, or macOS with `install_blueprint_mac.sh`.
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

## Case 3: GitHub Native MCP from Sandbox

Requires:
- `GITHUB_MCP_TOKEN` set on the host
- GitHub MCP registered + approved in Guard
- bridge activated
- native bundle staged under `sandbox_workspace/openclaw-data/extensions/guard-mcp-bundle/`

Recommended host-side rollout:

```bash
./install_mcp_bridge.sh github --sandbox my-assistant
```

Verify the bundle is visible inside the sandbox:

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 30 -- \
  sh -lc 'find /sandbox/.openclaw/extensions/guard-mcp-bundle -maxdepth 3 -type f | sort'
```

Expected:
- `/sandbox/.openclaw/extensions/guard-mcp-bundle/.claude-plugin/plugin.json`
- `/sandbox/.openclaw/extensions/guard-mcp-bundle/.mcp.json`

Then run a native agent MCP call:

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 180 -- \
  bash -lc 'openclaw agent --agent main --message "Use the github MCP server to tell me my GitHub login and return only the login." --json --timeout 120'
```

Expected:
- `"status": "ok"`
- payload contains `bforecast`
- `systemPromptReport.tools.entries` contains GitHub MCP tools such as `github__get_me`

## Case 5: Context7 Native MCP from Sandbox

Same recipe as Case 3 (GitHub) with these differences:

- Server name `context7`; upstream `https://mcp.context7.com/mcp`; no
  credential env.
- Tool names become `context7__resolve-library-id` and
  `context7__query-docs`.
- Upstream returns real snippet counts and version lists (LLM cannot
  fabricate them) — this makes Case 5 the sharpest anti-hallucination
  smoke test.

Force a real tool call:

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 180 -- \
  openclaw agent --agent main \
    -m 'Call context7__resolve-library-id with {"query":"Next.js"}. Output only the raw tool response.'
```

Expected rows (2026-04-20 values, numbers drift over time):

```
Context7-compatible library ID: /vercel/next.js    Code Snippets: 2264
Context7-compatible library ID: /websites/nextjs   Code Snippets: 7157
Context7-compatible library ID: /llmstxt/nextjs_llms-full_txt   Code Snippets: 40721
```

Bridge log evidence — pod-source IP `172.18.0.x` is the
openshell-cluster bridge network:

```
BRIDGE-ALLOWED [context7] POST https://mcp.context7.com/mcp -> 200 (1195ms)
172.18.0.2:xxxxx - "POST /mcp/context7/ HTTP/1.1" 200 OK
```

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

## PowerShell to EC2 Lessons

This project has now accumulated a very specific set of lessons for running
`ssh` from Windows PowerShell into EC2 and then further into NemoClaw /
OpenShell sandbox commands.

### Common failure modes

1. PowerShell quoting corrupts remote shell commands
   - Symptoms:
     - `||` rejected by PowerShell before the command reaches SSH
     - variables such as `LANG` or `NO_PROXY` interpreted by PowerShell
     - nested quotes break commands that should have run remotely
   - Safer pattern:
     - prefer a remote script or here-doc over one giant inline command
     - keep `ssh "..."` payloads small

2. Windows CRLF breaks remote bash scripts
   - Symptoms:
     - `$'\\r': command not found`
     - `set: pipefail\r: invalid option name`
   - Fix:
     - normalize to LF before copying to EC2
     - if needed on EC2:
       - `python3 - <<'PY'`
       - rewrite file bytes replacing `\\r\\n` with `\\n`

3. `openshell sandbox exec` rejects multi-line command arguments
   - Symptoms:
     - `status: InvalidArgument`
     - `command argument 2 contains newline or carriage return characters`
   - Fix:
     - pass a single-line command only
     - do not send multi-line `bash -lc` bodies directly through `sandbox exec`

4. Non-interactive SSH sessions may not load PATH
   - Symptoms:
     - `openshell: command not found`
     - `nemoclaw: command not found`
     - binaries exist under `~/.local/bin` but are not found
   - Fix:
     - prefer absolute paths in automation:
       - `/home/ubuntu/.local/bin/openshell`
       - `/home/ubuntu/.local/bin/nemoclaw`
     - do not assume `.bashrc` was loaded

5. `scp` from Windows can be inconsistent with path handling
   - Symptoms:
     - `stat local ... No such file or directory`
     - copied file never appears on EC2
   - Safer pattern:
     - when `scp` is flaky, pipe file content over `ssh`:
       - `Get-Content -Raw localfile | ssh ... "cat > /tmp/file.sh"`

6. `tmux` is the safest way to run long EC2 installs
   - Use it for:
     - `ec2_ubuntu_start.sh`
     - long NemoClaw installs
     - repeated debug commands that should survive SSH disconnects
   - Recommended pattern:
     - start a named session
     - redirect output to a log file
     - inspect the log with `tail`

### Recommended operating procedure

1. Keep complex remote logic in a temporary script, not inside one deeply nested PowerShell string.
2. Normalize scripts to LF before sending them to EC2.
3. Use absolute binary paths for `openshell` and `nemoclaw`.
4. Use `tmux` for installs, rebuilds, and long MCP tests.
5. When testing sandbox commands, keep the `sandbox exec` command body to a single line.
6. If a bridge or MCP test fails, separate the layers:
   - direct upstream test
   - Guard host-side bridge test
   - sandbox-side `mcporter` or OpenClaw test

### Guard-specific lessons from this project

1. `host.openshell.internal` can work differently from `172.17.0.1` or the EC2 private IP.
   - test host aliases instead of assuming one is always correct

2. `inference.local` is inference-only in practice
   - do not reuse it as a generic MCP bridge URL

3. For `host.openshell.internal` in the validated EC2 runtime, keep the
   default sandbox proxy enabled. Direct `172.17.0.1:8090` access returned
   `ECONNREFUSED`, while proxied requests to
   `http://host.openshell.internal:8090/...` succeeded.

4. `mcporter` is debug-only for this project now
   - native OpenClaw MCP is the primary consumer path

5. A successful `GET /mcp` or bridge activation does not prove MCP usability
   - always test actual MCP initialize / native tool execution

## WSL + Docker Desktop Script Fixes (2026-04-19)

Four script changes that together make `install_blueprint_wsl.sh` +
`install_mcp_bridge.sh` work on WSL + Docker Desktop with zero manual
steps. Each fixes a failure mode that is silent or misleading from the
host side.

### 1. Docker Desktop detection (`install_blueprint_wsl.sh`, `2987790`)

Skip `apt-get install docker.io` when `/usr/bin/docker` is already provided
by Docker Desktop's WSL integration. Installing the apt package on top of
Docker Desktop gives you two engines fighting over the socket.

### 2. `detect_docker_bridge_ip()` post-install (`install_blueprint_wsl.sh`, `3c40c71`)

On Docker Desktop the host bridge IP is **`192.168.65.254`**, not
`172.17.0.1`, and it is only resolvable via `--add-host
gateway-probe:host-gateway` against a running engine. The old script
called the detection pre-install when no cluster container existed yet →
returned empty → `GUARD_BRIDGE_ALLOWED_IPS=` empty → sandbox egress 403s
silently against the guard network policy, while host-side
`bridge verify-runtime` looks OK.

Fix: call it exactly once **after** install, auto-apply via
`openshell policy set`:

```bash
IP=$(docker run --rm --add-host gateway-probe:host-gateway alpine \
       getent hosts gateway-probe | awk '{print $1}')
GUARD_BRIDGE_ALLOWED_IPS="$IP" .venv/bin/python -m guard.cli onboard ...
openshell policy set --sandbox my-assistant --allowed-ip "$IP"
```

Works for both Docker CE (`172.17.0.1`) and Docker Desktop (`192.168.65.254`).

### 3. PVC bundle sync with dot-prefixed path (`install_mcp_bridge.sh`, `8cdd40c`)

Host staging and pod-side paths differ by **one dot**:

| Side    | Path                                                             |
|---------|------------------------------------------------------------------|
| Host    | `sandbox_workspace/openclaw-data/extensions/<plugin-id>/`        |
| Pod PVC | `<pvc-root>/.openclaw-data/extensions/<plugin-id>/`              |

On EC2, k3s mounts `sandbox_workspace/` and the rename is invisible. On
WSL + Docker Desktop the PVC is a Docker volume not backed by
`sandbox_workspace/`, so the copy must target **`.openclaw-data/`** (with
dot) or OpenClaw never sees the bundle.

```bash
cp -r "${BUNDLE_OUTPUT_DIR}/." "${PVC_ROOT}/.openclaw-data/extensions/${PLUGIN_ID}/"
#                                          ^^^^^^^^^^^^^^^^  dot matters
kubectl delete pod my-assistant     # force OpenClaw to reload
```

Verify from both sides:

```bash
docker run --rm -v "openshell-cluster-nemoclaw:/cluster" alpine \
  find /cluster -path '*/.openclaw-data/extensions/<plugin-id>/*'
openshell sandbox exec --name my-assistant --no-tty --timeout 30 -- \
  sh -lc 'ls /sandbox/.openclaw-data/extensions/<plugin-id>/'
```

### 4. Multi-bridge single-shot stage+sync+restart (`install_mcp_bridge.sh`, `cd57037`)

Naive per-bridge loop triggers a **pod restart race** on WSL + Docker
Desktop: the second bridge's activation probe hits the pod mid-restart,
fails, and only the first bridge lands in `.mcp.json`.

The same loop is also wrong on EC2 in a different way: each
`stage-openclaw-bundle --output-dir $BUNDLE_OUTPUT_DIR` writes a full
`.mcp.json` and **overwrites** the previous bridge's entry, so multi-bridge
installs ended up with only the last bridge's server.

Fix: stage each to a temp dir, merge all `mcpServers` into one
`.mcp.json`, single PVC sync, single pod restart:

```bash
for bridge in "${BRIDGES[@]}"; do
  activate / verify-runtime / render-openclaw-bundle    # no staging, no sync
done

MERGED_MCP='{"mcpServers":{}}'
for bridge in "${BRIDGES[@]}"; do
  stage-openclaw-bundle "$bridge" --output-dir "$(mktemp -d)"
  MERGED_MCP=$(merge mcpServers into MERGED_MCP)
done

write "$MERGED_MCP" > "$BUNDLE_OUTPUT_DIR/.mcp.json"
sync-to-pvc                      # once
kubectl delete pod my-assistant  # once
```

Symptom of running the buggy loop: `.mcp.json` on the PVC contains only
one server. Re-running the script "fixes" it accidentally because the pod
is already up the second time — a false recovery.

### Summary table (release gate)

| Commit     | File(s)                                     | Fix                                                                                          |
|------------|---------------------------------------------|----------------------------------------------------------------------------------------------|
| `2987790`  | `install_blueprint_wsl.sh`                  | Skip `apt-get install docker.io` under Docker Desktop                                        |
| `3c40c71`  | `install_blueprint_wsl.sh`, `gateway.yaml`  | `detect_docker_bridge_ip()` post-install + `openshell policy set`                            |
| `8cdd40c`  | `install_mcp_bridge.sh`                     | Copy bundle into PVC at dot-prefixed `.openclaw-data/extensions/...`, restart pod            |
| `cd57037`  | `install_mcp_bridge.sh`, `gateway.yaml`     | Multi-bridge: stage to temp dirs, merge, single sync + single pod restart                    |

Miss any one of these and `install_blueprint_wsl.sh` +
`install_mcp_bridge.sh --all` fails on Docker Desktop in a non-obvious way.

## EC2 + Context7 MCP Lessons (2026-04-20)

Two gaps on the EC2 path that the WSL path had already closed. Both
block `openclaw agent` from reaching `context7` even when the Guard
bridge itself proxies fine at the HTTP layer.

### 1. Post-install policy apply was missing on EC2

Diagnostic: OCSF event stream shows paired
`HTTP:POST [INFO] ALLOWED` → `HTTP:POST [MED] DENIED` for every sandbox
→ `host.openshell.internal:8090` request; `openshell policy get
my-assistant` shows no `guard_bridge_host.endpoints[0].allowed_ips`.

Root cause: `ec2_ubuntu_start.sh` exported `GUARD_BRIDGE_ALLOWED_IPS`
and ran `guard.cli onboard` *before* `install.sh`, but the resulting
Guard policy (`$PROJECT_DIR/policies/openclaw-sandbox.yaml`) was never
consumed — `install.sh` reads the pre-merged `$NEMOCLAW_SRC/nemoclaw-blueprint/`
tree only. The sandbox came up with stock NemoClaw policy, and
OpenShell's SSRF guard rejected anything resolving to an RFC1918 IP.

Fix (now in `ec2_ubuntu_start.sh` L221–252, ported from
`install_blueprint_wsl.sh` L286–310): after `install.sh` returns,
re-detect the bridge IP, re-run `guard.cli onboard` so the policy YAML
gains `allowed_ips`, then push it with `openshell policy set --policy
… my-assistant --wait`.

Verify: `openshell policy get my-assistant` shows `Version > 1` and
the policy contains `guard_bridge_host.endpoints[0].allowed_ips:
["172.17.0.1"]`.

### 2. `install_mcp_bridge.sh` stops at host staging on EC2

Diagnostic: Host staging succeeds
(`Combined bundle written to sandbox_workspace/openclaw-data/extensions/guard-mcp-bundle`)
but `/sandbox/.openclaw-data/extensions/guard-mcp-bundle/` is empty;
`openclaw plugins list` inside the sandbox does not include
`guard-mcp-bundle`.

Root cause: The WSL path uses `kubectl delete pod` + PVC replay to
sync bundle files into the pod; on EC2 the script prints
sandbox-side copy steps as human instructions and does not execute
them. Tracked in `implementation_plan.md §6.3`.

Workaround until productized — `openshell sandbox cp` is not a
subcommand today, so base64-inject through `sandbox exec`. Each exec
body must be **single-line** (see PowerShell lesson 3), so wrap the
`mkdir + cat >` sequence as a single encoded blob:

```bash
PLUGIN_B64=$(base64 -w0 <<<'{ "name": "guard-mcp-bundle" }')
MCP_B64=$(base64 -w0 <<'JSON'
{ "mcpServers": { "context7": { "transport": "streamable-http",
  "url": "http://host.openshell.internal:8090/mcp/context7/" } } }
JSON
)
SCRIPT_B64=$(base64 -w0 <<EOF
set -e
B=/sandbox/.openclaw-data/extensions/guard-mcp-bundle
mkdir -p "\$B/.claude-plugin"
echo "$PLUGIN_B64" | base64 -d > "\$B/.claude-plugin/plugin.json"
echo "$MCP_B64"    | base64 -d > "\$B/.mcp.json"
EOF
)
openshell sandbox exec --name my-assistant --no-tty --timeout 20 -- \
  sh -c "echo $SCRIPT_B64 | base64 -d | sh"
```

Confirm OpenClaw picked it up:

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 20 -- \
  sh -c 'openclaw plugins list 2>/dev/null | grep guard-mcp-bundle'
# → guard-mcp-bundle | bundle | loaded | global:guard-mcp-bundle
```

### Verified Results (2026-04-20, EC2 us-west-1 — context7 E2E)

| Test                                                          | Status | Detail                                                                                     |
|---------------------------------------------------------------|--------|--------------------------------------------------------------------------------------------|
| Sandbox bridge reachability after policy fix                  | OK     | curl `/mcp/context7/` initialize → HTTP 200, Context7 v2.1.7 serverInfo                    |
| `openclaw plugins list` inside sandbox                        | OK     | `guard-mcp-bundle` shown as `loaded`                                                       |
| `systemPromptReport.tools.entries` from `main` agent          | OK     | Contains `context7__resolve-library-id`, `context7__query-docs`                            |
| `openclaw agent` tool call `context7__resolve-library-id`      | OK     | Live Next.js rows returned (`/vercel/next.js` 2264 snippets, `/websites/nextjs` 7157, etc.) |
| `gateway.log` BRIDGE-ALLOWED record                           | OK     | `POST https://mcp.context7.com/mcp -> 200` from pod IP `172.18.0.2`                        |

## Sandbox Architecture Reference

```
/sandbox/.openclaw/              (ro mount from sandbox_workspace/openclaw/)
  openclaw.json                  (immutable runtime config)
  extensions -> /sandbox/.openclaw-data/extensions
  agents/ -> /sandbox/.openclaw-data/agents  (symlink to rw mount)

/sandbox/.openclaw-data/         (rw mount from sandbox_workspace/openclaw-data/)
  extensions/guard-mcp-bundle/
    .claude-plugin/plugin.json
    .mcp.json
  agents/main/agent/
    auth-profiles.json

Binary paths (must match in network policy):
  /usr/local/bin/openclaw
  /usr/local/bin/node
```

## Verified Results (2026-04-16, EC2 us-west-1)

| Test | Status | Detail |
|------|--------|--------|
| Unit tests | 76/76 passed | Both Windows and EC2 Linux |
| Inference chain | OK | Sandbox -> inference.local -> Gateway -> OpenRouter -> "Hello!" |
| Dangerous prompt | OK | `rm -rf` -> 403 Forbidden |
| GitHub native MCP get_me | OK | Returned live login `bforecast` |
| GitHub MCP tool discovery | OK | `systemPromptReport.tools.entries` included `github__get_me` and related tools |
| GitHub MCP via TUI | OK | Live repository data returned from `openclaw tui` |
| Gateway health | OK | openrouter provider active |
| Sandbox status | OK | my-assistant: Ready |

## Final Release Checklist

Use this as the shortest release-gate checklist for the current architecture.

### Host

1. Base install completed with `ec2_ubuntu_start.sh`.
2. `curl -s http://127.0.0.1:8090/health` returns `status: ok`.
3. `openshell inference get` points to the Guard-managed inference route.
4. `GITHUB_MCP_TOKEN` is present on the host for GitHub MCP testing.

### Guard MCP

5. GitHub MCP is registered and approved:

```bash
python -m guard.cli mcp status github
```

Expected:
- `Status: approved`
- upstream URL present

6. Bridge is active:

```bash
python -m guard.cli bridge verify-runtime github --sandbox my-assistant --workspace .
```

Expected:
- all checks show `OK`

### Native MCP

7. Stage the native bundle:

```bash
./install_mcp_bridge.sh github --sandbox my-assistant
```

8. Confirm the bundle is visible inside the sandbox:

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 30 -- \
  sh -lc 'find /sandbox/.openclaw/extensions/guard-mcp-bundle -maxdepth 3 -type f | sort'
```

Expected:
- `.claude-plugin/plugin.json`
- `.mcp.json`

9. Run the native GitHub MCP smoke test:

```bash
openshell sandbox exec --name my-assistant --no-tty --timeout 180 -- \
  bash -lc 'openclaw agent --agent main --message "Use the github MCP server to tell me my GitHub login and return only the login." --json --timeout 120'
```

Expected:
- `"status": "ok"`
- payload text is `bforecast`

### Runtime rules

10. Do not force `NO_PROXY` for `host.openshell.internal` in the validated EC2 runtime.
    Keep the default sandbox proxy path enabled unless a direct route has been separately validated.
