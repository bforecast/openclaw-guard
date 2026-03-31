import os
import subprocess

import typer

from onboard import prepare_onboarding

app = typer.Typer(help="OpenClaw Guard - Unbypassable Security Gateway CLI")

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


if __name__ == "__main__":
    app()
