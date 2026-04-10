#!/usr/bin/env bash
# =============================================================================
# End-to-end test: GitHub MCP install -> initialize -> tools/list -> search repos
#
# Usage:
#   bash tests/test_mcp_github_e2e.sh
#
# Prerequisites:
#   - .env contains GITHUB_MCP_TOKEN=ghp_xxx  (or github_pat_xxx)
#   - Python packages: uvicorn, httpx, fastapi, typer, pyyaml
#
# The script will:
#   1. Kill any existing gateway on :8090
#   2. Start a fresh gateway
#   3. Run the full MCP test sequence
#   4. Print report to stdout AND save to logs/test_github_mcp_report.md
#   5. Stop gateway on exit
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
GATEWAY_LOG="$LOG_DIR/test_github_mcp_gateway.log"
AUDIT_DB="$LOG_DIR/security_audit.db"
TEST_REPORT="$LOG_DIR/test_github_mcp_report.md"
GATEWAY_PID=""
PASS=0
FAIL=0
SESSION_ID=""

# ── helpers ──────────────────────────────────────────────────────────
cleanup() {
    if [[ -n "$GATEWAY_PID" ]]; then
        kill "$GATEWAY_PID" 2>/dev/null || true
        wait "$GATEWAY_PID" 2>/dev/null || true
        echo "[cleanup] gateway stopped"
    fi
}
trap cleanup EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

# ── resolve python command ───────────────────────────────────────────
# Auto-detect a working python with uvicorn.  On Windows the Store app
# alias often breaks when PowerShell spawns bash, so we also probe the
# real Store Python path as a last resort.
PY=""
_CANDIDATES=(python3 python py python3.11 python3.12)

# Convert LOCALAPPDATA to unix path  (C:\Users\... -> /c/Users/...)
_LOCAL_UNIX=$(echo "${LOCALAPPDATA:-}" | sed 's|\\|/|g; s|^\([A-Za-z]\):|/\L\1|')
[[ -z "$_LOCAL_UNIX" ]] && _LOCAL_UNIX="/c/Users/${USERNAME:-$USER}/AppData/Local"

# Windows Store Python real paths (not the alias stubs)
_WIN_PATHS=()
for _storedir in "$_LOCAL_UNIX"/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.*/; do
    [[ -f "${_storedir}python.exe" ]] && _WIN_PATHS+=("${_storedir}python.exe")
done
# Also try the alias and Programs paths
_WIN_PATHS+=(
    "$_LOCAL_UNIX/Microsoft/WindowsApps/python3.exe"
    "$_LOCAL_UNIX/Microsoft/WindowsApps/python.exe"
    "$_LOCAL_UNIX/Programs/Python/Python311/python.exe"
    "$_LOCAL_UNIX/Programs/Python/Python312/python.exe"
)

# 1) Try PATH candidates
for _candidate in "${_CANDIDATES[@]}"; do
    if command -v "$_candidate" &>/dev/null \
       && "$_candidate" -c "import uvicorn" 2>/dev/null; then
        PY="$_candidate"
        break
    fi
done

# 2) Try absolute Windows paths
if [[ -z "$PY" ]]; then
    for _wpath in "${_WIN_PATHS[@]}"; do
        if [[ -f "$_wpath" ]] && "$_wpath" -c "import uvicorn" 2>/dev/null; then
            PY="$_wpath"
            break
        fi
    done
fi

[[ -z "$PY" ]] && fail "No python with uvicorn found. Run: pip install uvicorn"
echo "Using: $PY ($($PY --version 2>&1))"

check() {
    local label="$1" ok="$2"
    if [[ "$ok" == "true" ]]; then
        echo "  [PASS] $label"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label"
        FAIL=$((FAIL + 1))
    fi
}

# ── load .env ────────────────────────────────────────────────────────
if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

export GUARD_ADMIN_TOKEN="${GUARD_ADMIN_TOKEN:-test-e2e}"

if [[ -z "${GITHUB_MCP_TOKEN:-}" ]]; then
    fail "GITHUB_MCP_TOKEN is not set. Add it to .env and re-run."
fi
echo "GITHUB_MCP_TOKEN is set (${#GITHUB_MCP_TOKEN} chars)"

# ── kill old gateway on :8090 ────────────────────────────────────────
echo ""
echo ">>> Cleaning up port 8090..."
# Windows: find PID listening on 8090 and kill it
OLD_PID=$(netstat -ano 2>/dev/null | grep ":8090.*LISTENING" | awk '{print $NF}' | head -1 || true)
if [[ -n "$OLD_PID" && "$OLD_PID" != "0" ]]; then
    echo "    Killing old process on :8090 (PID $OLD_PID)"
    taskkill //F //PID "$OLD_PID" > /dev/null 2>&1 || kill "$OLD_PID" 2>/dev/null || true
    sleep 2
fi

