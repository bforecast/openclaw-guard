import json
import os
import secrets
import socket
import subprocess
import ipaddress
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
    sandbox_host: str


def get_sandbox_host() -> str:
    """Return the hostname that K3s sandbox pods use to reach the EC2 host.

    NemoClaw registers 'host.openshell.internal' in K3s cluster DNS; it resolves
    to the Docker bridge gateway regardless of which server we run on.
    We use this instead of 'inference.local', which OpenClaw 2026.4+ blocks
    via its SSRF guard.
    """
    return "host.openshell.internal"


def _is_always_blocked_ip(value: ipaddress._BaseAddress) -> bool:
    return value.is_loopback or value.is_link_local or value.is_unspecified


def _parse_allowed_ip_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    values: list[str] = []
    for part in raw.replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            parsed = ipaddress.ip_address(item)
        except ValueError:
            continue
        if _is_always_blocked_ip(parsed):
            continue
        values.append(str(parsed))
    return list(dict.fromkeys(values))


def _resolve_private_allowed_ips(host: str) -> list[str]:
    try:
        parsed_host = ipaddress.ip_address(host)
    except ValueError:
        parsed_host = None
    if parsed_host is not None:
        if parsed_host.is_private and not _is_always_blocked_ip(parsed_host):
            return [str(parsed_host)]
        return []

    resolved: list[str] = []
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_ip = sockaddr[0]
        try:
            parsed = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if not parsed.is_private or _is_always_blocked_ip(parsed):
            continue
        resolved.append(str(parsed))
    return list(dict.fromkeys(resolved))


def _guard_bridge_allowed_ips(host: str) -> list[str]:
    env_override = _parse_allowed_ip_values(os.environ.get("GUARD_BRIDGE_ALLOWED_IPS"))
    if env_override:
        return env_override
    return _resolve_private_allowed_ips(host)


def _network_endpoint(
    host: str,
    port: int,
    *,
    methods: list[str],
    allowed_ips: list[str] | None = None,
) -> dict:
    endpoint = {
        "host": host,
        "port": port,
        "protocol": "rest",
        "enforcement": "enforce",
        "rules": [
            {"allow": {"method": method, "path": "/**"}}
            for method in methods
        ],
    }
    if allowed_ips:
        endpoint["allowed_ips"] = list(allowed_ips)
    return endpoint


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
        allowed_ips = _resolve_private_allowed_ips(host)
        key = host.replace(".", "_").replace("*", "any")
        policies[key] = {
            "name": entry.get("purpose") or f"Allow {host}",
            "endpoints": [
                _network_endpoint(
                    host,
                    port,
                    methods=["GET", "POST", "PUT", "DELETE"],
                    allowed_ips=allowed_ips,
                )
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

    sandbox_host = get_sandbox_host()
    gateway_url = f"http://{sandbox_host}:{gateway_port}/v1"
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
    _write_policy(
        policy_path,
        extra_policies,
        bridge_host=sandbox_host,
        gateway_port=gateway_port,
    )
    _write_policy(
        blueprint_policy_path,
        extra_policies,
        bridge_host=sandbox_host,
        gateway_port=gateway_port,
    )
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
        sandbox_host=sandbox_host,
    )


def _write_policy(
    policy_path: Path,
    extra_network_policies: dict | None = None,
    *,
    bridge_host: str | None = None,
    gateway_port: int = 8090,
) -> None:
    bridge_host = bridge_host or get_sandbox_host()
    bridge_allowed_ips = _guard_bridge_allowed_ips(bridge_host)
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
            "guard_bridge_host": {
                "name": "Guard host bridge",
                "endpoints": [
                    _network_endpoint(
                        bridge_host,
                        gateway_port,
                        methods=["GET", "POST", "DELETE"],
                        allowed_ips=bridge_allowed_ips,
                    ),
                ],
                "binaries": [
                    {"path": "/usr/local/bin/openclaw"},
                    {"path": "/usr/local/bin/node"},
                    {"path": "/usr/bin/python3"},
                    {"path": "/usr/bin/curl"},
                ],
            },
            "inference_local": {
                "name": "OpenShell inference.local proxy",
                "endpoints": [
                    _network_endpoint("inference.local", 443, methods=["GET", "POST"]),
                ],
                "binaries": [
                    {"path": "/usr/local/bin/openclaw"},
                    {"path": "/usr/local/bin/node"},
                ],
            },
            "openclaw_api": {
                "name": "OpenClaw API",
                "endpoints": [
                    _network_endpoint("openclaw.ai", 443, methods=["GET", "POST"]),
                ],
                "binaries": [
                    {"path": "/usr/local/bin/openclaw"},
                    {"path": "/usr/local/bin/node"},
                ],
            },
            "openclaw_docs": {
                "name": "OpenClaw docs",
                "endpoints": [
                    _network_endpoint("docs.openclaw.ai", 443, methods=["GET"]),
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
    """Read approved MCP servers from gateway.yaml and return a dict suitable
    for ``openclaw.json``'s ``mcp.servers`` field.

    Each entry is an OpenClaw 2026.4+ compatible MCP server definition.
    The sandbox connects directly to the upstream MCP endpoint; the network
    policy must allowlist each host (done automatically by ``_write_policy``
    via the ``runtime.allow`` entries in ``gateway.yaml``).

    Credential-bearing servers are rendered with OpenShell placeholder refs
    instead of resolved tokens. This keeps real secrets out of the generated
    host-side config artifact and matches the supported "attach provider, then
    rebuild sandbox" workflow.
    """
    del gateway_port  # compatibility placeholder during migration

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
            entry["headers"] = {
                "Authorization": f"Bearer openshell:resolve:env:{cred_env}"
            }

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
