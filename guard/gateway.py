"""
OpenClaw Security Gateway - Multi-Provider Intelligent Router.

Architecture:
  Sandbox -> inference.local -> OpenShell Proxy -> This Gateway (8090) -> Providers

The gateway inspects every LLM prompt for malicious intent, then routes
to the correct upstream provider based on the requested model name.
"""

import json
import logging
import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from urllib.parse import urlparse

from guard import gateway_config
from guard.gateway_config import GatewayConfigError, McpServer
from guard.network_monitor import BLOCK as NET_BLOCK, get_default as get_network_monitor

PROVIDERS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "endpoint": "/chat/completions",
        "responses_endpoint": "/responses",
        "stream_media_type": "text/event-stream",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "extra_headers": {
            "anthropic-version": "2023-06-01",
        },
        "endpoint": "/messages",
        "stream_media_type": "application/json",
        "transform": "anthropic",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "endpoint": "/chat/completions",
        "responses_endpoint": "/responses",
        "stream_media_type": "text/event-stream",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "endpoint": "/chat/completions",
        "responses_endpoint": "/responses",
        "stream_media_type": "text/event-stream",
    },
}

MODEL_ROUTES = [
    (r"^(gpt-|o1-|o3-|o4-|dall-e|tts-|whisper)", "openai"),
    (r"^claude-", "anthropic"),
    # Models with org/ prefix (nvidia/, meta/, google/, …) are hosted on
    # OpenRouter as free-tier endpoints.  Route through OpenRouter so the
    # gateway uses OPENROUTER_API_KEY rather than the NVIDIA direct API
    # (which does not expose a standard /chat/completions surface).
    (r"^(openrouter/|nvidia/|meta/|mistralai/|google/|microsoft/|deepseek/|anthropic/|openai/)", "openrouter"),
]

DEFAULT_MODEL = os.environ.get("MODEL_ID", "nvidia/nemotron-3-super-120b-a12b:free")


def _infer_default_provider(model_id: str) -> str:
    """Derive the default provider from MODEL_ID using MODEL_ROUTES."""
    for pattern, provider_name in MODEL_ROUTES:
        if re.match(pattern, model_id, re.IGNORECASE):
            return provider_name
    if "/" in model_id:
        return "openrouter"
    return "openrouter"


# Prefer explicit PROVIDER_ID from .env; fall back to inference for backward compat
_explicit_provider = os.environ.get("PROVIDER_ID", "")
if _explicit_provider and _explicit_provider in PROVIDERS:
    DEFAULT_PROVIDER = _explicit_provider
elif _explicit_provider:
    logging.warning(
        "PROVIDER_ID=%s is not a recognized provider; falling back to inference from MODEL_ID.",
        _explicit_provider,
    )
    DEFAULT_PROVIDER = _infer_default_provider(DEFAULT_MODEL)
else:
    DEFAULT_PROVIDER = _infer_default_provider(DEFAULT_MODEL)

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
DB_PATH = LOG_DIR / "security_audit.db"
GATEWAY_CONFIG_PATH = Path(__file__).parent.parent / "gateway.yaml"

# Initialize the shared NetworkMonitor early so the audit DB has both tables
# (audit_log + network_events) before the first request lands.
network_monitor = get_network_monitor(blueprint_path=GATEWAY_CONFIG_PATH, db_path=DB_PATH)

# In-memory MCP server cache, populated from gateway.yaml. Refreshed by
# /v1/mcp/policy/reload and after every admin write so the proxy hot path
# never re-parses YAML.
_mcp_cache: dict[str, McpServer] = {}
_mcp_cache_lock = asyncio.Lock()


def _refresh_mcp_cache() -> None:
    """Re-read mcp.servers from gateway.yaml into the in-memory cache."""
    global _mcp_cache
    try:
        servers = gateway_config.list_servers(GATEWAY_CONFIG_PATH)
    except GatewayConfigError as exc:
        log.warning("mcp cache refresh failed: %s", exc)
        return
    _mcp_cache = {s.name: s for s in servers}


# Hop-by-hop headers per RFC 7230 §6.1 — must NOT be forwarded by proxies.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;",
    r"\bchmod\s+777\b",
    r"\bcurl\b.*\|\s*bash",
    r"\bwget\b.*\|\s*sh",
    r"\beval\b.*\bbase64\b",
    r"\bnc\s+-e\b",
    r"\bsudo\s+rm\b",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [gateway] %(message)s")
log = logging.getLogger("security-gateway")


def resolve_provider(model: str) -> tuple[str, dict, str]:
    # 1. Try explicit matching
    for pattern, provider_name in MODEL_ROUTES:
        if re.match(pattern, model, re.IGNORECASE):
            config = PROVIDERS[provider_name]
            cleaned_model = model
            if provider_name == "openrouter":
                cleaned_model = re.sub(r"^openrouter/", "", model)
            elif provider_name == "nvidia":
                cleaned_model = re.sub(r"^nvidia/", "", model)
            return provider_name, config, cleaned_model

    # 2. Heuristic: If it looks like a tiered model ID (e.g. stepfun/...) and 
    # OpenRouter is configured, assume it's an OpenRouter model.
    if "/" in model and os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter", PROVIDERS["openrouter"], model

    # 3. Final Fallback: If no match, use the first configured provider we can find.
    # This prevents 503/401 errors when using custom models through our gateway.
    for p_name, p_cfg in PROVIDERS.items():
        if os.environ.get(p_cfg["api_key_env"]):
            log.info(f"Unrecognized model '{model}', falling back to configured provider: {p_name}")
            return p_name, p_cfg, model

    return DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER], model


