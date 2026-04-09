import os
import subprocess
from pathlib import Path

import typer

from guard import gateway_config
from guard.onboard import prepare_onboarding

app = typer.Typer(help="OpenClaw Guard - Unbypassable Security Gateway CLI")
net_app = typer.Typer(help="Manage gateway network whitelist (install/runtime).")
app.add_typer(net_app, name="net")
mcp_app = typer.Typer(help="Manage MCP servers via the guard gateway HTTP API.")
app.add_typer(mcp_app, name="mcp")


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


@mcp_app.command("list")
def mcp_list(
    gateway_url: str = typer.Option("http://127.0.0.1:8090", "--gateway"),
):
    """List all registered MCP servers (queries the gateway)."""
    data = _gateway_admin_request("GET", "/v1/mcp/servers", gateway_url=gateway_url)
    servers = data if isinstance(data, list) else data.get("servers", [])
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


if __name__ == "__main__":
    app()