# ── start gateway ────────────────────────────────────────────────────
echo ""
echo ">>> Starting gateway..."
$PY -m uvicorn guard.gateway:app \
    --host 127.0.0.1 --port 8090 --log-level info \
    > "$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!
sleep 4

if ! curl -sf http://127.0.0.1:8090/health > /dev/null 2>&1; then
    echo "--- Gateway log ---"
    cat "$GATEWAY_LOG"
    fail "Gateway failed to start on :8090"
fi
echo "    Gateway running (PID $GATEWAY_PID)"

# ── begin report (tee to file and stdout) ─────────────────────────────
exec > >(tee "$TEST_REPORT") 2>&1

echo "# GitHub MCP End-to-End Test Report"
echo ""
echo "Date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 1: Install github MCP via template
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 1: guard mcp install github"
echo '```'
T1_OUT=$($PY -m guard.cli mcp install github --by e2e-tester 2>&1) || true
echo "$T1_OUT"
echo '```'
echo "$T1_OUT" | grep -q "OK installed" && check "install github" "true" || check "install github" "false"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 2: Status check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 2: guard mcp status github"
echo '```'
T2_OUT=$($PY -m guard.cli mcp status github 2>&1) || true
echo "$T2_OUT"
echo '```'
echo "$T2_OUT" | grep -q "Status:.*approved" && check "status approved" "true" || check "status approved" "false"
echo "$T2_OUT" | grep -q "Runtime allow:.*yes" && check "allowlist auto-added" "true" || check "allowlist auto-added" "false"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 3: MCP Initialize (JSON-RPC)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 3: MCP Initialize (JSON-RPC)"
INIT_REQ='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"guard-e2e-test","version":"1.0"}}}'

INIT_RESP=$(curl -sS -X POST http://127.0.0.1:8090/mcp/github/ \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -D - \
    -d "$INIT_REQ" 2>&1)

# Extract session ID
SESSION_ID=$(echo "$INIT_RESP" | grep -i "^mcp-session-id:" | tr -d '\r' | awk '{print $2}' || true)

echo '```'
echo "$INIT_RESP" | head -40
echo '```'
echo ""
echo "Session ID: \`${SESSION_ID:-<none>}\`"
echo ""

