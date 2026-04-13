import json
import os
import secrets
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class OnboardingArtifacts:
    workspace_path: Path
    sandbox_data_dir: Path
    immutable_openclaw_dir: Path
    policy_path: Path
    gateway_token: str
    gateway_url: str
    host_ip: str


def get_host_ip() -> str:
    """Resolve a host address that is usually reachable from Docker sandboxes."""
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            check=True,
        )
        first_ip = result.stdout.strip().split()[0]
        if first_ip:
            return first_ip
    except Exception:
        pass

    try:
        return socket.gethostbyname("host.docker.internal")
    except Exception:
        return "host.docker.internal"


def _load_runtime_network_allow(workspace_path: Path) -> list[dict]:
    """Read `network.runtime.allow` from gateway.yaml so onboarding can mirror
    the policy into the OpenShell-style network_policies. Returns [] if missing."""
    gw_path = workspace_path / "gateway.yaml"
    if not gw_path.exists():
        return []
    try:
        data = yaml.safe_load(gw_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    runtime = (data.get("network") or {}).get("runtime") or {}
    allow = runtime.get("allow")
    return allow if isinstance(allow, list) else []


def _project_network_policies(allow_entries: list[dict]) -> dict:
    """Convert blueprint.network.runtime.allow into the OpenShell
    network_policies dict consumed by NemoClaw / Landlock."""
    policies: dict[str, dict] = {}
    for entry in allow_entries:
        if not isinstance(entry, dict):
            continue
        host = entry.get("host")
        if not isinstance(host, str) or not host:
            continue
        ports_raw = entry.get("ports") or [443]
        ports = [int(p) for p in ports_raw if isinstance(p, (int, str)) and str(p).isdigit()]
        if not ports:
            ports = [443]
        enforcement = entry.get("enforcement") or "enforce"
        key = host.replace(".", "_").replace("*", "any")
        policies[key] = {
            "name": entry.get("purpose") or f"Allow {host}",
            "endpoints": [
                {
                    "host": host,
                    "port": port,
                    "protocol": "rest",
                    "enforcement": "enforce",
                    "rules": [
                        {"allow": {"method": "GET", "path": "/**"}},
                        {"allow": {"method": "POST", "path": "/**"}},
                        {"allow": {"method": "PUT", "path": "/**"}},
                        {"allow": {"method": "DELETE", "path": "/**"}},
                    ],
                }
                for port in ports
            ],
            "binaries": [
                {"path": "/usr/local/bin/openclaw"},
                {"path": "/usr/local/bin/node"},
            ],
        }
    return policies


def prepare_onboarding(
    workspace: str | Path,
    sandbox_name: str,
    gateway_port: int,
) -> OnboardingArtifacts:
    workspace_path = Path(workspace).expanduser().resolve()
    sandbox_data_dir = workspace_path / "sandbox_workspace"
    sandbox_data_dir.mkdir(parents=True, exist_ok=True)

    immutable_openclaw_dir = sandbox_data_dir / "openclaw"
    immutable_openclaw_dir.mkdir(parents=True, exist_ok=True)
    stateful_openclaw_dir = sandbox_data_dir / "openclaw-data"
    stateful_openclaw_dir.mkdir(parents=True, exist_ok=True)

    agent_dir = immutable_openclaw_dir / "agents" / "main" / "agent"
    sessions_dir = immutable_openclaw_dir / "agents" / "main" / "sessions"
    agent_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # The sandbox symlinks /sandbox/.openclaw/agents -> /sandbox/.openclaw-data/agents.
    # Write auth artifacts to the rw data dir as well so they are visible at runtime.
    data_agent_dir = stateful_openclaw_dir / "agents" / "main" / "agent"
    data_sessions_dir = stateful_openclaw_dir / "agents" / "main" / "sessions"
    data_agent_dir.mkdir(parents=True, exist_ok=True)
    data_sessions_dir.mkdir(parents=True, exist_ok=True)

    host_ip = get_host_ip()
    gateway_url = f"http://{host_ip}:{gateway_port}/v1"
    gateway_token = secrets.token_hex(24)

    policy_dir = workspace_path / "policies"
    policy_dir.mkdir(parents=True, exist_ok=True)
    policy_path = policy_dir / f"{sandbox_name}.yaml"
    blueprint_policy_dir = workspace_path / "nemoclaw-blueprint" / "policies"
    blueprint_policy_dir.mkdir(parents=True, exist_ok=True)
    blueprint_policy_path = blueprint_policy_dir / f"{sandbox_name}.yaml"

    extra_policies = _project_network_policies(
        _load_runtime_network_allow(workspace_path)
    )
    _write_policy(policy_path, extra_policies)
    _write_policy(blueprint_policy_path, extra_policies)
    mcp_servers = _build_mcp_servers_config(workspace_path, gateway_port=gateway_port)
    _write_openclaw_config(
        immutable_openclaw_dir / "openclaw.json",
        gateway_token,
        mcp_servers=mcp_servers,
    )
    _write_auth_profiles(agent_dir / "auth-profiles.json")
    _write_sessions_file(sessions_dir / "sessions.json")

    # Mirror into the rw data dir (symlink target inside the sandbox)
    _write_auth_profiles(data_agent_dir / "auth-profiles.json")
    _write_sessions_file(data_sessions_dir / "sessions.json")

    return OnboardingArtifacts(
        workspace_path=workspace_path,
        sandbox_data_dir=sandbox_data_dir,
        immutable_openclaw_dir=immutable_openclaw_dir,
        policy_path=policy_path,
        gateway_token=gateway_token,
        gateway_url=gateway_url,
        host_ip=host_ip,
    )


def _write_policy(policy_path: Path, extra_network_policies: dict | None = None) -> None:
    policy_doc = {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_only": [
                "/usr",
                "/lib",
                "/proc",
                "/dev/urandom",
                "/app",
                "/etc",
                "/var/log",
                "/sandbox/.openclaw",
            ],
            "read_write": [
                "/sandbox",
                "/tmp",
                "/dev/null",
                "/sandbox/.openclaw-data",
            ],
        },
        "landlock": {
            "compatibility": "best_effort",
        },
        "process": {
            "run_as_user": "sandbox",
            "run_as_group": "sandbox",
        },
        "network_policies": {
            "inference_local": {
                "name": "OpenShell inference.local proxy",
                "endpoints": [
                    {
                        "host": "inference.local",
                        "port": 443,
                        "protocol": "rest",
                        "enforcement": "enforce",
                        "rules": [
                            {"allow": {"method": "GET", "path": "/**"}},
                            {"allow": {"method": "POST", "path": "/**"}},
                        ],
                    },
                ],
                "binaries": [
                    {"path": "/usr/local/bin/openclaw"},
                    {"path": "/usr/local/bin/node"},
                ],
            },
            "openclaw_api": {
                "name": "OpenClaw API",
                "endpoints": [
                    {
                        "host": "openclaw.ai",
                        "port": 443,
                        "protocol": "rest",
                        "enforcement": "enforce",
                        "rules": [
                            {"allow": {"method": "GET", "path": "/**"}},
                            {"allow": {"method": "POST", "path": "/**"}},
                        ],
                    },
                ],
                "binaries": [
                    {"path": "/usr/local/bin/openclaw"},
                    {"path": "/usr/local/bin/node"},
                ],
            },
            "openclaw_docs": {
                "name": "OpenClaw docs",
                "endpoints": [
                    {
                        "host": "docs.openclaw.ai",
                        "port": 443,
                        "protocol": "rest",
                        "enforcement": "enforce",
                        "rules": [
                            {"allow": {"method": "GET", "path": "/**"}},
                        ],
                    },
                ],
                "binaries": [
                    {"path": "/usr/local/bin/openclaw"},
                ],
            },
        },
    }
    if extra_network_policies:
        # Merge blueprint-driven entries; do not overwrite the built-in trio above.
        for key, value in extra_network_policies.items():
            policy_doc["network_policies"].setdefault(key, value)
    with policy_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(policy_doc, handle, sort_keys=False)