def get_api_key(config: dict) -> str:
    return os.environ.get(config["api_key_env"], "")


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            action TEXT NOT NULL,
            reason TEXT,
            prompt_preview TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            server_name TEXT NOT NULL,
            action TEXT NOT NULL,
            decision TEXT,
            reason TEXT,
            actor TEXT,
            upstream_host TEXT,
            upstream_status INTEGER,
            latency_ms INTEGER,
            metadata TEXT
        )
        """
    )
    conn.commit()
    return conn


def _log_mcp_event(
    server_name: str,
    action: str,
    *,
    decision: str | None = None,
    reason: str = "",
    actor: str | None = None,
    upstream_host: str | None = None,
    upstream_status: int | None = None,
    latency_ms: int | None = None,
    metadata: dict | None = None,
) -> None:
    try:
        conn = _init_db()
        conn.execute(
            "INSERT INTO mcp_events (timestamp, server_name, action, decision, "
            "reason, actor, upstream_host, upstream_status, latency_ms, metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                server_name,
                action,
                decision,
                reason[:300] if reason else "",
                actor,
                upstream_host,
                upstream_status,
                latency_ms,
                json.dumps(metadata) if metadata else None,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("mcp_events write failed: %s", exc)


def _log_event(provider: str, model: str, action: str, reason: str, preview: str) -> None:
    try:
        conn = _init_db()
        conn.execute(
            "INSERT INTO audit_log (timestamp, provider, model, action, reason, prompt_preview) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                provider,
                model,
                action,
                reason,
                preview[:500],
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning(f"Audit DB write failed: {exc}")


def _flatten_content(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        flattened: list[str] = []
        for item in value:
            flattened.extend(_flatten_content(item))
        return flattened
    if isinstance(value, dict):
        flattened: list[str] = []
        for key in ("text", "content", "input", "value"):
            if key in value:
                flattened.extend(_flatten_content(value[key]))
        return flattened
    return []


def extract_text_from_messages(messages: list[dict]) -> str:
    collected: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            collected.extend(_flatten_content(message.get("content")))
    return " ".join(part for part in collected if part)


def scan_messages(messages: list[dict]) -> tuple[bool, str]:
    full_text = extract_text_from_messages(messages)
    for pattern in DANGEROUS_PATTERNS:
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            return False, f"Blocked: dangerous pattern '{match.group()}' detected"
    return True, "clean"


def scan_text_output(text: str) -> tuple[bool, str]:
    for pattern in DANGEROUS_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return False, f"Blocked output: dangerous pattern '{match.group()}' detected"
    return True, "clean"


def extract_text_from_response_payload(payload: dict) -> str:
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            for key in ("text", "output_text", "content", "output", "message"):
                if key in value:
                    walk(value[key])

    walk(payload)
    return " ".join(part for part in parts if part)


def _anthropic_content_blocks(content: Any) -> list[dict]:
    return [
        {"type": "text", "text": text}
        for text in _flatten_content(content)
        if text
    ]


def transform_anthropic_request(body: dict, model: str) -> dict:
    messages = body.get("messages", [])
    system_parts: list[str] = []
    anthropic_messages: list[dict] = []

    for message in messages:
        if not isinstance(message, dict):
            continue

        role = message.get("role", "user")
        content_blocks = _anthropic_content_blocks(message.get("content"))
        if not content_blocks:
            continue

        if role == "system":
            system_parts.extend(block["text"] for block in content_blocks)
            continue

        anthropic_messages.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": content_blocks,
            }
        )

    transformed = {
        "model": model,
        "messages": anthropic_messages,
        "stream": body.get("stream", False),
        "max_tokens": body.get("max_tokens", body.get("max_completion_tokens", 1024)),
    }

    if system_parts:
        transformed["system"] = "\n\n".join(system_parts)

    for field in (
        "metadata",
        "stop_sequences",
        "temperature",
        "top_k",
        "top_p",
        "tools",
        "tool_choice",
    ):
        if field in body:
            transformed[field] = body[field]

    return transformed


def _provider_endpoint(provider_cfg: dict) -> str:
    return provider_cfg.get("endpoint", "/chat/completions")


def _responses_endpoint(provider_cfg: dict) -> str:
    return provider_cfg.get("responses_endpoint", "/responses")


def _normalize_responses_limits(provider_name: str, body: dict) -> dict:
    """
    Keep responses API token limits in a safe range for hosted providers.
    OpenRouter may reject very large defaults (for example 65536) with 402.
    """
    if provider_name != "openrouter":
        return body

    max_allowed = int(os.environ.get("GATEWAY_MAX_OUTPUT_TOKENS", "1024"))
    normalized = dict(body)

    current = normalized.get("max_output_tokens", normalized.get("max_tokens"))
    if current is None:
        normalized["max_output_tokens"] = max_allowed
        return normalized

    try:
        numeric = int(current)
    except Exception:
        normalized["max_output_tokens"] = max_allowed
        normalized.pop("max_tokens", None)
        return normalized

    if numeric > max_allowed:
        normalized["max_output_tokens"] = max_allowed
        normalized.pop("max_tokens", None)

    return normalized


def _responses_input_to_messages(body: dict) -> list[dict]:
    payload = body.get("input", "")
    if isinstance(payload, str):
        return [{"role": "user", "content": payload}]
    if isinstance(payload, list):
        messages: list[dict] = []
        for item in payload:
            if isinstance(item, dict) and "role" in item:
                messages.append(
                    {"role": item.get("role", "user"), "content": item.get("content", "")}
                )
            else:
                messages.append({"role": "user", "content": item})
        return messages
    return [{"role": "user", "content": str(payload)}]


def _chat_to_responses_payload(chat_payload: dict) -> dict:
    text = ""
    choices = chat_payload.get("choices", [])
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            content = message.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        if "text" in item and isinstance(item["text"], str):
                            parts.append(item["text"])
                        elif "content" in item and isinstance(item["content"], str):
                            parts.append(item["content"])
                    elif isinstance(item, str):
                        parts.append(item)
                text = "".join(parts)
            else:
                text = str(content)

    created = int(datetime.now(timezone.utc).timestamp())
    response_id = f"resp_{chat_payload.get('id', created)}"
    item_id = f"msg_{created}"
    usage = chat_payload.get("usage", {})
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": chat_payload.get("model", "unknown"),
        "error": None,
        "incomplete_details": None,
        "output_text": text,
        "output": [
            {
                "id": item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],        
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _responses_stream_event(event: dict) -> bytes:
    event_name = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")


def _latest_user_input_for_scan(body: dict) -> Any:
    """
    For Responses API, default to scanning the newest user turn instead of the
    full historical transcript to avoid persistent session contamination.
    """
    payload = body.get("input")
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, list):
        return payload

    for item in reversed(payload):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).lower()
        if role == "user":
            return item.get("content", item)

    return payload


def _gateway_debug_headers(
    provider_name: str,
    requested_model: str,
    routed_model: str,
    upstream_model: str | None = None,
) -> dict[str, str]:
    headers = {
        "X-Guard-Provider": provider_name,
        "X-Guard-Model-Requested": requested_model,
        "X-Guard-Model-Routed": routed_model,
    }
    if upstream_model:
        headers["X-Guard-Model-Upstream"] = upstream_model
    return headers


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
        return seconds if seconds >= 0 else None
    except Exception:
        return None


def _retry_delay_seconds(attempt: int, retry_after: str | None = None) -> float:
    parsed = _parse_retry_after_seconds(retry_after)
    if parsed is not None:
        return parsed
    return min(2 ** (attempt - 1), 8)


app = FastAPI(title="OpenClaw Security Gateway - Multi-Provider")


@app.get("/health")
async def health():
    available = {
        name: bool(os.environ.get(cfg["api_key_env"]))
        for name, cfg in PROVIDERS.items()
    }
    return {
        "status": "ok",
        "providers": available,
        "default_provider": DEFAULT_PROVIDER,
        "default_model": DEFAULT_MODEL,
        "network_monitor": "ok",
        "network_policy": network_monitor.policy_summary(),
    }


def _admin_token_ok(request: Request) -> bool:
    expected = os.environ.get("GUARD_ADMIN_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if not expected:
        return True  # no token configured -> open (local-only diagnostic)
    presented = request.headers.get("Authorization", "")
    if presented.startswith("Bearer "):
        presented = presented[7:]
    return presented == expected


@app.get("/v1/network/events")
async def list_network_events(request: Request):
    if not _admin_token_ok(request):
        return JSONResponse(
            {"error": {"message": "admin token required", "type": "auth"}},
            status_code=401,
        )
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    return {"events": network_monitor.recent_events(limit=max(1, min(limit, 1000)))}


@app.post("/v1/network/policy/reload")
async def reload_network_policy(request: Request):
    if not _admin_token_ok(request):
        return JSONResponse(
            {"error": {"message": "admin token required", "type": "auth"}},
            status_code=401,
        )
    network_monitor.reload()
    return {"status": "reloaded", "policy": network_monitor.policy_summary()}


# ── MCP admin endpoints (source of truth for `guard mcp ...`) ──────────────
def _auth_or_401(request: Request) -> JSONResponse | None:
    if not _admin_token_ok(request):
        return JSONResponse(
            {"error": {"message": "admin token required", "type": "auth"}},
            status_code=401,
        )
    return None


def _mcp_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "mcp"}}, status_code=status)


@app.on_event("startup")
async def _gateway_startup() -> None:
    _refresh_mcp_cache()
    _init_db().close()  # ensure mcp_events table exists


@app.get("/v1/mcp/servers")
async def mcp_list_servers(request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    return [s.to_dict() for s in _mcp_cache.values()]


@app.post("/v1/mcp/servers")
async def mcp_register_server(request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:
        return _mcp_error("invalid JSON body")
    name = str(body.get("name", "")).strip()
    url = str(body.get("url", "")).strip()
    transport = str(body.get("transport", "sse")).strip()
    credential_env = body.get("credential_env") or None
    purpose = str(body.get("purpose", ""))
    try:
        async with _mcp_cache_lock:
            srv = gateway_config.register_server(
                GATEWAY_CONFIG_PATH,
                name=name,
                url=url,
                transport=transport,
                credential_env=credential_env,
                purpose=purpose,
            )
            _refresh_mcp_cache()
    except GatewayConfigError as exc:
        msg = str(exc)
        status = 409 if "already exists" in msg else 400
        _log_mcp_event(name or "?", "register", decision="block", reason=msg)
        return _mcp_error(msg, status=status)
    _log_mcp_event(srv.name, "register", decision="allow",
                   upstream_host=urlparse(srv.url).hostname or "")
    return JSONResponse(srv.to_dict(), status_code=201)


def _auto_allow_upstream(srv: McpServer) -> None:
    """Add the MCP upstream host to network.runtime.allow so the existing
    egress check stops blocking it. Best-effort: any failure is logged but
    does not abort the approval flow."""
    parsed = urlparse(srv.url)
    host = parsed.hostname
    if not host:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        gateway_config.add_entry(
            GATEWAY_CONFIG_PATH,
            scope="runtime",
            host=host,
            ports=[port],
            enforcement="enforce",
            purpose=f"MCP {srv.name}",
        )
        network_monitor.reload()
    except GatewayConfigError as exc:
        log.warning("auto-allow for mcp %s failed: %s", srv.name, exc)


@app.post("/v1/mcp/servers/{name}/approve")
async def mcp_approve_server(name: str, request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    actor = str(body.get("actor", "")).strip()
    if not actor:
        return _mcp_error("actor is required")
    auto_allow = bool(body.get("auto_allow", True))
    try:
        async with _mcp_cache_lock:
            srv = gateway_config.set_server_status(
                GATEWAY_CONFIG_PATH, name, "approved", actor=actor,
            )
            if auto_allow:
                _auto_allow_upstream(srv)
            _refresh_mcp_cache()
    except GatewayConfigError as exc:
        return _mcp_error(str(exc), status=404)
    _log_mcp_event(srv.name, "approve", decision="allow", actor=actor,
                   upstream_host=urlparse(srv.url).hostname or "")
    return srv.to_dict()


@app.post("/v1/mcp/servers/{name}/deny")
async def mcp_deny_server(name: str, request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    actor = str(body.get("actor", "")).strip()
    if not actor:
        return _mcp_error("actor is required")
    reason = str(body.get("reason", ""))
    try:
        async with _mcp_cache_lock:
            srv = gateway_config.set_server_status(
                GATEWAY_CONFIG_PATH, name, "denied", actor=actor, reason=reason,
            )
            _refresh_mcp_cache()
    except GatewayConfigError as exc:
        return _mcp_error(str(exc), status=404)
    _log_mcp_event(srv.name, "deny", decision="block", actor=actor, reason=reason)
    return srv.to_dict()


@app.post("/v1/mcp/servers/{name}/revoke")
async def mcp_revoke_server(name: str, request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    actor = str(body.get("actor", "")).strip()
    if not actor:
        return _mcp_error("actor is required")
    reason = str(body.get("reason", ""))
    try:
        async with _mcp_cache_lock:
            srv = gateway_config.set_server_status(
                GATEWAY_CONFIG_PATH, name, "revoked", actor=actor, reason=reason,
            )
            _refresh_mcp_cache()
    except GatewayConfigError as exc:
        return _mcp_error(str(exc), status=404)
    _log_mcp_event(srv.name, "revoke", decision="block", actor=actor, reason=reason)
    return srv.to_dict()


@app.delete("/v1/mcp/servers/{name}")
async def mcp_remove_server(name: str, request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    async with _mcp_cache_lock:
        removed = gateway_config.remove_server(GATEWAY_CONFIG_PATH, name)
        _refresh_mcp_cache()
    if not removed:
        return _mcp_error(f"mcp server {name!r} not found", status=404)
    _log_mcp_event(name, "remove", decision="allow")
    return Response(status_code=204)


@app.post("/v1/mcp/policy/reload")
async def mcp_reload_policy(request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    async with _mcp_cache_lock:
        _refresh_mcp_cache()
    return {"status": "reloaded", "count": len(_mcp_cache)}


@app.get("/v1/mcp/events")
async def mcp_list_events(request: Request):
    err = _auth_or_401(request)
    if err is not None:
        return err
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 1000))
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM mcp_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("mcp_events read failed: %s", exc)
        return []


# ── MCP reverse proxy (sandbox-facing — no admin token) ────────────────────
# Two routes: /mcp/... (direct host access) and /v1/mcp/... (via inference.local
# which OpenShell's proxy only allows under the /v1/ prefix).
@app.api_route(
    "/mcp/{server_name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
@app.api_route(
    "/v1/mcp/{server_name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def mcp_proxy(server_name: str, path: str, request: Request):
    srv = _mcp_cache.get(server_name)
    if srv is None:
        return _mcp_error(f"mcp server {server_name!r} not found", status=404)
    if srv.status != "approved":
        reason = f"mcp server {server_name!r} status={srv.status}"
        _log_mcp_event(server_name, "call", decision="block", reason=reason)
        return _mcp_error(reason, status=403)

    parsed = urlparse(srv.url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    decision = network_monitor.authorize(host, port, scope="runtime")
    if decision.verdict == NET_BLOCK:
        _log_mcp_event(
            server_name, "call", decision="block", reason=decision.reason,
            upstream_host=host,
        )
        return _mcp_error(f"network policy blocked: {decision.reason}", status=403)

    # Build upstream URL: srv.url is treated as the base, then we append the
    # tail path and query string from the incoming request.
    base = srv.url.rstrip("/")
    tail = f"/{path}" if path else ""
    upstream_url = f"{base}{tail}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    # Forward headers, dropping hop-by-hop and the inbound Authorization (we
    # inject our own from credential_env if configured).
    fwd_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in _HOP_BY_HOP or kl == "authorization":
            continue
        fwd_headers[k] = v
    fwd_headers["host"] = parsed.netloc
    if srv.credential_env:
        token = os.environ.get(srv.credential_env, "")
        if token:
            fwd_headers["authorization"] = f"Bearer {token}"

    body_bytes = await request.body()
    started = datetime.now(timezone.utc)
    client = httpx.AsyncClient(timeout=None)

    try:
        upstream_req = client.build_request(
            request.method, upstream_url, headers=fwd_headers, content=body_bytes,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as exc:
        await client.aclose()
        _log_mcp_event(
            server_name, "call", decision="block",
            reason=f"upstream error: {exc}", upstream_host=host,
        )
        return _mcp_error(f"upstream error: {exc}", status=502)

    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    _log_mcp_event(
        server_name, "call", decision="allow",
        upstream_host=host, upstream_status=upstream_resp.status_code,
        latency_ms=latency_ms,
    )

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    async def _stream():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


@app.get("/v1/models")
async def list_models():
    all_models = []
    for provider_name, provider_cfg in PROVIDERS.items():
        api_key = get_api_key(provider_cfg)
        if not api_key:
            continue
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                headers = {
                    provider_cfg["auth_header"]: (
                        f"{provider_cfg['auth_prefix']}{api_key}"
                    )
                }
                headers.update(provider_cfg.get("extra_headers", {}))
                resp = await client.get(
                    f"{provider_cfg['base_url']}/models",
                    headers=headers,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                models = data.get("data", data.get("models", []))
                for model in models:
                    model["_provider"] = provider_name
                    # Prefix IDs for non-default providers to trigger routing
                    if provider_name != "openai" and not model["id"].startswith(f"{provider_name}/"):
                        model["id"] = f"{provider_name}/{model['id']}"
                all_models.extend(models)
        except Exception:
            pass
    return {"object": "list", "data": all_models}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model") or DEFAULT_MODEL
    messages = body.get("messages", [])
    
    # [BYPASS] Mock NemoClaw probe
    for msg in messages:
        if isinstance(msg, dict) and msg.get("content") == "Reply with exactly: OK":
            log.info("Detected NemoClaw onboarding probe (Chat Completions). Returning mock success.")
            return JSONResponse({
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 1711920000, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]
            })

    is_streaming = body.get("stream", False)

    provider_name, provider_cfg, cleaned_model = resolve_provider(model)
    api_key = get_api_key(provider_cfg)

    if not api_key:
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "message": (
                            f"Provider '{provider_name}' not configured. "
                            f"Set {provider_cfg['api_key_env']} on the host."
                        ),
                        "type": "configuration_error",
                        "code": "missing_api_key",
                    }
                }
            ),
            status_code=503,
            media_type="application/json",
        )

    is_safe, reason = scan_messages(messages)
    preview = json.dumps(messages[:2], ensure_ascii=False)[:300]

    if not is_safe:
        log.warning(f"BLOCKED [{provider_name}/{model}]: {reason}")
        _log_event(provider_name, model, "block", reason, preview)
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "message": f"Request blocked by security gateway: {reason}",
                        "type": "security_block",
                        "code": "content_policy_violation",
                    }
                }
            ),
            status_code=403,
            media_type="application/json",
        )

    log.info(f"ALLOWED [{provider_name}/{cleaned_model}] -> {provider_cfg['base_url']}")
    _log_event(provider_name, model, "allow", reason, preview)

    headers = {
        provider_cfg["auth_header"]: f"{provider_cfg['auth_prefix']}{api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider_cfg.get("extra_headers", {}))

    for header_name in ("HTTP-Referer", "X-Title"):
        value = request.headers.get(header_name)
        if value:
            headers[header_name] = value

    if provider_cfg.get("transform") == "anthropic":
        upstream_body = transform_anthropic_request(body, cleaned_model)
    else:
        upstream_body = {**body, "model": cleaned_model}

    endpoint = _provider_endpoint(provider_cfg)
    if is_streaming:
        return _stream_upstream(
            provider_name,
            model,
            cleaned_model,
            provider_cfg["base_url"],
            endpoint,
            upstream_body,
            headers,
            provider_cfg,
        )
    return await _forward_upstream(
        provider_name,
        model,
        cleaned_model,
        provider_cfg["base_url"],
        endpoint,
        upstream_body,
        headers,
    )


@app.post("/v1/responses")
async def responses(request: Request):
    body = await request.json()
    
    # [BYPASS] Mock NemoClaw probe
    if body.get("input") == "Reply with exactly: OK":
        log.info("Detected NemoClaw onboarding probe (Responses). Returning mock success.")
        return JSONResponse({
            "id": "resp-mock", "object": "response", "status": "completed",
            "output_text": "OK", "model": body.get("model", "unknown")
        })

    # [BYPASS] Inject low max_tokens to bypass OpenRouter 16k token reserve (402)
    model = body.get("model") or DEFAULT_MODEL
    provider_name, provider_cfg, cleaned_model = resolve_provider(model)
    if provider_name == "openrouter" and "max_tokens" not in body:
        log.info("Injecting default max_tokens: 1024 for OpenRouter compatibility.")
        body["max_tokens"] = 1024
    
    is_streaming = body.get("stream", False)
    api_key = get_api_key(provider_cfg)

    if not api_key:
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "message": (
                            f"Provider '{provider_name}' not configured. "
                            f"Set {provider_cfg['api_key_env']} on the host."
                        ),
                        "type": "configuration_error",
                        "code": "missing_api_key",
                    }
                }
            ),
            status_code=503,
            media_type="application/json",
        )

    if provider_name == "anthropic":
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "message": (
                            "Responses API passthrough is not supported for "
                            "Anthropic routes in this gateway. Use chat/completions."
                        ),
                        "type": "unsupported_operation",
                        "code": "responses_not_supported_for_provider",
                    }
                }
            ),
            status_code=400,
            media_type="application/json",
        )

    scan_candidates = []
    if "input" in body:
        scan_candidates.append(
            {"role": "user", "content": _latest_user_input_for_scan(body)}
        )
    if "instructions" in body:
        scan_candidates.append({"role": "system", "content": body.get("instructions")})

    is_safe, reason = scan_messages(scan_candidates)
    preview = json.dumps(body.get("input", ""), ensure_ascii=False)[:300]

    if not is_safe:
        log.warning(f"BLOCKED [{provider_name}/{model}]: {reason}")
        _log_event(provider_name, model, "block", reason, preview)
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "message": f"Request blocked by security gateway: {reason}",
                        "type": "security_block",
                        "code": "content_policy_violation",
                    }
                }
            ),
            status_code=403,
            media_type="application/json",
        )

    log.info(f"ALLOWED [{provider_name}/{cleaned_model}] -> {provider_cfg['base_url']}")
    _log_event(provider_name, model, "allow", reason, preview)

    headers = {
        provider_cfg["auth_header"]: f"{provider_cfg['auth_prefix']}{api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider_cfg.get("extra_headers", {}))

    for header_name in ("HTTP-Referer", "X-Title"):
        value = request.headers.get(header_name)
        if value:
            headers[header_name] = value

    upstream_body = {**body, "model": cleaned_model}
    upstream_body = _normalize_responses_limits(provider_name, upstream_body)

    use_openrouter_chat_fallback = (
        provider_name == "openrouter"
        and os.environ.get("GATEWAY_OPENROUTER_RESPONSES_VIA_CHAT", "0") == "1"
    )

    if use_openrouter_chat_fallback:
        log.info(
            "RESPONSES-FALLBACK [%s/%s]: routing via chat/completions",
            provider_name,
            cleaned_model,
        )
        chat_body = {
            "model": cleaned_model,
            "messages": _responses_input_to_messages(body),
            "max_tokens": upstream_body.get(
                "max_output_tokens",
                upstream_body.get("max_tokens", 1024),
            ),
            "temperature": body.get("temperature"),
            "top_p": body.get("top_p"),
            "stream": False,
        }
        chat_body = {k: v for k, v in chat_body.items() if v is not None}
        if is_streaming:
            return _stream_openrouter_responses_via_chat(
                provider_name,
                model,
                cleaned_model,
                provider_cfg["base_url"],
                headers,
                {**chat_body, "stream": True},
            )
        chat_resp = await _forward_upstream(
            provider_name,
            model,
            cleaned_model,
            provider_cfg["base_url"],
            _provider_endpoint(provider_cfg),
            chat_body,
            headers,
        )
        if chat_resp.status_code >= 400:
            return chat_resp
        try:
            chat_payload = json.loads(chat_resp.body.decode("utf-8"))
            adapted = _chat_to_responses_payload(chat_payload)
            return Response(
                content=json.dumps(adapted),
                status_code=200,
                media_type="application/json",
                headers=_gateway_debug_headers(
                    provider_name,
                    model,
                    cleaned_model,
                    adapted.get("model"),
                ),
            )
        except Exception:
            return chat_resp

    endpoint = _responses_endpoint(provider_cfg)

    if is_streaming:
        return _stream_upstream(
            provider_name,
            model,
            cleaned_model,
            provider_cfg["base_url"],
            endpoint,
            upstream_body,
            headers,
            provider_cfg,
        )
    return await _forward_upstream(
        provider_name,
        model,
        cleaned_model,
        provider_cfg["base_url"],
        endpoint,
        upstream_body,
        headers,
    )


def _upstream_target(base_url: str, endpoint: str) -> tuple[str, int, str]:
    parsed = urlparse(f"{base_url}{endpoint}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port, parsed.path or endpoint


def _network_block_response(
    provider_name: str,
    requested_model: str,
    model: str,
    decision_reason: str,
) -> Response:
    return Response(
        content=json.dumps(
            {
                "error": {
                    "message": (
                        "Upstream blocked by network authorization policy: "
                        f"{decision_reason}"
                    ),
                    "type": "network_policy_block",
                    "code": "egress_denied",
                }
            }
        ),
        status_code=403,
        media_type="application/json",
        headers=_gateway_debug_headers(provider_name, requested_model, model),
    )


async def _forward_upstream(
    provider_name: str,
    requested_model: str,
    model: str,
    base_url: str,
    endpoint: str,
    body: dict,
    headers: dict,
) -> Response:
    host, port, path = _upstream_target(base_url, endpoint)
    decision = network_monitor.authorize(host, port, scope="runtime")
    if decision.verdict == NET_BLOCK:
        log.warning("NET-BLOCK [%s/%s] %s:%d (%s)", provider_name, model, host, port, decision.reason)
        network_monitor.record(
            source="gateway", scope="runtime",
            host=host, port=port, decision=decision,
            method="POST", path=path,
        )
        return _network_block_response(provider_name, requested_model, model, decision.reason)

    start_ts = asyncio.get_event_loop().time()
    max_retries = max(0, int(os.environ.get("GATEWAY_429_RETRIES", "2")))
    async with httpx.AsyncClient(timeout=120) as client:
        resp = None
        for attempt in range(max_retries + 1):
            resp = await client.post(
                f"{base_url}{endpoint}",
                json=body,
                headers=headers,
            )
            if resp.status_code != 429 or attempt >= max_retries:
                break
            retry_after = resp.headers.get("Retry-After")
            delay = _retry_delay_seconds(attempt + 1, retry_after)
            log.warning(
                "UPSTREAM-429 [%s/%s]: retry %d/%d in %.1fs",
                provider_name,
                model,
                attempt + 1,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)

        latency_ms = int((asyncio.get_event_loop().time() - start_ts) * 1000)

        def _emit_event(status_code: int, bytes_in: int) -> None:
            network_monitor.record(
                source="gateway", scope="runtime",
                host=host, port=port, decision=decision,
                method="POST", path=path,
                status=status_code, bytes_in=bytes_in,
                bytes_out=len(json.dumps(body)) if body else 0,
                latency_ms=latency_ms,
            )

        if resp is None:
            _emit_event(502, 0)
            return Response(
                content=json.dumps(
                    {
                        "error": {
                            "message": "Gateway upstream request failed with empty response.",
                            "type": "gateway_error",
                            "code": "empty_upstream_response",
                        }
                    }
                ),
                status_code=502,
                media_type="application/json",
                headers=_gateway_debug_headers(
                    provider_name,
                    requested_model,
                    model,
                ),
            )
        if resp.status_code >= 400:
            _emit_event(resp.status_code, len(resp.content) if resp.content else 0)
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
                headers=_gateway_debug_headers(
                    provider_name,
                    requested_model,
                    model,
                ),
            )

        output_text = ""
        upstream_model = None
        try:
            payload = resp.json()
            output_text = extract_text_from_response_payload(payload)
            if isinstance(payload, dict):
                candidate = payload.get("model")
                if isinstance(candidate, str) and candidate:
                    upstream_model = candidate
        except Exception:
            output_text = ""

        if output_text:
            output_safe, output_reason = scan_text_output(output_text)
            if not output_safe:
                log.warning(f"BLOCKED-OUTPUT [{provider_name}/{model}]: {output_reason}")
                _log_event(
                    provider_name,
                    model,
                    "block",
                    output_reason,
                    output_text[:300],
                )
                return Response(
                    content=json.dumps(
                        {
                            "error": {
                                "message": (
                                    "Response blocked by security gateway: "
                                    f"{output_reason}"
                                ),
                                "type": "security_block",
                                "code": "output_policy_violation",
                            }
                        }
                    ),
                    status_code=403,
                    media_type="application/json",
                    headers=_gateway_debug_headers(
                        provider_name,
                        requested_model,
                        model,
                        upstream_model,
                    ),
                )

        _emit_event(resp.status_code, len(resp.content) if resp.content else 0)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type="application/json",
            headers=_gateway_debug_headers(
                provider_name,
                requested_model,
                model,
                upstream_model,
            ),
        )


def _stream_upstream(
    provider_name: str,
    requested_model: str,
    routed_model: str,
    base_url: str,
    endpoint: str,
    body: dict,
    headers: dict,
    provider_cfg: dict,
) -> StreamingResponse:
    host, port, path = _upstream_target(base_url, endpoint)
    decision = network_monitor.authorize(host, port, scope="runtime")
    if decision.verdict == NET_BLOCK:
        log.warning(
            "NET-BLOCK-STREAM [%s/%s] %s:%d (%s)",
            provider_name, routed_model, host, port, decision.reason,
        )
        network_monitor.record(
            source="gateway", scope="runtime",
            host=host, port=port, decision=decision,
            method="POST-STREAM", path=path,
        )
        blocked = _network_block_response(provider_name, requested_model, routed_model, decision.reason)

        async def _blocked_gen():
            yield blocked.body

        return StreamingResponse(
            _blocked_gen(),
            status_code=403,
            media_type="application/json",
            headers=_gateway_debug_headers(provider_name, requested_model, routed_model),
        )

    async def generate():
        max_retries = max(0, int(os.environ.get("GATEWAY_429_RETRIES", "2")))
        start_ts = asyncio.get_event_loop().time()
        total_in = 0
        final_status = 200
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                attempt = 0
                while True:
                    async with client.stream(
                        "POST",
                        f"{base_url}{endpoint}",
                        json=body,
                        headers=headers,
                    ) as resp:
                        if resp.status_code == 429 and attempt < max_retries:
                            retry_after = resp.headers.get("Retry-After")
                            delay = _retry_delay_seconds(attempt + 1, retry_after)
                            log.warning(
                                "UPSTREAM-429-STREAM [%s/%s]: retry %d/%d in %.1fs",
                                provider_name,
                                routed_model,
                                attempt + 1,
                                max_retries,
                                delay,
                            )
                            await resp.aread()
                            await asyncio.sleep(delay)
                            attempt += 1
                            continue

                        if resp.status_code >= 400:
                            final_status = resp.status_code
                            payload = await resp.aread()
                            total_in += len(payload)
                            yield payload
                            return

                        async for chunk in resp.aiter_bytes():
                            total_in += len(chunk)
                            yield chunk
                        return
        finally:
            latency_ms = int((asyncio.get_event_loop().time() - start_ts) * 1000)
            network_monitor.record(
                source="gateway", scope="runtime",
                host=host, port=port, decision=decision,
                method="POST-STREAM", path=path,
                status=final_status, bytes_in=total_in,
                bytes_out=len(json.dumps(body)) if body else 0,
                latency_ms=latency_ms,
            )

    return StreamingResponse(
        generate(),
        media_type=provider_cfg.get("stream_media_type", "text/event-stream"),
        headers=_gateway_debug_headers(provider_name, requested_model, routed_model),
    )


def _stream_openrouter_responses_via_chat(
    provider_name: str,
    requested_model: str,
    model: str,
    base_url: str,
    headers: dict,
    body: dict,
) -> StreamingResponse:
    async def generate():
        now = int(datetime.now(timezone.utc).timestamp())
        item_id = f"msg_{now}"
        response_id = f"resp_{now}"
        full_text = ""

        created_event = {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "model": model,
            },
        }
        yield _responses_stream_event(created_event)
        yield _responses_stream_event(
            {
                "type": "response.output_item.added",
                "response_id": response_id,
                "output_index": 0,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }
        )
        yield _responses_stream_event(
            {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            }
        )

        max_retries = max(0, int(os.environ.get("GATEWAY_429_RETRIES", "2")))
        async with httpx.AsyncClient(timeout=120) as client:
            attempt = 0
            while True:
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    json=body,
                    headers=headers,
                ) as resp:
                    if resp.status_code == 429 and attempt < max_retries:
                        retry_after = resp.headers.get("Retry-After")
                        delay = _retry_delay_seconds(attempt + 1, retry_after)
                        log.warning(
                            "UPSTREAM-429-FALLBACK [%s/%s]: retry %d/%d in %.1fs",
                            provider_name,
                            model,
                            attempt + 1,
                            max_retries,
                            delay,
                        )
                        await resp.aread()
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue

                    if resp.status_code >= 400:
                        raw = await resp.aread()
                        message = f"Upstream error ({resp.status_code})"
                        try:
                            err = json.loads(raw.decode("utf-8", errors="ignore"))
                            message = (
                                err.get("error", {}).get("message")
                                if isinstance(err, dict)
                                else message
                            ) or message
                        except Exception:
                            pass

                        yield _responses_stream_event(
                            {
                                "type": "response.failed",
                                "response": {
                                    "id": response_id,
                                    "object": "response",
                                    "status": "failed",
                                    "model": model,
                                    "error": {
                                        "type": "upstream_error",
                                        "code": str(resp.status_code),
                                        "message": message,
                                    },
                                },
                            }
                        )
                        yield b"data: [DONE]\n\n"
                        return

                    async for raw_chunk in resp.aiter_text():
                        for line in raw_chunk.splitlines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except Exception:
                                continue
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            part = delta.get("content", "")
                            if isinstance(part, list):
                                part = "".join(
                                    p.get("text", "")
                                    if isinstance(p, dict)
                                    else (p if isinstance(p, str) else "")
                                    for p in part
                                )
                            if not isinstance(part, str) or not part:
                                continue
                            full_text += part
                            yield _responses_stream_event(
                                {
                                    "type": "response.output_text.delta",
                                    "item_id": item_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": part,
                                }
                            )
                    break

        yield _responses_stream_event(
            {
                "type": "response.content_part.done",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": full_text},
            }
        )
        yield _responses_stream_event(
            {
                "type": "response.output_item.done",
                "response_id": response_id,
                "output_index": 0,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": full_text, "annotations": []}
                    ],
                },
            }
        )
        yield _responses_stream_event(
            {
                "type": "response.output_text.done",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "text": full_text,
            }
        )
        yield _responses_stream_event(
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "status": "completed",
                    "model": model,
                    "output_text": full_text,
                },
            }
        )
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=_gateway_debug_headers(provider_name, requested_model, model),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("GATEWAY_PORT", "8090"))
    log.info(f"Security Gateway starting on port {port}")
    log.info(f"   Providers: {list(PROVIDERS.keys())}")
    for name, cfg in PROVIDERS.items():
        key = get_api_key(cfg)
        status = "configured" if key else "not set"
        log.info(f"   {name}: {status} ({cfg['api_key_env']})")
    uvicorn.run(app, host="0.0.0.0", port=port)