echo "$INIT_RESP" | grep -q "github-mcp-server" && check "MCP initialize" "true" || check "MCP initialize" "false"
[[ -n "$SESSION_ID" ]] && check "got session ID" "true" || check "got session ID" "false"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 4: Send initialized notification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 4: notifications/initialized"
NOTIF_CODE=$(curl -sS -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8090/mcp/github/ \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SESSION_ID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' 2>&1)

echo "Response: HTTP $NOTIF_CODE"
[[ "$NOTIF_CODE" == "202" ]] && check "initialized notification" "true" || check "initialized notification" "false"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 5: List available tools (tools/list)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 5: MCP tools/list"
TOOLS_RESP=$(curl -sS -X POST http://127.0.0.1:8090/mcp/github/ \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SESSION_ID" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' 2>&1)

# Count tools
TOOL_COUNT=$(echo "$TOOLS_RESP" | grep -o '"name":' | wc -l)
echo "Tools returned: $TOOL_COUNT"
echo '```'
# Extract just tool names
echo "$TOOLS_RESP" | $PY -c "
import sys, json
raw = sys.stdin.read()
for line in raw.splitlines():
    if line.startswith('data: '):
        data = json.loads(line[6:])
        tools = data.get('result', {}).get('tools', [])
        for t in tools:
            print(f\"  - {t['name']}\")
" 2>/dev/null || echo "$TOOLS_RESP" | head -c 2000
echo '```'
[[ "$TOOL_COUNT" -gt 5 ]] && check "tools/list returned tools" "true" || check "tools/list returned tools" "false"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 6: Call tools/call -> search_repositories (list user repos)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 6: tools/call search_repositories"
CALL_RESP=$(curl -sS -X POST http://127.0.0.1:8090/mcp/github/ \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SESSION_ID" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_repositories","arguments":{"query":"user:bforecast","perPage":30}}}' 2>&1)

echo '```'
# Parse and pretty-print repo list
echo "$CALL_RESP" | $PY -c "
import sys, json
raw = sys.stdin.read()
for line in raw.splitlines():
    if line.startswith('data: '):
        data = json.loads(line[6:])
        text = data.get('result', {}).get('content', [{}])[0].get('text', '')
        repos = json.loads(text)
        print(f\"total_count: {repos['total_count']}\")
        print()
        print(f\"{'#':<4} {'Name':<28} {'Language':<12} {'Private':<8} {'Stars':<6} Created\")
        print(f\"{'--':<4} {'---':<28} {'---':<12} {'---':<8} {'---':<6} ---\")
        for i, r in enumerate(repos['items'], 1):
            print(f\"{i:<4} {r['name']:<28} {str(r.get('language') or '-'):<12} {str(r['private']):<8} {r['stargazers_count']:<6} {r['created_at'][:10]}\")
" 2>/dev/null || echo "$CALL_RESP" | head -c 3000
echo '```'

REPO_COUNT=$(echo "$CALL_RESP" | grep -oP '"total_count":\s*\K[0-9]+' || echo "$CALL_RESP" | grep -o 'total_count.*[0-9]' | grep -o '[0-9]*' | head -1 || echo "0")
# Fallback: count repo names in the parsed output
if [[ "$REPO_COUNT" == "0" ]]; then
    REPO_COUNT=$(echo "$CALL_RESP" | grep -o '"full_name"' | wc -l)
fi
echo ""
echo "Repositories found: $REPO_COUNT"
[[ "$REPO_COUNT" -gt 0 ]] && check "search_repositories returned repos" "true" || check "search_repositories returned repos" "false"
echo "$CALL_RESP" | grep -q "openclaw-guard" && check "openclaw-guard repo visible" "true" || check "openclaw-guard repo visible" "false"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 7: guard mcp status (after calls)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 7: guard mcp status github (after MCP calls)"
echo '```'
$PY -m guard.cli mcp status github 2>&1
echo '```'
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 8: guard mcp logs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 8: guard mcp logs"
echo '```'
$PY -m guard.cli mcp logs --limit 20 2>&1
echo '```'
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 9: Audit DB raw query (via Python since sqlite3 may not exist)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 9: Raw audit DB (mcp_events)"
echo '```'
$PY -c "
import sqlite3
conn = sqlite3.connect('$AUDIT_DB')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT * FROM mcp_events ORDER BY id DESC LIMIT 15').fetchall()
total = conn.execute('SELECT count(*) FROM mcp_events').fetchone()[0]
print(f'Total MCP events in DB: {total}')
print()
print(f'{\"ID\":<4} {\"Timestamp\":<20} {\"Server\":<10} {\"Action\":<10} {\"Decision\":<8} {\"Status\":<7} {\"Latency\":<8} {\"Host\":<28} {\"Actor\"}')
print(f'{\"--\":<4} {\"---\":<20} {\"---\":<10} {\"---\":<10} {\"---\":<8} {\"---\":<7} {\"---\":<8} {\"---\":<28} {\"---\"}')
for r in rows:
    ts = (r['timestamp'] or '')[:19]
    print(f'{r[\"id\"]:<4} {ts:<20} {r[\"server_name\"]:<10} {r[\"action\"]:<10} {(r[\"decision\"] or \"-\"):<8} {str(r[\"upstream_status\"] or \"-\"):<7} {str(r[\"latency_ms\"] or \"-\"):<8} {(r[\"upstream_host\"] or \"-\"):<28} {r[\"actor\"] or \"-\"}')
conn.close()
" 2>&1
echo '```'
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 10: Gateway log analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 10: Gateway log analysis"
echo '```'
echo "--- MCP proxy requests ---"
grep -i "/mcp/" "$GATEWAY_LOG" 2>/dev/null | tail -20 || echo "(no mcp lines)"
echo ""
echo "--- Network policy ---"
grep -i "network policy\|authorize" "$GATEWAY_LOG" 2>/dev/null | tail -5 || echo "(none)"
echo '```'
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 11: Security check - token NOT in logs/db
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 11: Security verification"
TOKEN_PREFIX="${GITHUB_MCP_TOKEN:0:8}"
if grep -q "$TOKEN_PREFIX" "$GATEWAY_LOG" 2>/dev/null; then
    check "token NOT in gateway log" "false"
else
    check "token NOT in gateway log" "true"
fi
# Check audit DB for token leakage
DB_LEAK=$($PY -c "
import sqlite3
conn = sqlite3.connect('$AUDIT_DB')
rows = conn.execute('SELECT * FROM mcp_events').fetchall()
for r in rows:
    for val in r:
        if isinstance(val, str) and '$TOKEN_PREFIX' in val:
            print('LEAK')
            break
conn.close()
" 2>&1)
if [[ -z "$DB_LEAK" ]]; then
    check "token NOT in audit DB" "true"
else
    check "token NOT in audit DB" "false"
fi
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 12: Cleanup - uninstall
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "## Test 12: Cleanup - uninstall"
echo '```'
$PY -m guard.cli mcp uninstall github 2>&1
$PY -m guard.cli mcp list 2>&1
echo '```'

# Verify proxy returns 404 after uninstall
AFTER_CODE=$(curl -sS -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8090/mcp/github/ \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":99,"method":"initialize","params":{}}' 2>&1)
[[ "$AFTER_CODE" == "404" ]] && check "proxy blocked after uninstall" "true" || check "proxy blocked after uninstall" "false"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "---"
echo ""
echo "## Summary"
echo ""
TOTAL=$((PASS + FAIL))
echo "**$PASS/$TOTAL PASS** ($FAIL failed)"
echo ""
echo "Test completed at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
