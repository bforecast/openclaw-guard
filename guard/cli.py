import json
import os
import socket
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import typer

from guard import gateway_config, sandbox_policy
from guard import bridge_state
from guard.onboard import prepare_onboarding

app = typer.Typer(help="OpenClaw Guard - Unbypassable Security Gateway CLI")
net_app = typer.Typer(help="Manage gateway network whitelist (install/runtime).")
app.add_typer(net_app, name="net")
mcp_app = typer.Typer(help="Manage MCP servers via the guard gateway HTTP API.")
app.add_typer(mcp_app, name="mcp")
bridge_app = typer.Typer(help="Manage the self-hosted MCP bridge compatibility layer.")
app.add_typer(bridge_app, name="bridge")

MCP_INSTALL_TEMPLATES = {
    "github": {
        "url": "https://api.githubcopilot.com/mcp/",
        "transport": "streamable_http",
        "credential_env_hint": "GITHUB_MCP_TOKEN",
        "purpose": "GitHub MCP (repos, issues, PRs, code search)",
        "allowlist_hosts": ["api.githubcopilot.com", "api.github.com"],
    },
    "slack": {
        "url": "https://slack.mcp.run/sse",
        "transport": "sse",
        "credential_env_hint": "SLACK_MCP_TOKEN",
        "purpose": "Slack MCP (channels, messages, users)",
        "allowlist_hosts": ["slack.mcp.run"],
    },
    "linear": {
        "url": "https://mcp.linear.app/sse",
        "transport": "sse",
        "credential_env_hint": "LINEAR_MCP_TOKEN",
        "purpose": "Linear MCP (issues, projects, teams)",
        "allowlist_hosts": ["mcp.linear.app"],
    },
    "brave-search": {
        "url": "https://mcp.bravesearch.com/sse",
        "transport": "sse",
        "credential_env_hint": "BRAVE_API_KEY",
        "purpose": "Brave Search MCP (web search, local search)",
        "allowlist_hosts": ["mcp.bravesearch.com"],
    },
    "sentry": {
        "url": "https://mcp.sentry.dev/sse",
        "transport": "sse",
        "credential_env_hint": "SENTRY_AUTH_TOKEN",
        "purpose": "Sentry MCP (error tracking, issues, releases)",
        "allowlist_hosts": ["mcp.sentry.dev"],
    },
}


def _gateway_config_path() -> Path:
    """Resolve gateway.yaml relative to this file's project root."""
    return Path(__file__).resolve().parent.parent / "gateway.yaml"


def _gateway_reload(gateway_url: str, token: str | None) -> None:
    """POST /v1/network/policy/reload to live-reload the gateway."""
    import httpx  # local import to keep base CLI startup light

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.post(
            f"{gateway_url.rstrip('/')}/v1/network/policy/reload",
            headers=headers,
            timeout=5.0,
        )
        if resp.status_code == 200:
            typer.secho(f"   OK gateway reloaded ({gateway_url})", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"   WARN gateway reload returned {resp.status_code}: {resp.text[:200]}",
                fg=typer.colors.YELLOW,
            )
    except Exception as exc:
        typer.secho(
            f"   WARN gateway not reachable at {gateway_url} ({exc}); "
            "edit took effect on disk only",
            fg=typer.colors.YELLOW,
        )