def _build_mcp_servers_config(
    workspace_path: Path,
    gateway_port: int = 8090,
) -> dict:
    """Read approved MCP servers from gateway.yaml, resolve tokens from env,
    and return a dict suitable for ``openclaw.json``'s ``mcp.servers`` field.

    Each entry is an OpenClaw 2026.4+ compatible MCP server definition.
    The sandbox connects directly to the upstream MCP endpoint; the network
    policy must allowlist each host (done automatically by ``_write_policy``
    via the ``runtime.allow`` entries in ``gateway.yaml``).
    """
    gw_path = workspace_path / "gateway.yaml"
    if not gw_path.exists():
        return {}
    try:
        data = yaml.safe_load(gw_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

    raw_servers = (data.get("mcp") or {}).get("servers") or []
    if not isinstance(raw_servers, list):
        return {}

    mcp_servers: dict = {}
    for srv in raw_servers:
        if not isinstance(srv, dict):
            continue
        if srv.get("status") != "approved":
            continue
        name = srv.get("name")
        url = srv.get("url")
        if not name or not url:
            continue

        transport = srv.get("transport", "streamable_http")
        # Normalize: gateway.yaml uses underscores, OpenClaw uses hyphens
        oc_transport = transport.replace("_", "-")

        entry: dict = {"type": "http", "transport": oc_transport, "url": url}

        cred_env = srv.get("credential_env")
        if cred_env:
            token = os.environ.get(cred_env, "")
            if token:
                entry["headers"] = {"Authorization": f"Bearer {token}"}

        mcp_servers[name] = entry

    return mcp_servers


def _write_openclaw_config(
    output_path: Path,
    gateway_token: str,
    *,
    mcp_servers: dict | None = None,
) -> None:
    openclaw_json = {
        "commands": {
            "native": "auto",
            "nativeSkills": "auto",
            "restart": True,
            "ownerDisplay": "raw",
        },
        "gateway": {
            "auth": {"mode": "token", "token": gateway_token},
            "mode": "local",
        },
        "models": {
            "providers": {
                "anthropic": {
                    "baseUrl": "https://inference.local/v1",
                    "apiKey": "guard-managed",
                    "request": {"allowPrivateNetwork": True},
                    "models": [
                        {"id": "claude-opus-4-6", "name": "claude-opus-4-6"},
                        {"id": "claude-3.5-sonnet", "name": "claude-3.5-sonnet"},
                        {
                            "id": "claude-3-5-sonnet-20241022",
                            "name": "claude-3-5-sonnet-20241022",
                        },
                    ],
                },
                "openai": {
                    "baseUrl": "https://inference.local/v1",
                    "apiKey": "guard-managed",
                    "request": {"allowPrivateNetwork": True},
                    "models": [
                        {"id": "gpt-4o", "name": "gpt-4o"},
                        {"id": "o1", "name": "o1"},
                        {"id": "o3-mini", "name": "o3-mini"},
                    ],
                },
                "openrouter": {
                    "baseUrl": "https://inference.local/v1",
                    "apiKey": "guard-managed",
                    "request": {"allowPrivateNetwork": True},
                    "models": [
                        {"id": "openrouter/auto", "name": "openrouter/auto"},
                        {
                            "id": "google/gemini-2.5-pro-preview",
                            "name": "google/gemini-2.5-pro-preview",
                        },
                        {
                            "id": "deepseek/deepseek-chat-v3",
                            "name": "deepseek/deepseek-chat-v3",
                        },
                        {
                            "id": "anthropic/claude-opus-4-6",
                            "name": "anthropic/claude-opus-4-6",
                        },
                        {"id": "openai/gpt-4o", "name": "openai/gpt-4o"},
                    ],
                },
            }
        },
    }
    if mcp_servers:
        openclaw_json["mcp"] = {"servers": mcp_servers}
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(openclaw_json, handle, indent=2)


def _write_auth_profiles(output_path: Path) -> None:
    auth_profiles = {
        "version": 1,
        "profiles": {
            "anthropic:default": {
                "type": "api_key",
                "provider": "anthropic",
                "key": "guard-managed",
            },
            "openai:default": {
                "type": "api_key",
                "provider": "openai",
                "key": "guard-managed",
            },
            "openrouter:default": {
                "type": "api_key",
                "provider": "openrouter",
                "key": "guard-managed",
            },
        },
    }
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(auth_profiles, handle, indent=2)


def _write_sessions_file(output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump({"version": 1, "sessions": []}, handle, indent=2)
