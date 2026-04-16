from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


STATE_DIRNAME = ".guard"
STATE_FILENAME = "mcp-bridges.json"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def resolve_bridge_state_path(workspace: str | Path) -> Path:
    root = Path(workspace).resolve()
    return root / STATE_DIRNAME / STATE_FILENAME


def _empty_state() -> dict:
    return {"version": 1, "sandboxes": {}}


def load_bridge_state(workspace: str | Path) -> dict:
    path = resolve_bridge_state_path(workspace)
    if not path.exists():
        return _empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return _empty_state()
    sandboxes = data.get("sandboxes")
    if not isinstance(sandboxes, dict):
        data["sandboxes"] = {}
    if not data.get("version"):
        data["version"] = 1
    return data


def save_bridge_state(workspace: str | Path, state: dict) -> Path:
    path = resolve_bridge_state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def list_bridges(workspace: str | Path, sandbox_name: str | None = None) -> list[dict]:
    state = load_bridge_state(workspace)
    sandboxes = state.get("sandboxes", {})
    results: list[dict] = []
    for current_sandbox, sandbox_entry in sandboxes.items():
        if sandbox_name and current_sandbox != sandbox_name:
            continue
        bridges = sandbox_entry.get("bridges", {})
        if not isinstance(bridges, dict):
            continue
        for name, bridge in bridges.items():
            if isinstance(bridge, dict):
                results.append({"sandbox": current_sandbox, "name": name, **bridge})
    results.sort(key=lambda item: (item["sandbox"], item["name"]))
    return results


def get_bridge(workspace: str | Path, sandbox_name: str, name: str) -> dict | None:
    state = load_bridge_state(workspace)
    sandboxes = state.get("sandboxes", {})
    sandbox = sandboxes.get(sandbox_name)
    if not isinstance(sandbox, dict):
        return None
    bridges = sandbox.get("bridges", {})
    bridge = bridges.get(name) if isinstance(bridges, dict) else None
    if not isinstance(bridge, dict):
        return None
    return {"sandbox": sandbox_name, "name": name, **bridge}


def upsert_bridge(workspace: str | Path, sandbox_name: str, name: str, bridge: dict) -> Path:
    state = load_bridge_state(workspace)
    sandboxes = state.setdefault("sandboxes", {})
    sandbox = sandboxes.setdefault(sandbox_name, {})
    bridges = sandbox.setdefault("bridges", {})
    existing = bridges.get(name, {}) if isinstance(bridges.get(name), dict) else {}
    merged = {
        **existing,
        **bridge,
        "updated_at": _utcnow(),
    }
    if "created_at" not in merged:
        merged["created_at"] = merged["updated_at"]
    bridges[name] = merged
    return save_bridge_state(workspace, state)


def remove_bridge(workspace: str | Path, sandbox_name: str, name: str) -> tuple[Path, bool]:
    state = load_bridge_state(workspace)
    sandboxes = state.setdefault("sandboxes", {})
    sandbox = sandboxes.get(sandbox_name)
    removed = False
    if isinstance(sandbox, dict):
        bridges = sandbox.get("bridges", {})
        if isinstance(bridges, dict) and name in bridges:
            del bridges[name]
            removed = True
        if not bridges:
            sandboxes.pop(sandbox_name, None)
    path = save_bridge_state(workspace, state)
    return path, removed


def mark_bridge_restarted(workspace: str | Path, sandbox_name: str, name: str) -> tuple[Path, bool]:
    state = load_bridge_state(workspace)
    sandboxes = state.get("sandboxes", {})
    sandbox = sandboxes.get(sandbox_name)
    if not isinstance(sandbox, dict):
        return save_bridge_state(workspace, state), False
    bridges = sandbox.get("bridges", {})
    bridge = bridges.get(name) if isinstance(bridges, dict) else None
    if not isinstance(bridge, dict):
        return save_bridge_state(workspace, state), False
    bridge["status"] = "planned"
    bridge["last_restart_at"] = _utcnow()
    bridge["updated_at"] = bridge["last_restart_at"]
    return save_bridge_state(workspace, state), True


def mark_bridge_activated(
    workspace: str | Path,
    sandbox_name: str,
    name: str,
    *,
    host_port: int | None = None,
    execution_model: str = "gateway-http-bridge",
) -> tuple[Path, bool]:
    state = load_bridge_state(workspace)
    sandboxes = state.get("sandboxes", {})
    sandbox = sandboxes.get(sandbox_name)
    if not isinstance(sandbox, dict):
        return save_bridge_state(workspace, state), False
    bridges = sandbox.get("bridges", {})
    bridge = bridges.get(name) if isinstance(bridges, dict) else None
    if not isinstance(bridge, dict):
        return save_bridge_state(workspace, state), False
    now = _utcnow()
    bridge["status"] = "active"
    bridge["execution_model"] = execution_model
    bridge["last_activated_at"] = now
    bridge["updated_at"] = now
    if host_port is not None:
        bridge["host_port"] = host_port
    notes = bridge.get("notes")
    if not isinstance(notes, list):
        notes = []
    runtime_note = "Active runtime uses Guard gateway /mcp/{server} reverse proxy."
    if runtime_note not in notes:
        notes.append(runtime_note)
    bridge["notes"] = notes
    return save_bridge_state(workspace, state), True


@dataclass(slots=True)
class BridgeSpec:
    name: str
    sandbox: str
    transport: str
    upstream_url: str
    credential_env: str | None
    allowed_hosts: list[str]
    purpose: str
    host_alias: str = "host.openshell.internal"
    host_port: int | None = None
    status: str = "planned"

    def to_record(self) -> dict:
        return {
            "name": self.name,
            "sandbox": self.sandbox,
            "transport": self.transport,
            "upstream_url": self.upstream_url,
            "credential_env": self.credential_env,
            "allowed_hosts": list(self.allowed_hosts),
            "purpose": self.purpose,
            "host_alias": self.host_alias,
            "host_port": self.host_port,
            "status": self.status,
            "execution_model": "compatibility-bridge",
            "notes": [
                "Inspired by NemoClaw PR #565 host-to-sandbox MCP bridge.",
                "This state record does not itself start the host proxy yet.",
            ],
        }