def _gateway_admin_request(
    method: str,
    path: str,
    *,
    gateway_url: str = "http://127.0.0.1:8090",
    json: dict | None = None,
    params: dict | None = None,
) -> dict | list:
    """Call an admin endpoint on the gateway. Fails loudly (typer.Exit) if the
    gateway is unreachable or returns a non-2xx status. Used by `guard mcp ...`
    commands which are pure HTTP wrappers — they NEVER touch gateway.yaml on
    disk; the gateway is the source of truth."""
    import httpx

    token = os.environ.get("GUARD_ADMIN_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if not token:
        typer.secho(
            "ERROR: GUARD_ADMIN_TOKEN (or OPENCLAW_GATEWAY_TOKEN) is not set",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    url = f"{gateway_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.request(
            method, url, headers=headers, json=json, params=params, timeout=10.0,
        )
    except Exception as exc:
        typer.secho(f"ERROR: gateway not reachable at {gateway_url}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(2) from exc

    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("error") or body.get("detail") or body
        except Exception:
            detail = resp.text[:300]
        typer.secho(f"ERROR: {method} {path} -> {resp.status_code}: {detail}", fg=typer.colors.RED)
        raise typer.Exit(1)

    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def _build_bridge_spec(server: dict, sandbox_name: str) -> bridge_state.BridgeSpec:
    url = str(server.get("url") or "")
    transport = _openclaw_transport_name(server.get("transport"))
    purpose = str(server.get("purpose") or f"MCP bridge for {server.get('name', 'unknown')}")
    credential_env = server.get("credential_env")
    parsed = urlparse(url) if url else None
    allowed_hosts = [parsed.hostname] if parsed and parsed.hostname else []
    return bridge_state.BridgeSpec(
        name=str(server.get("name") or ""),
        sandbox=sandbox_name,
        transport=transport,
        upstream_url=url,
        credential_env=str(credential_env) if credential_env else None,
        allowed_hosts=allowed_hosts,
        purpose=purpose,
    )


def _print_bridge_row(row: dict) -> None:
    typer.echo(
        f"  {row.get('sandbox', '-'):<16} "
        f"{row.get('name', '-'):<18} "
        f"{row.get('status', '-'):<10} "
        f"{row.get('transport', '-'):<18} "
        f"{row.get('host_alias', '-')}"
        f"{':' + str(row['host_port']) if row.get('host_port') else ''}"
    )


def _default_bridge_host() -> str:
    return os.environ.get("GUARD_BRIDGE_HOST") or "host.openshell.internal"


def _default_bridge_port(default: int = 8090) -> int:
    raw = os.environ.get("GUARD_BRIDGE_PORT")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _bridge_port(row: dict, gateway_port: int) -> int:
    host_port = row.get("host_port")
    if isinstance(host_port, int) and host_port > 0:
        return host_port
    return _default_bridge_port(gateway_port)


def _bridge_url(row: dict, gateway_port: int) -> str:
    host_alias = row.get("host_alias") or _default_bridge_host()
    port = _bridge_port(row, gateway_port)
    name = row.get("name") or "mcp"
    return f"http://{host_alias}:{port}/mcp/{name}/"


def _render_openclaw_mcp_command(name: str, url: str, transport: str) -> str:
    payload: dict[str, object] = {"url": url}
    if transport and transport != "sse":
        payload["transport"] = transport
    return f"openclaw mcp set {name} '{json.dumps(payload, separators=(', ', ': '))}'"


def _openclaw_bundle_transport_name(transport: str | None) -> str | None:
    normalized = (transport or "sse").strip().lower()
    if normalized in {"streamable-http", "streamable_http", "http"}:
        return "streamable-http"
    if normalized == "sse":
        return None
    return normalized or None


def _render_openclaw_bundle_files(
    plugin_id: str,
    name: str,
    row: dict,
    gateway_port: int,
) -> dict[str, object]:
    bridge_url = _bridge_url(row, gateway_port)
    transport = _openclaw_bundle_transport_name(str(row.get("transport") or "sse"))
    plugin_manifest = {"name": plugin_id}
    server_config: dict[str, object] = {"url": bridge_url}
    if transport:
        server_config["transport"] = transport
    bundle_config = {"mcpServers": {name: server_config}}
    openclaw_config = {
        "plugins": {
            "entries": {
                plugin_id: {
                    "enabled": True,
                }
            }
        }
    }
    return {
        "plugin_id": plugin_id,
        "bridge_url": bridge_url,
        "plugin_manifest": plugin_manifest,
        "bundle_config": bundle_config,
        "openclaw_config": openclaw_config,
    }


def _merge_openclaw_bundle_enable_config(
    target_path: Path,
    plugin_id: str,
) -> dict[str, object]:
    if target_path.exists():
        data = json.loads(target_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{target_path} must contain a JSON object")
    else:
        data = {}

    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins

    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        plugins["entries"] = entries

    current = entries.get(plugin_id)
    if not isinstance(current, dict):
        current = {}
    current["enabled"] = True
    entries[plugin_id] = current

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


def _mcporter_transport_name(transport: str | None) -> str:
    if transport in {"streamable-http", "streamable_http"}:
        return "http"
    return transport or "sse"


def _render_mcporter_server_config(row: dict, gateway_port: int) -> dict:
    bridge_url = _bridge_url(row, gateway_port)
    entry: dict[str, object] = {
        "baseUrl": bridge_url,
    }
    transport = _mcporter_transport_name(str(row.get("transport") or "sse"))
    if transport and transport != "sse":
        entry["transport"] = transport
    credential_env = row.get("credential_env")
    if credential_env:
        entry["headers"] = {
            # mcporter resolves $env:VAR at runtime; the real token stays on the host/sandbox env
            "Authorization": f"$env:{credential_env}",
        }
    purpose = row.get("purpose")
    if purpose:
        entry["description"] = str(purpose)
    return entry


def _render_mcporter_add_command(name: str, row: dict, gateway_port: int) -> str:
    bridge_url = _bridge_url(row, gateway_port)
    transport = _mcporter_transport_name(str(row.get("transport") or "sse"))
    command = f"mcporter config add '{name}' --url '{bridge_url}'"
    if transport:
        command += f" --transport '{transport}'"
    command += " --scope home"
    return command


def _default_bridge_host_candidates() -> list[str]:
    candidates: list[str] = []
    env_host = os.environ.get("GUARD_BRIDGE_HOST")
    if env_host and env_host not in candidates:
        candidates.append(env_host)
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.connect(("8.8.8.8", 80))
        local_ip = udp.getsockname()[0]
        udp.close()
        if local_ip and local_ip not in candidates:
            candidates.append(local_ip)
    except Exception:
        pass
    # In remote NemoClaw deployments, host.openshell.internal may resolve to
    # docker bridge IPs such as 172.17.0.1 without exposing a reachable host
    # HTTP listener. Keep it only as a compatibility fallback.
    for name in ("host.openshell.internal", "172.17.0.1", "172.18.0.1"):
        if name not in candidates:
            candidates.append(name)
    return candidates


def _bridge_probe_script(host_alias: str, gateway_port: int) -> str:
    return (
        "node -e "
        f"\"fetch('http://{host_alias}:{gateway_port}/health')"
        ".then(r=>{if(!r.ok){throw new Error(`HTTP ${r.status}`)} return r.text()})"
        ".then(t=>console.log(t))"
        ".catch(e=>{console.error(e);process.exit(1)})\""
    )


def _probe_bridge_host_alias(
    sandbox_name: str,
    host_alias: str,
    gateway_port: int,
    *,
    openshell_bin: str = "openshell",
    gateway_name: str = "nemoclaw",
    timeout: int = 30,
) -> tuple[bool, str]:
    command = [
        openshell_bin,
        "sandbox",
        "exec",
        "-g",
        gateway_name,
        "--name",
        sandbox_name,
        "--no-tty",
        "--timeout",
        str(timeout),
        "--",
        "bash",
        "-lc",
        _bridge_probe_script(host_alias, gateway_port),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    return result.returncode == 0, output


def _collect_bridge_rows(workspace: str, sandbox_name: str | None) -> list[dict]:
    rows = bridge_state.list_bridges(workspace, sandbox_name)
    return [row for row in rows if row.get("name")]


def _build_mcporter_payload(rows: list[dict], gateway_port: int, active_only: bool) -> dict:
    mcp_servers: dict[str, dict] = {}
    for row in rows:
        if active_only and row.get("status") != "active":
            continue
        name = str(row.get("name") or "")
        if not name:
            continue
        mcp_servers[name] = _render_mcporter_server_config(row, gateway_port)
    return {"mcpServers": mcp_servers}

# ---------------------------------------------------------------------------
# All providers that we register. Each one points to our security gateway
# instead of the real API endpoint. The gateway handles routing internally.
# ---------------------------------------------------------------------------
MANAGED_PROVIDERS = {
    "openai": {
        "type": "openai",
        "display": "OpenAI (gpt-4o, o1, o3...)",
        "api_key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "type": "openai",
        "display": "Anthropic (claude-3.5, claude-opus...)",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openrouter": {
        "type": "openai",
        "display": "OpenRouter (any model)",
        "api_key_env": "OPENROUTER_API_KEY",
    },
}


def _register_managed_providers(gateway_url: str) -> None:
    typer.echo("\nRegistering all providers -> security gateway...")
    for provider_name, provider_config in MANAGED_PROVIDERS.items():
        subprocess.run(
            ["openshell", "provider", "delete", provider_name],
            capture_output=True,
        )

        cmd = [
            "openshell",
            "provider",
            "create",
            "--name",
            provider_name,
            "--type",
            provider_config["type"],
            "--credential",
            "OPENAI_API_KEY=guard-managed",
            "--config",
            f"OPENAI_BASE_URL={gateway_url}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            typer.secho(
                f"   OK {provider_name}: {provider_config['display']} -> gateway",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho(
                f"   WARN {provider_name}: {result.stderr.strip()}",
                fg=typer.colors.YELLOW,
            )


def _set_default_inference_route() -> None:
    typer.echo("\nSetting default inference route...")
    subprocess.run(
        [
            "openshell",
            "inference",
            "set",
            "--provider",
            "openrouter",
            "--model",
            "openrouter/auto",
            "--no-verify",
        ],
        capture_output=True,
    )


def _create_and_seed_sandbox(
    sandbox_name: str,
    agent: str,
    workspace_path: str,
    policy_path: str,
) -> None:
    typer.echo("\nProvisioning sandbox...")
    subprocess.run(
        ["openshell", "sandbox", "delete", sandbox_name],
        capture_output=True,
    )

    create_cmd = [
        "openshell",
        "sandbox",
        "create",
        "--name",
        sandbox_name,
        "--policy",
        policy_path,
        "--from",
        agent,
    ]

    subprocess.run(create_cmd, check=True, cwd=workspace_path)


@app.command()
def start(
    workspace: str = typer.Option(..., help="Path to host workspace (project root)"),
    sandbox_name: str = typer.Option("openclaw-sandbox", help="Name of the sandbox"),
    agent: str = typer.Option("openclaw", help="Base agent to run"),
    gateway_port: int = typer.Option(8090, help="Port for the security gateway"),
):
    """Launch an OpenClaw sandbox with host-side auditing and pre-generated configs."""
    typer.echo(f"Starting {agent} securely attached to {workspace}...")
    artifacts = prepare_onboarding(
        workspace=workspace,
        sandbox_name=sandbox_name,
        gateway_port=gateway_port,
    )

    typer.echo(f"\nSecurity Gateway URL: {artifacts.gateway_url}")
    typer.echo(f"Prepared immutable configs in: {artifacts.immutable_openclaw_dir}")

    _register_managed_providers(artifacts.gateway_url)
    _set_default_inference_route()

    try:
        _create_and_seed_sandbox(
            sandbox_name=sandbox_name,
            agent=agent,
            workspace_path=str(artifacts.workspace_path),
            policy_path=str(artifacts.policy_path),
        )
    except subprocess.CalledProcessError as exc:
        typer.secho(f"\nCLI Error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.secho("\n=======================================", fg=typer.colors.GREEN)
    typer.secho("OK Sandbox Created - UNBYPASSABLE Security Mode", fg=typer.colors.GREEN)
    typer.secho("=======================================", fg=typer.colors.GREEN)
    typer.secho("\nSecurity architecture:", fg=typer.colors.WHITE)
    typer.secho("  ALL providers -> Security Gateway -> Real Provider", fg=typer.colors.CYAN)
    typer.secho("  Sandbox users CANNOT create new providers", fg=typer.colors.CYAN)
    typer.secho("  API keys NEVER enter the sandbox", fg=typer.colors.CYAN)
    typer.secho(f"\n  Gateway:    {artifacts.gateway_url}", fg=typer.colors.YELLOW)
    typer.secho(f"  Workspace:  {artifacts.sandbox_data_dir}", fg=typer.colors.YELLOW)
    typer.secho("  Audit DB:   logs/security_audit.db", fg=typer.colors.YELLOW)
    typer.secho("\nRegistered providers (all -> gateway):", fg=typer.colors.WHITE)
    for provider_name, provider_config in MANAGED_PROVIDERS.items():
        typer.secho(
            f"  - {provider_name}: {provider_config['display']}",
            fg=typer.colors.CYAN,
        )
    typer.secho("\nConnect:", fg=typer.colors.WHITE)
    typer.secho(f"  openshell sandbox connect {sandbox_name}", fg=typer.colors.CYAN)
    typer.secho("  openclaw tui", fg=typer.colors.CYAN)
    typer.secho("=======================================\n", fg=typer.colors.GREEN)


@app.command()
def onboard(
    workspace: str = typer.Option(..., help="Path to host workspace (project root)"),
    sandbox_name: str = typer.Option("openclaw-sandbox", help="Name of the sandbox"),
    gateway_port: int = typer.Option(8090, help="Port for the security gateway"),
):
    """Prepare immutable host-side configs for the NemoClaw blueprint flow."""
    artifacts = prepare_onboarding(
        workspace=workspace,
        sandbox_name=sandbox_name,
        gateway_port=gateway_port,
    )
    typer.echo("Onboarding artifacts generated:")
    typer.echo(f"  Policy:       {artifacts.policy_path}")
    typer.echo(f"  Config dir:   {artifacts.immutable_openclaw_dir}")
    typer.echo(f"  Gateway URL:  {artifacts.gateway_url}")


@app.command()
def stop(sandbox_name: str = typer.Argument("openclaw-sandbox")):
    """Stop and delete the sandbox."""
    subprocess.run(["openshell", "sandbox", "delete", sandbox_name], check=False)


@app.command()
def providers():
    """Show registered providers and their security status."""
    typer.echo("\nManaged providers (all route through the security gateway):\n")
    for provider_name, provider_config in MANAGED_PROVIDERS.items():
        key = os.environ.get(provider_config["api_key_env"], "")
        status = "OK key configured" if key else "WARN key not set"
        typer.echo(
            f"  {provider_name:12s}  {provider_config['display']:40s}  {status}"
        )
    typer.echo("")


# ── network whitelist subcommands ───────────────────────────────────────────
@net_app.command("list")
def net_list(
    scope: str = typer.Option("runtime", help="install | runtime"),
):
    """Show all whitelist entries for a scope."""
    gw = _gateway_config_path()
    try:
        default = gateway_config.get_default(gw, scope)
        entries = gateway_config.list_entries(gw, scope)
    except gateway_config.GatewayConfigError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo(f"\nnetwork.{scope}.default = {default}")
    typer.echo(f"network.{scope}.allow ({len(entries)} entries):\n")
    typer.echo(f"  {'HOST':<40} {'PORTS':<12} {'ENFORCE':<10} {'RPM':<8} PURPOSE")
    typer.echo(f"  {'-' * 40} {'-' * 12} {'-' * 10} {'-' * 8} {'-' * 20}")
    for e in entries:
        ports = ",".join(str(p) for p in e.ports) or "*"
        enf = e.enforcement or "-"
        rpm = str(e.rpm) if e.rpm is not None else "-"
        typer.echo(f"  {e.host:<40} {ports:<12} {enf:<10} {rpm:<8} {e.purpose}")
    typer.echo("")


@net_app.command("add")
def net_add(
    host: str = typer.Argument(..., help="Hostname (e.g. api.deepseek.com or *.foo.com)"),
    scope: str = typer.Option("runtime", help="install | runtime"),
    port: list[int] = typer.Option([443], "--port", "-p", help="Port (repeatable)"),
    enforcement: str = typer.Option(
        None, "--enforcement", "-e",
        help="enforce | warn | monitor (omit to use scope default)",
    ),
    purpose: str = typer.Option("", "--purpose", help="Free-text reason"),
    rpm: int = typer.Option(None, "--rpm", help="Per-host rate limit (req/min)"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
    no_reload: bool = typer.Option(False, "--no-reload", help="Skip gateway hot-reload"),
):
    """Add a hostname to the whitelist and hot-reload the gateway."""
    gw = _gateway_config_path()
    try:
        added = gateway_config.add_entry(
            gw, scope=scope, host=host, ports=port,
            enforcement=enforcement, purpose=purpose, rpm=rpm,
        )
    except gateway_config.GatewayConfigError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    if not added:
        typer.secho(
            f"   {host!r} already exists in network.{scope}.allow — no change",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(0)

    typer.secho(f"   OK added {host} to network.{scope}.allow", fg=typer.colors.GREEN)
    if not no_reload:
        token = os.environ.get("GUARD_ADMIN_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        _gateway_reload(gateway_url, token)


@net_app.command("remove")
def net_remove(
    host: str = typer.Argument(...),
    scope: str = typer.Option("runtime", help="install | runtime"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
    no_reload: bool = typer.Option(False, "--no-reload"),
):
    """Remove a hostname from the whitelist and hot-reload the gateway."""
    gw = _gateway_config_path()
    try:
        removed = gateway_config.remove_entry(gw, scope=scope, host=host)
    except gateway_config.GatewayConfigError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    if not removed:
        typer.secho(
            f"   {host!r} not found in network.{scope}.allow",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(0)

    typer.secho(f"   OK removed {host} from network.{scope}.allow", fg=typer.colors.GREEN)
    if not no_reload:
        token = os.environ.get("GUARD_ADMIN_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        _gateway_reload(gateway_url, token)


@net_app.command("reload")
def net_reload(
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Hot-reload the gateway's network policy from gateway.yaml.

    Note: this only refreshes gateway.py.  network_capture.py reloads its
    DNSForwardCache on its own TTL (default 300 s); restart its systemd unit
    if you need an immediate effect there:
        sudo systemctl restart guard-network-capture
    """
    token = os.environ.get("GUARD_ADMIN_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    _gateway_reload(gateway_url, token)


# ── mcp subcommands (HTTP-only thin wrappers) ───────────────────────────────
def _print_mcp_row(s: dict) -> None:
    typer.echo(
        f"  {s.get('name','?'):<24} {s.get('status','?'):<10} "
        f"{s.get('transport','?'):<16} {s.get('url','?')}"
    )


def _extract_mcp_servers(data: dict | list) -> list[dict]:
    if isinstance(data, list):
        return data
    return data.get("servers", [])


def _find_mcp_server(name: str, gateway_url: str) -> dict | None:
    servers = _extract_mcp_servers(
        _gateway_admin_request("GET", "/v1/mcp/servers", gateway_url=gateway_url)
    )
    for server in servers:
        if server.get("name") == name:
            return server
    return None


def _recent_mcp_events(name: str, gateway_url: str, limit: int = 20) -> list[dict]:
    data = _gateway_admin_request(
        "GET", "/v1/mcp/events", gateway_url=gateway_url, params={"limit": limit}
    )
    rows = data if isinstance(data, list) else data.get("events", [])
    return [row for row in rows if row.get("server_name") == name]


def _runtime_allowlist_hosts() -> set[str]:
    gw = _gateway_config_path()
    try:
        return {entry.host for entry in gateway_config.list_entries(gw, "runtime")}
    except gateway_config.GatewayConfigError:
        return set()


def _resolve_install_template(
    name: str,
    url: str | None,
    transport: str | None,
    purpose: str,
    credential_env: str | None,
) -> tuple[str, str, str, str | None, list[str]]:
    """Return (url, transport, purpose, credential_env, allowlist_hosts)."""
    template = MCP_INSTALL_TEMPLATES.get(name)
    if template is None:
        if not url:
            typer.secho(
                f"ERROR: no built-in install template for {name!r}; "
                "please provide the upstream URL.\n"
                f"Available templates: {', '.join(sorted(MCP_INSTALL_TEMPLATES))}",
                fg=typer.colors.RED,
            )
            raise typer.Exit(2)
        return url, transport or "sse", purpose, credential_env, []

    resolved_url = url or template["url"]
    resolved_transport = transport or template["transport"]
    resolved_purpose = purpose or template["purpose"]
    resolved_cred = credential_env or template.get("credential_env_hint")
    allowlist_hosts = template.get("allowlist_hosts", [])
    return resolved_url, resolved_transport, resolved_purpose, resolved_cred, allowlist_hosts


@mcp_app.command("list")
def mcp_list(
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """List all registered MCP servers (queries the gateway)."""
    servers = _extract_mcp_servers(
        _gateway_admin_request("GET", "/v1/mcp/servers", gateway_url=gateway_url)
    )
    if not servers:
        typer.echo("\nNo MCP servers registered.\n")
        return
    typer.echo(f"\nMCP servers ({len(servers)}):\n")
    typer.echo(f"  {'NAME':<24} {'STATUS':<10} {'TRANSPORT':<16} URL")
    typer.echo(f"  {'-' * 24} {'-' * 10} {'-' * 16} {'-' * 30}")
    for srv in servers:
        _print_mcp_row(srv)
    typer.echo("")


@mcp_app.command("register")
def mcp_register(
    name: str = typer.Argument(..., help="Unique slug, URL-safe (used in /mcp/<name>/...)"),
    url: str = typer.Argument(..., help="Upstream MCP URL"),
    transport: str = typer.Option("sse", "--transport", "-t", help="sse | streamable_http"),
    credential_env: str = typer.Option(
        None, "--credential-env",
        help="Env var holding the upstream bearer token (injected by gateway)",
    ),
    purpose: str = typer.Option("", "--purpose", help="Free-text reason"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Register a new MCP server in pending state."""
    body = {"name": name, "url": url, "transport": transport, "purpose": purpose}
    if credential_env:
        body["credential_env"] = credential_env
    data = _gateway_admin_request(
        "POST", "/v1/mcp/servers", gateway_url=gateway_url, json=body,
    )
    typer.secho(f"   OK registered MCP server {name!r} (status=pending)", fg=typer.colors.GREEN)
    if isinstance(data, dict) and data:
        _print_mcp_row(data)


def _compute_event_stats(events: list[dict]) -> dict:
    """Compute summary stats from a list of MCP events."""
    calls = [e for e in events if e.get("action") == "call"]
    total = len(calls)
    if total == 0:
        return {"total_calls": 0}
    allowed = sum(1 for e in calls if e.get("decision") == "allow")
    blocked = sum(1 for e in calls if e.get("decision") in ("block", "deny"))
    latencies = [e["latency_ms"] for e in calls if e.get("latency_ms") is not None]
    avg_latency = round(sum(latencies) / len(latencies)) if latencies else None
    error_count = sum(
        1 for e in calls
        if e.get("upstream_status") is not None and e["upstream_status"] >= 400
    )
    return {
        "total_calls": total,
        "allowed": allowed,
        "blocked": blocked,
        "error_count": error_count,
        "avg_latency_ms": avg_latency,
    }

def _find_allowlist_entry(host: str) -> "gateway_config.NetEntry | None":
    """Find the full allowlist entry for a host, or None."""
    gw = _gateway_config_path()
    try:
        for entry in gateway_config.list_entries(gw, "runtime"):
            if entry.host == host:
                return entry
    except gateway_config.GatewayConfigError:
        pass
    return None


def _openclaw_transport_name(transport: str | None) -> str:
    if transport == "streamable_http":
        return "streamable-http"
    return transport or "sse"


def _openclaw_mcp_payload(server: dict) -> dict:
    payload: dict[str, object] = {
        "url": server.get("url", ""),
    }
    transport = _openclaw_transport_name(server.get("transport"))
    if transport != "sse":
        payload["transport"] = transport
    credential_env = server.get("credential_env")
    if credential_env:
        payload["headers"] = {
            "Authorization": f"Bearer openshell:resolve:env:{credential_env}",
        }
    return payload


def _render_openclaw_mcp_set_command(server: dict) -> str:
    payload = json.dumps(_openclaw_mcp_payload(server), separators=(", ", ": "))
    return f"openclaw mcp set {server.get('name', 'mcp')} '{payload}'"


def _print_mcp_next_steps(server: dict, sandbox_name: str) -> None:
    name = server.get("name", "-")
    credential_env = server.get("credential_env")
    typer.echo("")
    typer.echo("Next step in the supported NemoClaw/OpenClaw path:")
    if credential_env:
        typer.echo(
            f"  1. Create or update an OpenShell provider that supplies `{credential_env}`"
        )
        typer.echo(
            f"     Example: openshell provider create --name {name}-mcp --type generic --credential {credential_env}"
        )
        typer.echo(
            f"  2. Regenerate the host-side mounted OpenClaw config so `{name}` is present under `mcp.servers`"
        )
        typer.echo(
            f"     Example: guard mcp sync --workspace . --sandbox {sandbox_name}"
        )
        typer.echo(
            f"  3. Recreate sandbox `{sandbox_name}` with `--provider {name}-mcp` attached so the mounted config and secret resolution are both available"
        )
    else:
        typer.echo("  1. No provider credential is required for this MCP server")
        typer.echo(
            f"  2. Regenerate the host-side mounted OpenClaw config:"
        )
        typer.echo(
            f"     guard mcp sync --workspace . --sandbox {sandbox_name}"
        )
        typer.echo(
            f"  3. Recreate sandbox `{sandbox_name}` so the mounted MCP registry becomes active"
        )
    typer.echo("")


@mcp_app.command("status")
def mcp_status(
    name: str = typer.Argument(..., help="Registered MCP server name"),
    event_limit: int = typer.Option(5, "--event-limit", help="How many recent events to show"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Show one MCP server with approval, transport, URL, credential ref,
    host allowlist details, and recent audit event summary."""
    server = _find_mcp_server(name, gateway_url)
    if not server:
        typer.secho(f"ERROR: MCP server {name!r} not found", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo("")
    typer.echo(f"Name:           {server.get('name', '-')}")
    typer.echo(f"Status:         {server.get('status', '-')}")
    typer.echo(f"Transport:      {server.get('transport', '-')}")
    typer.echo(f"URL:            {server.get('url', '-')}")

    # ── host allowlist detail ─────────────────────────────────────────
    upstream_host = urlparse(server.get("url", "")).hostname or "-"
    entry = _find_allowlist_entry(upstream_host) if upstream_host != "-" else None
    typer.echo(f"Upstream host:  {upstream_host}")
    if entry:
        ports = ",".join(str(p) for p in entry.ports) or "*"
        enf = entry.enforcement or "(scope default)"
        rpm = str(entry.rpm) if entry.rpm is not None else "-"
        typer.echo(f"Runtime allow:  yes  ports={ports}  enforcement={enf}  rpm={rpm}")
        if entry.purpose:
            typer.echo(f"Allow purpose:  {entry.purpose}")
    else:
        typer.echo("Runtime allow:  no")

    typer.echo(f"Credential env: {server.get('credential_env') or '-'}")
    typer.echo(f"Purpose:        {server.get('purpose') or '-'}")
    typer.echo(f"Registered at:  {server.get('registered_at') or '-'}")
    typer.echo(f"Approved at:    {server.get('approved_at') or '-'}")
    typer.echo(f"Approved by:    {server.get('approved_by') or '-'}")

    # ── event stats summary ───────────────────────────────────────────
    all_events = _recent_mcp_events(name, gateway_url, limit=max(event_limit, 1) * 4)
    stats = _compute_event_stats(all_events)
    typer.echo("")
    if stats["total_calls"] > 0:
        typer.echo("Event summary:")
        typer.echo(f"  Total calls:    {stats['total_calls']}")
        typer.echo(f"  Allowed:        {stats['allowed']}")
        typer.echo(f"  Blocked:        {stats['blocked']}")
        typer.echo(f"  Upstream errors: {stats['error_count']}")
        if stats["avg_latency_ms"] is not None:
            typer.echo(f"  Avg latency:    {stats['avg_latency_ms']}ms")
        typer.echo("")

    typer.echo("Recent events:")
    if not all_events:
        typer.echo("  - none")
    else:
        for event in all_events[:event_limit]:
            action = event.get("action", "-")
            decision = event.get("decision") or "-"
            upstream_status = event.get("upstream_status")
            latency_ms = event.get("latency_ms")
            extras: list[str] = []
            if upstream_status is not None:
                extras.append(f"http={upstream_status}")
            if latency_ms is not None:
                extras.append(f"latency={latency_ms}ms")
            extra_text = f" ({', '.join(extras)})" if extras else ""
            typer.echo(f"  - {action} / {decision}{extra_text}")
    if server.get("status") == "approved":
        _print_mcp_next_steps(server, "my-assistant")
    typer.echo("")


@mcp_app.command("templates")
def mcp_templates():
    """List available built-in MCP install templates."""
    typer.echo(f"\nAvailable MCP templates ({len(MCP_INSTALL_TEMPLATES)}):\n")
    typer.echo(f"  {'NAME':<16} {'TRANSPORT':<18} {'CREDENTIAL_ENV':<24} PURPOSE")
    typer.echo(f"  {'-' * 16} {'-' * 18} {'-' * 24} {'-' * 30}")
    for tname, tpl in MCP_INSTALL_TEMPLATES.items():
        typer.echo(
            f"  {tname:<16} {tpl['transport']:<18} "
            f"{tpl.get('credential_env_hint', '-'):<24} {tpl['purpose']}"
        )
    typer.echo(
        "\nUsage:  guard mcp install <template-name> [--credential-env ENV] --by ACTOR\n"
    )


@mcp_app.command("install")
def mcp_install(
    name: str = typer.Argument(..., help="Known MCP slug / registered name"),
    url: str | None = typer.Argument(None, help="Upstream MCP URL (optional for known templates)"),
    transport: str | None = typer.Option(None, "--transport", "-t", help="sse | streamable_http"),
    credential_env: str | None = typer.Option(
        None,
        "--credential-env",
        help="Env var holding the upstream bearer token (template default used if omitted; optional for public MCP servers)",
    ),
    purpose: str = typer.Option("", "--purpose", help="Free-text reason"),
    by: str = typer.Option(..., "--by", help="Operator login (recorded for audit)"),
    no_auto_allow: bool = typer.Option(
        False, "--no-auto-allow",
        help="Do NOT auto-add the upstream host to network.runtime.allow",
    ),
    sandbox_name: str = typer.Option(
        "my-assistant", "--sandbox", help="NemoClaw sandbox to apply network preset to",
    ),
    no_sandbox_policy: bool = typer.Option(
        False, "--no-sandbox-policy",
        help="Skip OpenShell sandbox policy update",
    ),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Register and approve an MCP server in one step.

    For known templates (github, slack, linear, brave-search, sentry),
    URL, transport, and credential-env are pre-filled automatically.
    Use 'guard mcp templates' to see available templates.

    After approval, Guard auto-generates an OpenShell network preset so the
    sandbox can reach the MCP upstream host. Operators then regenerate the
    host-side mounted OpenClaw config and recreate the sandbox so OpenClaw can
    see the approved MCP registry on startup.
    """
    existing = _find_mcp_server(name, gateway_url)
    if existing:
        typer.secho(
            f"ERROR: MCP server {name!r} already exists; use 'guard mcp status {name}' "
            "or the lower-level admin commands.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    resolved_url, resolved_transport, resolved_purpose, resolved_cred, allowlist_hosts = (
        _resolve_install_template(name, url, transport, purpose, credential_env)
    )

    if not resolved_cred and name in MCP_INSTALL_TEMPLATES:
        typer.secho(
            "ERROR: --credential-env is required (no template default available)",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    body = {
        "name": name,
        "url": resolved_url,
        "transport": resolved_transport,
        "purpose": resolved_purpose,
    }
    if resolved_cred:
        body["credential_env"] = resolved_cred

    _gateway_admin_request(
        "POST", "/v1/mcp/servers", gateway_url=gateway_url, json=body,
    )
    _gateway_admin_request(
        "POST",
        f"/v1/mcp/servers/{name}/approve",
        gateway_url=gateway_url,
        json={"actor": by, "auto_allow": not no_auto_allow},
    )

    typer.secho(
        f"   OK installed MCP server {name!r} and approved it for use",
        fg=typer.colors.GREEN,
    )
    if name in MCP_INSTALL_TEMPLATES:
        typer.echo(f"   Template: {name} -> {resolved_url} ({resolved_transport})")
        if resolved_cred and not credential_env:
            typer.echo(f"   Credential env (from template): {resolved_cred}")
    installed = _find_mcp_server(name, gateway_url)
    if installed:
        _print_mcp_row(installed)
        _print_mcp_next_steps(installed, sandbox_name)

    # ── Generate OpenShell sandbox preset and apply ──────────────────
    if not no_sandbox_policy:
        all_hosts = list(allowlist_hosts) if allowlist_hosts else []
        url_hosts = sandbox_policy.hosts_from_url(resolved_url)
        for h in url_hosts:
            if h not in all_hosts:
                all_hosts.append(h)

        if all_hosts:
            preset = sandbox_policy.generate_preset(
                name,
                description=f"MCP {name} upstream access",
                hosts=all_hosts,
            )
            written = sandbox_policy.write_preset_file(name, preset)
            for p in written:
                typer.echo(f"   Preset: {p}")

            ok, msg = sandbox_policy.apply_sandbox_policy(sandbox_name)
            if ok:
                typer.secho(f"   OK {msg}", fg=typer.colors.GREEN)
            else:
                typer.secho(f"   WARN {msg}", fg=typer.colors.YELLOW)


@mcp_app.command("approve")
def mcp_approve(
    name: str = typer.Argument(...),
    by: str = typer.Option(..., "--by", help="Operator login (recorded for audit)"),
    no_auto_allow: bool = typer.Option(
        False, "--no-auto-allow",
        help="Do NOT auto-add the upstream host to network.runtime.allow",
    ),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Approve an MCP server so the sandbox can call it."""
    body = {"actor": by, "auto_allow": not no_auto_allow}
    _gateway_admin_request(
        "POST", f"/v1/mcp/servers/{name}/approve",
        gateway_url=gateway_url, json=body,
    )
    typer.secho(f"   OK {name!r} approved by {by}", fg=typer.colors.GREEN)


@mcp_app.command("deny")
def mcp_deny(
    name: str = typer.Argument(...),
    by: str = typer.Option(..., "--by"),
    reason: str = typer.Option("", "--reason"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Deny a pending MCP server."""
    _gateway_admin_request(
        "POST", f"/v1/mcp/servers/{name}/deny",
        gateway_url=gateway_url, json={"actor": by, "reason": reason},
    )
    typer.secho(f"   OK {name!r} denied by {by}", fg=typer.colors.YELLOW)


@mcp_app.command("revoke")
def mcp_revoke(
    name: str = typer.Argument(...),
    by: str = typer.Option(..., "--by"),
    reason: str = typer.Option("", "--reason"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Revoke an approved MCP server (subsequent calls will be blocked)."""
    _gateway_admin_request(
        "POST", f"/v1/mcp/servers/{name}/revoke",
        gateway_url=gateway_url, json={"actor": by, "reason": reason},
    )
    typer.secho(f"   OK {name!r} revoked by {by}", fg=typer.colors.YELLOW)


@mcp_app.command("remove")
def mcp_remove(
    name: str = typer.Argument(...),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Permanently delete an MCP server entry."""
    _gateway_admin_request(
        "DELETE", f"/v1/mcp/servers/{name}", gateway_url=gateway_url,
    )
    typer.secho(f"   OK {name!r} removed", fg=typer.colors.GREEN)


@mcp_app.command("uninstall")
def mcp_uninstall(
    name: str = typer.Argument(..., help="Registered MCP server name"),
    sandbox_name: str = typer.Option(
        "my-assistant", "--sandbox", help="NemoClaw sandbox to update policy for",
    ),
    no_sandbox_policy: bool = typer.Option(
        False, "--no-sandbox-policy",
        help="Skip OpenShell sandbox policy update",
    ),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Remove a registered MCP server and its sandbox network preset."""
    server = _find_mcp_server(name, gateway_url)
    if not server:
        typer.secho(f"ERROR: MCP server {name!r} not found", fg=typer.colors.RED)
        raise typer.Exit(1)
    _gateway_admin_request(
        "DELETE", f"/v1/mcp/servers/{name}", gateway_url=gateway_url,
    )
    typer.secho(f"   OK uninstalled MCP server {name!r}", fg=typer.colors.GREEN)

    if not no_sandbox_policy:
        removed = sandbox_policy.remove_preset_file(name)
        for p in removed:
            typer.echo(f"   Removed preset: {p}")

        ok, msg = sandbox_policy.apply_sandbox_policy(sandbox_name)
        if ok:
            typer.secho(f"   OK {msg}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"   WARN {msg}", fg=typer.colors.YELLOW)


@mcp_app.command("sync")
def mcp_sync(
    sandbox_name: str = typer.Option(
        "my-assistant", "--sandbox", help="NemoClaw sandbox name",
    ),
    workspace: str = typer.Option(
        ".", "--workspace", help="Project root (contains gateway.yaml)",
    ),
    no_recreate: bool = typer.Option(
        False, "--no-recreate",
        help="Deprecated compatibility flag; Guard only stages host-side config",
    ),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Guard gateway port"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Stage host-side MCP config for the normal NemoClaw path.

    Under the standard NemoClaw/OpenClaw install flow, Guard does not mutate
    sandbox ``openclaw.json`` from inside the sandbox. Instead, Guard writes
    the host-side mapped ``sandbox_workspace/openclaw/openclaw.json`` and
    operators recreate the sandbox when needed.

    This command refreshes the host-side mounted config from ``gateway.yaml`` so
    approved MCP servers are present under ``mcp.servers``.
    """
    del no_recreate  # compatibility flag during migration

    servers = _extract_mcp_servers(
        _gateway_admin_request("GET", "/v1/mcp/servers", gateway_url=gateway_url)
    )
    approved = [srv for srv in servers if srv.get("status") == "approved" and srv.get("url")]
    if not approved:
        typer.secho("   No approved MCP servers with URLs in gateway.yaml", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    artifacts = prepare_onboarding(
        workspace=workspace,
        sandbox_name=sandbox_name,
        gateway_port=gateway_port,
    )

    typer.secho(
        f"   OK staged host-side OpenClaw config in {artifacts.immutable_openclaw_dir}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"   Policy refreshed: {artifacts.policy_path}")
    typer.echo(f"\nApproved MCP servers for sandbox {sandbox_name!r}:\n")
    for server in approved:
        typer.echo(f"  {server.get('name', '-'):<20} {server.get('url', '-')}")
        typer.echo(f"  {'':20} transport={_openclaw_transport_name(server.get('transport'))}")
        if server.get("credential_env"):
            typer.echo(f"  {'':20} credential_env={server['credential_env']}")
        typer.echo("")

    typer.echo("Guard wrote the MCP registry into the host-side mapped OpenClaw config.")
    typer.echo("Recreate the sandbox if it is already running so the refreshed config is mounted.")


@mcp_app.command("logs")
def mcp_logs(
    limit: int = typer.Option(50, "--limit", "-n"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """Show recent MCP audit events."""
    data = _gateway_admin_request(
        "GET", "/v1/mcp/events",
        gateway_url=gateway_url, params={"limit": limit},
    )
    rows = data if isinstance(data, list) else data.get("events", [])
    if not rows:
        typer.echo("\nNo MCP events.\n")
        return
    typer.echo(f"\nMCP events ({len(rows)}):\n")
    typer.echo(f"  {'TIMESTAMP':<27} {'SERVER':<20} {'ACTION':<10} {'DECISION':<8} ACTOR")
    typer.echo(f"  {'-' * 27} {'-' * 20} {'-' * 10} {'-' * 8} {'-' * 12}")
    for r in rows:
        typer.echo(
            f"  {(r.get('timestamp') or '')[:26]:<27} "
            f"{(r.get('server_name') or '')[:20]:<20} "
            f"{(r.get('action') or ''):<10} "
            f"{(r.get('decision') or '-'):<8} "
            f"{(r.get('actor') or '-')}"
        )
    typer.echo("")


@bridge_app.command("list")
def bridge_list(
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    sandbox_name: str | None = typer.Option(None, "--sandbox", help="Filter by sandbox name"),
):
    """List self-hosted MCP bridge compatibility records."""
    rows = bridge_state.list_bridges(workspace, sandbox_name)
    path = bridge_state.resolve_bridge_state_path(workspace)
    if not rows:
        typer.echo(f"\nNo bridge records in {path}\n")
        return
    typer.echo(f"\nBridge records ({path}):\n")
    typer.echo(f"  {'SANDBOX':<16} {'NAME':<18} {'STATUS':<10} {'TRANSPORT':<18} ENDPOINT")
    typer.echo(f"  {'-' * 16} {'-' * 18} {'-' * 10} {'-' * 18} {'-' * 30}")
    for row in rows:
        _print_bridge_row(row)
    typer.echo("")


@bridge_app.command("add")
def bridge_add(
    name: str = typer.Argument(..., help="Approved MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
    host_alias: str = typer.Option(
        "",
        "--host-alias",
        help="Sandbox-visible IP/domain for the Guard gateway bridge URL (defaults to GUARD_BRIDGE_HOST or compatibility fallback)",
    ),
    host_port: int | None = typer.Option(
        None,
        "--host-port",
        help="Reserved host-side bridge port, if already allocated externally",
    ),
):
    """Record a host-to-sandbox MCP bridge for approved HTTP MCP servers.

    The current minimal runtime uses the existing Guard gateway reverse proxy
    (`/mcp/{server}`) as the bridge executor for approved HTTP MCP servers.
    """
    server = _find_mcp_server(name, gateway_url)
    if not server:
        typer.secho(f"ERROR: MCP server {name!r} not found", fg=typer.colors.RED)
        raise typer.Exit(1)
    if server.get("status") != "approved":
        typer.secho(
            f"ERROR: MCP server {name!r} is not approved (status={server.get('status')})",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if not server.get("url"):
        typer.secho(
            f"ERROR: MCP server {name!r} has no upstream URL and cannot use the compatibility bridge",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    spec = _build_bridge_spec(server, sandbox_name)
    spec.host_alias = host_alias or _default_bridge_host()
    spec.host_port = host_port
    path = bridge_state.upsert_bridge(workspace, sandbox_name, name, spec.to_record())
    typer.secho(
        f"   OK recorded planned MCP bridge {name!r} for sandbox {sandbox_name!r}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"   State: {path}")
    typer.echo(f"   Upstream: {spec.upstream_url}")
    typer.echo(f"   Transport: {spec.transport}")
    typer.echo(f"   Host alias: {spec.host_alias}")
    if spec.credential_env:
        typer.echo(f"   Credential env: {spec.credential_env}")
    if spec.allowed_hosts:
        typer.echo(f"   Allowed hosts: {', '.join(spec.allowed_hosts)}")
    typer.echo("")
    typer.echo("Next implementation steps for this bridge record:")
    typer.echo("  1. Start a host-side stdio->HTTP MCP proxy for the approved server.")
    typer.echo("  2. Allow the sandbox to reach that host endpoint via OpenShell policy/runtime rules.")
    typer.echo("  3. Register the bridged HTTP endpoint with sandbox-side mcporter/OpenClaw consumption.")


@bridge_app.command("activate")
def bridge_activate(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
    host_alias: str | None = typer.Option(
        None,
        "--host-alias",
        help="Override the sandbox-visible IP/domain used by the bridge URL",
    ),
    auto_detect_host_alias: bool = typer.Option(
        False,
        "--auto-detect-host-alias",
        help="Probe candidate bridge hosts from the sandbox and persist the first reachable one",
    ),
    openshell_bin: str = typer.Option(
        "openshell",
        "--openshell-bin",
        help="Path/name of the openshell CLI used for sandbox probing",
    ),
    gateway_name: str = typer.Option(
        "nemoclaw",
        "--gateway-name",
        help="OpenShell gateway/group name used by openshell sandbox exec",
    ),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Guard gateway port"),
):
    """Activate the minimal bridge runtime using Guard gateway MCP proxy routes.

    This path currently supports approved HTTP/SSE/streamable MCP upstreams that
    can be served through the existing `/mcp/{server}` reverse proxy in
    `guard.gateway`.
    """
    row = bridge_state.get_bridge(workspace, sandbox_name, name)
    if not row:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    server = _find_mcp_server(name, gateway_url)
    if not server:
        typer.secho(f"ERROR: MCP server {name!r} not found", fg=typer.colors.RED)
        raise typer.Exit(1)
    if server.get("status") != "approved":
        typer.secho(
            f"ERROR: MCP server {name!r} is not approved (status={server.get('status')})",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if not server.get("url"):
        typer.secho(
            f"ERROR: MCP server {name!r} has no upstream URL",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    updates: dict[str, object] = {}
    if auto_detect_host_alias:
        candidates = []
        if host_alias:
            candidates.append(host_alias)
        candidates.extend(candidate for candidate in _default_bridge_host_candidates() if candidate not in candidates)
        detected_alias = None
        typer.echo("Probing bridge host aliases from sandbox:")
        for candidate in candidates:
            ok, detail = _probe_bridge_host_alias(
                sandbox_name,
                candidate,
                gateway_port,
                openshell_bin=openshell_bin,
                gateway_name=gateway_name,
            )
            if ok:
                typer.secho(f"  OK       {candidate}", fg=typer.colors.GREEN)
                detected_alias = candidate
                break
            typer.secho(f"  FAILED   {candidate}", fg=typer.colors.YELLOW)
            if detail:
                typer.echo(f"           {detail.splitlines()[-1][:200]}")
        if not detected_alias:
            typer.secho(
                "ERROR: no candidate bridge host alias was reachable from the sandbox",
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        host_alias = detected_alias
    if host_alias:
        updates["host_alias"] = host_alias
    elif not row.get("host_alias"):
        updates["host_alias"] = _default_bridge_host()
    if updates:
        bridge_state.upsert_bridge(workspace, sandbox_name, name, updates)
    path, found = bridge_state.mark_bridge_activated(
        workspace,
        sandbox_name,
        name,
        host_port=gateway_port,
    )
    if not found:
        typer.secho(
            f"ERROR: failed to activate bridge {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    active = bridge_state.get_bridge(workspace, sandbox_name, name) or row
    bridge_url = _bridge_url(active, gateway_port)
    typer.secho(
        f"   OK activated bridge {name!r} for sandbox {sandbox_name!r}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"   State: {path}")
    typer.echo(f"   Runtime: Guard gateway reverse proxy")
    typer.echo(f"   Bridge URL: {bridge_url}")
    typer.echo("")
    typer.echo("OpenClaw registration command:")
    typer.echo(f"  {_render_openclaw_mcp_command(name, bridge_url, str(active.get('transport') or 'sse'))}")


@bridge_app.command("remove")
def bridge_remove(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
):
    """Remove a planned or previously executed bridge record."""
    path, removed = bridge_state.remove_bridge(workspace, sandbox_name, name)
    if not removed:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.secho(f"   OK removed bridge record {name!r}", fg=typer.colors.GREEN)
    typer.echo(f"   State: {path}")


@bridge_app.command("restart")
def bridge_restart(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
):
    """Mark a bridge for restart/reconciliation.

    The real runtime executor is not implemented yet; this command updates the
    compatibility-layer state so a future executor can reconcile it.
    """
    path, found = bridge_state.mark_bridge_restarted(workspace, sandbox_name, name)
    if not found:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.secho(
        f"   OK marked bridge {name!r} for restart/reconciliation",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"   State: {path}")


@bridge_app.command("render")
def bridge_render(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Default Guard/bridge port"),
    output_format: str = typer.Option(
        "mcporter",
        "--format",
        help="mcporter | openclaw | json | env",
    ),
):
    """Render the planned bridge into a consumer-facing config snippet.

    This does not start the bridge runtime. It turns Guard's bridge planning
    record into a deterministic host endpoint and example client config.
    """
    row = bridge_state.get_bridge(workspace, sandbox_name, name)
    if not row:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    transport = str(row.get("transport") or "sse")
    bridge_url = _bridge_url(row, gateway_port)
    rendered = {
        "name": row.get("name"),
        "sandbox": row.get("sandbox"),
        "status": row.get("status"),
        "transport": transport,
        "bridge_url": bridge_url,
        "upstream_url": row.get("upstream_url"),
        "host_alias": row.get("host_alias") or _default_bridge_host(),
        "host_port": _bridge_port(row, gateway_port),
        "credential_env": row.get("credential_env"),
        "execution_model": row.get("execution_model"),
    }

    if output_format == "json":
        typer.echo(json.dumps(rendered, indent=2, sort_keys=True))
        return

    if output_format == "mcporter":
        mcporter_payload = {
            "mcpServers": {
                str(row.get("name") or name): _render_mcporter_server_config(row, gateway_port)
            }
        }
        typer.echo("# mcporter config snippet")
        typer.echo(json.dumps(mcporter_payload, indent=2, sort_keys=True))
        typer.echo("")
        typer.echo("# Preferred sandbox registration command")
        typer.echo(f"#   {_render_mcporter_add_command(name, row, gateway_port)}")
        return

    if output_format == "env":
        typer.echo(f"GUARD_BRIDGE_NAME={rendered['name']}")
        typer.echo(f"GUARD_BRIDGE_SANDBOX={rendered['sandbox']}")
        typer.echo(f"GUARD_BRIDGE_URL={bridge_url}")
        typer.echo(f"GUARD_BRIDGE_TRANSPORT={transport}")
        if rendered.get("credential_env"):
            typer.echo(f"GUARD_BRIDGE_CREDENTIAL_ENV={rendered['credential_env']}")
        return

    if output_format != "openclaw":
        typer.secho(
            f"ERROR: unsupported format {output_format!r}; use mcporter, openclaw, json, or env",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    typer.echo(f"# Planned bridge for sandbox {sandbox_name}")
    typer.echo(f"# Upstream: {row.get('upstream_url')}")
    if row.get("credential_env"):
        typer.echo(f"# Host-held credential env: {row['credential_env']}")
    typer.echo(f"# Planned bridge URL: {bridge_url}")
    typer.echo("")
    typer.echo(_render_openclaw_mcp_command(name, bridge_url, transport))
    typer.echo("")
    typer.echo("# JSON snippet")
    typer.echo(
        json.dumps(
            {
                name: {
                    "url": bridge_url,
                    **({"transport": transport} if transport != "sse" else {}),
                }
            },
            indent=2,
            sort_keys=True,
        )
    )


@bridge_app.command("render-mcporter-add")
def bridge_render_mcporter_add(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Default Guard/bridge port"),
):
    """Render the preferred sandbox-side mcporter registration command."""
    row = bridge_state.get_bridge(workspace, sandbox_name, name)
    if not row:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.echo(_render_mcporter_add_command(name, row, gateway_port))


@bridge_app.command("render-openclaw-bundle")
def bridge_render_openclaw_bundle(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Default Guard/bridge port"),
    plugin_id: str = typer.Option(
        "guard-mcp-bundle",
        "--plugin-id",
        help="Bundle plugin id/directory name to enable inside OpenClaw",
    ),
    plugin_root: str = typer.Option(
        "/sandbox/.openclaw/extensions",
        "--plugin-root",
        help="Sandbox-visible OpenClaw bundle extension root",
    ),
):
    """Render the OpenClaw 4.2 bundle-plugin files for native MCP consumption."""
    row = bridge_state.get_bridge(workspace, sandbox_name, name)
    if not row:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    rendered = _render_openclaw_bundle_files(plugin_id, name, row, gateway_port)
    bundle_dir = PurePosixPath(plugin_root) / plugin_id
    manifest_path = bundle_dir / ".claude-plugin" / "plugin.json"
    mcp_path = bundle_dir / ".mcp.json"

    typer.echo(f"# OpenClaw native bundle MCP for sandbox {sandbox_name}")
    typer.echo(f"# Bridge URL: {rendered['bridge_url']}")
    typer.echo(f"# Plugin id: {plugin_id}")
    typer.echo("")
    typer.echo("# 1. Bundle manifest")
    typer.echo(f"# Path: {manifest_path}")
    typer.echo(
        json.dumps(rendered["plugin_manifest"], indent=2, sort_keys=True)
    )
    typer.echo("")
    typer.echo("# 2. Bundle MCP config")
    typer.echo(f"# Path: {mcp_path}")
    typer.echo(json.dumps(rendered["bundle_config"], indent=2, sort_keys=True))
    typer.echo("")
    typer.echo("# 3. OpenClaw config snippet to enable the bundle plugin")
    typer.echo(json.dumps(rendered["openclaw_config"], indent=2, sort_keys=True))
    typer.echo("")
    typer.echo("# 4. Example sandbox commands")
    typer.echo(f"mkdir -p '{bundle_dir / '.claude-plugin'}'")
    typer.echo(f"cat > '{manifest_path}' <<'JSON'")
    typer.echo(json.dumps(rendered["plugin_manifest"], indent=2, sort_keys=True))
    typer.echo("JSON")
    typer.echo(f"cat > '{mcp_path}' <<'JSON'")
    typer.echo(json.dumps(rendered["bundle_config"], indent=2, sort_keys=True))
    typer.echo("JSON")
    typer.echo("# Then merge the OpenClaw config snippet above into the active host-mounted config")


@bridge_app.command("stage-openclaw-bundle")
def bridge_stage_openclaw_bundle(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Default Guard/bridge port"),
    plugin_id: str = typer.Option(
        "guard-mcp-bundle",
        "--plugin-id",
        help="Bundle plugin id/directory name to enable inside OpenClaw",
    ),
    output_dir: str = typer.Option(
        ...,
        "--output-dir",
        help="Directory where the OpenClaw bundle plugin should be written",
    ),
):
    """Write the OpenClaw 4.2 bundle-plugin files to a target directory."""
    row = bridge_state.get_bridge(workspace, sandbox_name, name)
    if not row:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    rendered = _render_openclaw_bundle_files(plugin_id, name, row, gateway_port)
    out_root = Path(output_dir)
    manifest_path = out_root / ".claude-plugin" / "plugin.json"
    mcp_path = out_root / ".mcp.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(rendered["plugin_manifest"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    mcp_path.write_text(
        json.dumps(rendered["bundle_config"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    typer.echo(f"Staged OpenClaw bundle plugin: {out_root}")
    typer.echo(f"  Plugin manifest: {manifest_path}")
    typer.echo(f"  MCP config:      {mcp_path}")
    typer.echo("  Enable snippet:")
    typer.echo(json.dumps(rendered["openclaw_config"], indent=2, sort_keys=True))


@bridge_app.command("enable-openclaw-bundle")
def bridge_enable_openclaw_bundle(
    plugin_id: str = typer.Option(
        "guard-mcp-bundle",
        "--plugin-id",
        help="Bundle plugin id/directory name to enable inside OpenClaw",
    ),
    config_path: str = typer.Option(
        ...,
        "--config-path",
        help="Path to the OpenClaw JSON config file to merge into",
    ),
):
    """Merge the bundle-plugin enable snippet into an OpenClaw config file."""
    target = Path(config_path)
    try:
        merged = _merge_openclaw_bundle_enable_config(target, plugin_id)
    except ValueError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo(f"Updated OpenClaw config: {target}")
    typer.echo("Enabled bundle plugin:")
    typer.echo(json.dumps(merged["plugins"]["entries"][plugin_id], indent=2, sort_keys=True))


@bridge_app.command("detect-host-alias")
def bridge_detect_host_alias(
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Optional bridge name to persist the detected host alias into bridge state",
    ),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Guard gateway port"),
    candidates: str | None = typer.Option(
        None,
        "--candidates",
        help="Comma-separated candidate host aliases; defaults to platform alias + detected IPs",
    ),
    openshell_bin: str = typer.Option(
        "openshell",
        "--openshell-bin",
        help="Path/name of the openshell CLI used for sandbox probing",
    ),
    gateway_name: str = typer.Option(
        "nemoclaw",
        "--gateway-name",
        help="OpenShell gateway/group name used by openshell sandbox exec",
    ),
):
    """Probe bridge host aliases from inside the sandbox and print the first reachable one."""
    candidate_list = (
        [item.strip() for item in candidates.split(",") if item.strip()]
        if candidates
        else _default_bridge_host_candidates()
    )
    typer.echo("Probing bridge host aliases from sandbox:")
    typer.echo("  Preference order: explicit/external host or detected host IP first;")
    typer.echo("  host.openshell.internal remains a compatibility fallback only.")
    detected_alias = None
    for candidate in candidate_list:
        ok, detail = _probe_bridge_host_alias(
            sandbox_name,
            candidate,
            gateway_port,
            openshell_bin=openshell_bin,
            gateway_name=gateway_name,
        )
        if ok:
            typer.secho(f"  OK       {candidate}", fg=typer.colors.GREEN)
            detected_alias = candidate
            break
        typer.secho(f"  FAILED   {candidate}", fg=typer.colors.YELLOW)
        if detail:
            typer.echo(f"           {detail.splitlines()[-1][:200]}")
    if not detected_alias:
        typer.secho("ERROR: no candidate bridge host alias was reachable", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo("")
    typer.echo(f"Detected bridge host alias: {detected_alias}")
    if name:
        row = bridge_state.get_bridge(workspace, sandbox_name, name)
        if not row:
            typer.secho(
                f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        path = bridge_state.upsert_bridge(
            workspace,
            sandbox_name,
            name,
            {"host_alias": detected_alias},
        )
        typer.echo(f"Updated bridge state: {path}")


@bridge_app.command("print-sandbox-steps")
def bridge_print_sandbox_steps(
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Default Guard/bridge port"),
    active_only: bool = typer.Option(
        True,
        "--active-only/--include-planned",
        help="Show only active bridges by default",
    ),
):
    """Print the concrete host + sandbox validation steps for native MCP first.

    This is a guidance command only. It does not execute remote operations.
    """
    rows = _collect_bridge_rows(workspace, sandbox_name)
    payload = _build_mcporter_payload(rows, gateway_port, active_only)
    if not payload["mcpServers"]:
        state_desc = "active" if active_only else "matching"
        typer.secho(
            f"ERROR: no {state_desc} bridge records found for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    active_rows = sorted(
        ((str(r.get("name")), r) for r in rows if (not active_only or r.get("status") == "active")),
        key=lambda item: item[0],
    )
    names = ", ".join(sorted(payload["mcpServers"].keys()))
    typer.echo("Host side")
    typer.echo(
        "1. Ensure the host-side bridge runtime is active:\n"
        + "\n".join(
            f"   guard bridge activate {name} --sandbox {sandbox_name} --workspace {workspace}"
            for name in sorted(payload["mcpServers"].keys())
        )
    )
    typer.echo("")
    typer.echo("Sandbox side")
    typer.echo(
        "2. Preferred path: stage the OpenClaw native MCP bundle:\n"
        + "\n".join(
            f"   python -m guard.cli bridge render-openclaw-bundle {name} --sandbox {sandbox_name} --workspace {workspace} --gateway-port {gateway_port}"
            for name, _ in active_rows
        )
    )
    typer.echo("")
    typer.echo(
        "3. Optional debug path: register the bridge in sandbox mcporter:\n"
        + "\n".join(
            f"   npx -y {_render_mcporter_add_command(name, row, gateway_port)}"
            for name, row in active_rows
        )
    )
    typer.echo("")
    typer.echo(
        "4. Optional debug check: verify mcporter sees the configured servers:\n"
        + "\n".join(
            "   npx -y mcporter list"
            for _, row in active_rows[:1]
        )
    )
    typer.echo("")
    typer.echo(
        f"5. Optional debug check: verify the specific bridged server schemas:\n"
        + "\n".join(
            f"   npx -y mcporter list {name} --schema"
            for name, row in active_rows
        )
    )
    typer.echo("")
    typer.echo(
        "   Note: for `host.openshell.internal` inside NemoClaw/OpenShell sandboxes,\n"
        "   keep the default proxy environment enabled. Do not force `NO_PROXY`\n"
        "   or `NODE_USE_ENV_PROXY=0` unless you have separately validated a direct path."
    )
    typer.echo("")
    typer.echo(
        "6. Optional direct bridge probe from inside the sandbox:\n"
        + "\n".join(
            f"   curl -sS http://{str(row.get('host_alias') or _default_bridge_host())}:{_bridge_port(row, gateway_port)}/mcp/{name}/"
            for name, row in active_rows
        )
    )
    typer.echo("")
    typer.echo(f"Configured bridge servers: {names}")


@bridge_app.command("verify-runtime")
def bridge_verify_runtime(
    name: str = typer.Argument(..., help="Bridge/MCP server name"),
    sandbox_name: str = typer.Option("my-assistant", "--sandbox", help="Target NemoClaw sandbox"),
    workspace: str = typer.Option(".", "--workspace", help="Project root used for bridge state"),
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
    gateway_port: int = typer.Option(8090, "--gateway-port", help="Default Guard/bridge port"),
):
    """Verify that the host-side bridge runtime is ready for sandbox mcporter registration."""
    row = bridge_state.get_bridge(workspace, sandbox_name, name)
    if not row:
        typer.secho(
            f"ERROR: no bridge record named {name!r} for sandbox {sandbox_name!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    server = _find_mcp_server(name, gateway_url)
    if not server:
        typer.secho(f"ERROR: MCP server {name!r} not found", fg=typer.colors.RED)
        raise typer.Exit(1)

    checks = [
        ("bridge record exists", True),
        ("bridge is active", row.get("status") == "active"),
        ("gateway MCP server exists", True),
        ("gateway MCP server approved", server.get("status") == "approved"),
        ("upstream URL present", bool(server.get("url"))),
    ]
    failed = False
    typer.echo(f"Bridge runtime verification for sandbox {sandbox_name!r}, server {name!r}:\n")
    for label, ok in checks:
        status = "OK" if ok else "MISSING"
        color = typer.colors.GREEN if ok else typer.colors.RED
        typer.secho(f"  {status:<8} {label}", fg=color)
        if not ok:
            failed = True
    typer.echo("")
    typer.echo(f"Bridge URL: {_bridge_url(row, gateway_port)}")
    typer.echo(f"Upstream:   {server.get('url')}")
    typer.echo(
        "Next step:  "
        f"python -m guard.cli bridge stage-openclaw-bundle {name} --sandbox {sandbox_name} "
        f"--workspace {workspace} --output-dir {Path(workspace).resolve() / 'sandbox_workspace' / 'openclaw-data' / 'extensions' / 'guard-mcp-bundle'}"
    )
    typer.echo(
        "Debug alt:  "
        f"{_render_mcporter_add_command(name, row, gateway_port)}"
    )
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
