"""
gateway_config — read/write helpers for the guard-owned ``gateway.yaml``.

This file is the single source of truth for everything that the guard gateway
controls but NemoClaw does not consume:

* ``network.install`` and ``network.runtime`` allowlists (previously stuffed
  into ``nemoclaw-blueprint/blueprint.yaml`` under the same key — moved out
  by ``tools/migrate_blueprint_to_gateway.py``).
* ``mcp.servers`` registry: each MCP server tracked by the gateway with a
  status (pending / approved / denied / revoked), upstream URL, transport,
  and optional credential env var.

Conventions
-----------
* All public functions take a ``cfg_path: Path`` and operate on disk in place.
* Reads/writes preserve key order via ``sort_keys=False``.
* Mutators raise ``GatewayConfigError`` on structural problems and return a
  bool/dataclass otherwise. They never print — callers decide how to surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

VALID_SCOPES = ("install", "runtime")
VALID_DEFAULTS = ("deny", "warn", "monitor", "allow")
VALID_ENFORCEMENTS = ("enforce", "warn", "monitor")

VALID_MCP_STATUSES = ("pending", "approved", "denied", "revoked")
VALID_MCP_TRANSPORTS = ("sse", "streamable_http")


class GatewayConfigError(Exception):
    """Raised on missing/malformed gateway.yaml or invalid arguments."""


# ── core read/write ─────────────────────────────────────────────────────────
def load(cfg_path: Path) -> dict:
    """Return the parsed gateway.yaml. An absent file yields an empty dict so
    callers can `setdefault` their way to a fresh config."""
    if not cfg_path.exists():
        return {}
    try:
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GatewayConfigError(f"gateway.yaml parse error: {exc}") from exc


def save(cfg_path: Path, data: dict) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _ensure_versioned(data: dict) -> dict:
    data.setdefault("version", 1)
    return data


# ── network: dataclasses ────────────────────────────────────────────────────
@dataclass
class NetEntry:
    host: str
    ports: list[int]
    enforcement: str | None
    purpose: str
    rpm: int | None

    @classmethod
    def from_dict(cls, raw: dict) -> "NetEntry":
        ports_raw = raw.get("ports") or raw.get("port") or []
        if isinstance(ports_raw, int):
            ports_raw = [ports_raw]
        rate = raw.get("rate_limit") or {}
        rpm = rate.get("rpm") if isinstance(rate, dict) else None
        return cls(
            host=str(raw.get("host", "")),
            ports=[int(p) for p in ports_raw],
            enforcement=raw.get("enforcement"),
            purpose=str(raw.get("purpose", "")),
            rpm=int(rpm) if rpm is not None else None,
        )


def _check_scope(scope: str) -> None:
    if scope not in VALID_SCOPES:
        raise GatewayConfigError(
            f"invalid scope {scope!r}, expected one of {VALID_SCOPES}"
        )


# ── network: defaults ───────────────────────────────────────────────────────
def get_default(cfg_path: Path, scope: str) -> str:
    _check_scope(scope)
    data = load(cfg_path)
    section = (data.get("network") or {}).get(scope) or {}
    return str(section.get("default", "deny" if scope == "install" else "warn"))


def set_default(cfg_path: Path, scope: str, value: str) -> None:
    _check_scope(scope)
    if value not in VALID_DEFAULTS:
        raise GatewayConfigError(
            f"invalid default {value!r}, expected one of {VALID_DEFAULTS}"
        )
    data = _ensure_versioned(load(cfg_path))
    net = data.setdefault("network", {})
    section = net.setdefault(scope, {})
    section["default"] = value
    save(cfg_path, data)


def set_defaults(cfg_path: Path, install_default: str, runtime_default: str) -> None:
    """Convenience used by the setup wizard."""
    set_default(cfg_path, "install", install_default)
    set_default(cfg_path, "runtime", runtime_default)


# ── network: entries ────────────────────────────────────────────────────────
def list_entries(cfg_path: Path, scope: str) -> list[NetEntry]:
    _check_scope(scope)
    data = load(cfg_path)
    raw = (((data.get("network") or {}).get(scope) or {}).get("allow")) or []
    if not isinstance(raw, list):
        return []
    return [NetEntry.from_dict(item) for item in raw if isinstance(item, dict)]


def add_entry(
    cfg_path: Path,
    scope: str,
    host: str,
    ports: list[int] | None = None,
    enforcement: str | None = None,
    purpose: str = "",
    rpm: int | None = None,
) -> bool:
    """Add a host entry. Returns True if added, False if (host,scope) already
    exists (caller can decide whether to update instead)."""
    _check_scope(scope)
    if not host:
        raise GatewayConfigError("host is required")
    if enforcement is not None and enforcement not in VALID_ENFORCEMENTS:
        raise GatewayConfigError(
            f"invalid enforcement {enforcement!r}, expected one of {VALID_ENFORCEMENTS}"
        )

    data = _ensure_versioned(load(cfg_path))
    net = data.setdefault("network", {})
    section = net.setdefault(scope, {})
    allow = section.setdefault("allow", [])
    if not isinstance(allow, list):
        raise GatewayConfigError(f"network.{scope}.allow is not a list")

    for item in allow:
        if isinstance(item, dict) and item.get("host") == host:
            return False  # already present

    entry: dict[str, Any] = {"host": host}
    if ports:
        entry["ports"] = list(ports)
    if enforcement:
        entry["enforcement"] = enforcement
    if purpose:
        entry["purpose"] = purpose
    if rpm is not None:
        entry["rate_limit"] = {"rpm": int(rpm)}
    allow.append(entry)
    save(cfg_path, data)
    return True


def remove_entry(cfg_path: Path, scope: str, host: str) -> bool:
    """Remove an entry by host. Returns True if removed, False if not found."""
    _check_scope(scope)
    data = load(cfg_path)
    section = (data.get("network") or {}).get(scope) or {}
    allow = section.get("allow")
    if not isinstance(allow, list):
        return False
    new_allow = [
        item for item in allow
        if not (isinstance(item, dict) and item.get("host") == host)
    ]
    if len(new_allow) == len(allow):
        return False
    section["allow"] = new_allow
    save(cfg_path, data)
    return True


# ── mcp: dataclass ──────────────────────────────────────────────────────────
@dataclass
class McpServer:
    name: str
    url: str
    transport: str = "sse"
    credential_env: str | None = None
    status: str = "pending"
    purpose: str = ""
    registered_at: str = ""
    approved_at: str | None = None
    approved_by: str | None = None
    denied_reason: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "name": self.name,
            "url": self.url,
            "transport": self.transport,
            "status": self.status,
            "registered_at": self.registered_at,
        }
        if self.credential_env:
            out["credential_env"] = self.credential_env
        if self.purpose:
            out["purpose"] = self.purpose
        if self.approved_at is not None:
            out["approved_at"] = self.approved_at
        if self.approved_by is not None:
            out["approved_by"] = self.approved_by
        if self.denied_reason is not None:
            out["denied_reason"] = self.denied_reason
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "McpServer":
        return cls(
            name=str(raw.get("name", "")),
            url=str(raw.get("url", "")),
            transport=str(raw.get("transport", "sse")),
            credential_env=raw.get("credential_env"),
            status=str(raw.get("status", "pending")),
            purpose=str(raw.get("purpose", "")),
            registered_at=str(raw.get("registered_at", "")),
            approved_at=raw.get("approved_at"),
            approved_by=raw.get("approved_by"),
            denied_reason=raw.get("denied_reason"),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_status(status: str) -> None:
    if status not in VALID_MCP_STATUSES:
        raise GatewayConfigError(
            f"invalid mcp status {status!r}, expected one of {VALID_MCP_STATUSES}"
        )


def _check_transport(transport: str) -> None:
    if transport not in VALID_MCP_TRANSPORTS:
        raise GatewayConfigError(
            f"invalid mcp transport {transport!r}, expected one of {VALID_MCP_TRANSPORTS}"
        )


def _validate_name(name: str) -> None:
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise GatewayConfigError(
            f"invalid mcp server name {name!r}: must be alphanumeric (with - or _ allowed)"
        )


# ── mcp: registry I/O ───────────────────────────────────────────────────────
def list_servers(cfg_path: Path) -> list[McpServer]:
    data = load(cfg_path)
    raw = ((data.get("mcp") or {}).get("servers")) or []
    if not isinstance(raw, list):
        return []
    return [McpServer.from_dict(item) for item in raw if isinstance(item, dict)]


def find_server(cfg_path: Path, name: str) -> McpServer | None:
    for srv in list_servers(cfg_path):
        if srv.name == name:
            return srv
    return None


def register_server(
    cfg_path: Path,
    name: str,
    url: str,
    transport: str = "sse",
    credential_env: str | None = None,
    purpose: str = "",
) -> McpServer:
    """Add a new MCP server in pending state. Raises GatewayConfigError if a
    server with the same name already exists."""
    _validate_name(name)
    if not url:
        raise GatewayConfigError("url is required")
    _check_transport(transport)

    data = _ensure_versioned(load(cfg_path))
    mcp = data.setdefault("mcp", {})
    servers = mcp.setdefault("servers", [])
    if not isinstance(servers, list):
        raise GatewayConfigError("mcp.servers is not a list")
    for item in servers:
        if isinstance(item, dict) and item.get("name") == name:
            raise GatewayConfigError(f"mcp server {name!r} already exists")

    server = McpServer(
        name=name,
        url=url,
        transport=transport,
        credential_env=credential_env or None,
        purpose=purpose,
        status="pending",
        registered_at=_now_iso(),
    )
    servers.append(server.to_dict())
    save(cfg_path, data)
    return server


def set_server_status(
    cfg_path: Path,
    name: str,
    status: str,
    actor: str,
    reason: str = "",
) -> McpServer:
    """Flip a server between pending/approved/denied/revoked. Stamps approval
    metadata when transitioning to ``approved``."""
    _check_status(status)
    if not actor:
        raise GatewayConfigError("actor is required")

    data = load(cfg_path)
    mcp = data.get("mcp") or {}
    servers = mcp.get("servers")
    if not isinstance(servers, list):
        raise GatewayConfigError(f"mcp server {name!r} not found")

    target_idx: int | None = None
    for idx, item in enumerate(servers):
        if isinstance(item, dict) and item.get("name") == name:
            target_idx = idx
            break
    if target_idx is None:
        raise GatewayConfigError(f"mcp server {name!r} not found")

    raw = servers[target_idx]
    raw["status"] = status
    if status == "approved":
        raw["approved_at"] = _now_iso()
        raw["approved_by"] = actor
        raw.pop("denied_reason", None)
    elif status == "denied":
        raw["denied_reason"] = reason or f"denied by {actor}"
    elif status == "revoked":
        raw["denied_reason"] = reason or f"revoked by {actor}"

    save(cfg_path, data)
    return McpServer.from_dict(raw)


def remove_server(cfg_path: Path, name: str) -> bool:
    data = load(cfg_path)
    mcp = data.get("mcp") or {}
    servers = mcp.get("servers")
    if not isinstance(servers, list):
        return False
    new_servers = [
        item for item in servers
        if not (isinstance(item, dict) and item.get("name") == name)
    ]
    if len(new_servers) == len(servers):
        return False
    mcp["servers"] = new_servers
    save(cfg_path, data)
    return True


__all__ = [
    "GatewayConfigError",
    "NetEntry",
    "McpServer",
    "VALID_SCOPES",
    "VALID_DEFAULTS",
    "VALID_ENFORCEMENTS",
    "VALID_MCP_STATUSES",
    "VALID_MCP_TRANSPORTS",
    "load",
    "save",
    "get_default",
    "set_default",
    "set_defaults",
    "list_entries",
    "add_entry",
    "remove_entry",
    "list_servers",
    "find_server",
    "register_server",
    "set_server_status",
    "remove_server",
]
