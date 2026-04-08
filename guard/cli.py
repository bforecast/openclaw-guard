import os
import subprocess
from pathlib import Path

import typer

from guard import blueprint_io
from guard.onboard import prepare_onboarding

app = typer.Typer(help="OpenClaw Guard - Unbypassable Security Gateway CLI")
net_app = typer.Typer(help="Manage blueprint network whitelist (install/runtime).")
app.add_typer(net_app, name="net")


def _blueprint_path() -> Path:
    """Resolve blueprint.yaml relative to this file's project root."""
    return Path(__file__).resolve().parent.parent / "nemoclaw-blueprint" / "blueprint.yaml"


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
    bp = _blueprint_path()
    try:
        default = blueprint_io.get_default(bp, scope)
        entries = blueprint_io.list_entries(bp, scope)
    except blueprint_io.BlueprintError as exc:
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
    bp = _blueprint_path()
    try:
        added = blueprint_io.add_entry(
            bp, scope=scope, host=host, ports=port,
            enforcement=enforcement, purpose=purpose, rpm=rpm,
        )
    except blueprint_io.BlueprintError as exc:
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
    bp = _blueprint_path()
    try:
        removed = blueprint_io.remove_entry(bp, scope=scope, host=host)
    except blueprint_io.BlueprintError as exc:
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
    """Hot-reload the gateway's network policy from blueprint.yaml.

    Note: this only refreshes gateway.py.  network_capture.py reloads its
    DNSForwardCache on its own TTL (default 300 s); restart its systemd unit
    if you need an immediate effect there:
        sudo systemctl restart guard-network-capture
    """
    token = os.environ.get("GUARD_ADMIN_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    _gateway_reload(gateway_url, token)


if __name__ == "__main__":
    app()
