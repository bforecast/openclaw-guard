import typer
import subprocess
import yaml
import os
import json
from pathlib import Path

app = typer.Typer(help="OpenClaw SECURE Runtime Management CLI (Powered by NVIDIA OpenShell)")

@app.command()
def start(
    workspace: str = typer.Option(..., help="Path to host workspace to mount"),
    sandbox_name: str = typer.Option("openclaw-sandbox", help="Name of the sandbox"),
    agent: str = typer.Option("openclaw", help="Agent to run (default: openclaw)"),
):
    """
    Start the OpenClaw Agent in a secure OpenShell Sandbox.
    """
    typer.echo(f"Starting {agent} securely attached to {workspace}...")
    
    workspace_path = Path(workspace).absolute()
    if not workspace_path.exists():
        typer.secho(f"Workspace path {workspace_path} does not exist!", fg=typer.colors.RED)
        raise typer.Exit(1)
        
    # 1. Ensure OpenShell Gateway is running
    typer.echo("Initializing OpenShell Gateway...")
    subprocess.run(["openshell", "gateway", "start"], check=False)
    
    # 2. Generate custom OpenShell Policy bridging the Host directory
    policy_dir = Path("policies")
    policy_dir.mkdir(exist_ok=True)
    policy_path = policy_dir / f"{sandbox_name}_policy.yaml"
    
    # This policy allows access to the mounted volume and local gateway
    policy_doc = {
        "version": "v1",
        "sandboxes": [
            {
                "name": sandbox_name,
                "filesystem": [
                    {
                        "path": "/workspace",
                        "host_path": str(workspace_path),
                        "access": "rw"
                    }
                ],
                "network": [
                    {
                        "domain": "host.docker.internal", # Access local FastAPI LLM proxy
                        "port": 8000,
                        "action": "allow"
                    },
                    {
                        "domain": "github.com", # Basic tooling
                        "action": "allow"
                    }
                ]
            }
        ]
    }
    
    with open(policy_path, "w") as f:
        yaml.dump(policy_doc, f)
        
    typer.echo(f"Generated strict policy at {policy_path}")
    
    # 3. Create Sandbox
    typer.echo("Provisioning the underlying Sandbox via OpenShell...")
    cmd = [
        "openshell", "sandbox", "create",
        "--name", sandbox_name,
        "--policy", str(policy_path),
        "--from", agent
    ]
    
    try:
        subprocess.run(cmd, check=True)
        typer.secho("Sandbox created successfully!", fg=typer.colors.GREEN)
    except subprocess.CalledProcessError:
        typer.secho("Failed to create Sandbox.", fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command()
def set_policy(
    sandbox_name: str = typer.Argument(..., help="Sandbox to apply policy to"),
    domain: str = typer.Option(..., help="Domain to whitelist"),
):
    """
    Dynamically update the network egress policy of a running sandbox.
    """
    typer.echo(f"Whitelisting {domain} for sandbox {sandbox_name}...")
    # Generate dynamic policy update using openshell native commands
    # In a fully fleshed out MVP, we would merge this with the existing yaml.
    
    cmd = [
        "openshell", "policy", "set", sandbox_name,
        "--allow-domain", domain
    ]
    subprocess.run(cmd, check=False)

@app.command()
def stop(sandbox_name: str = typer.Argument("openclaw-sandbox")):
    """
    Stop and destroy the secure Sandbox.
    """
    typer.echo(f"Destroying Sandbox {sandbox_name}...")
    subprocess.run(["openshell", "sandbox", "delete", sandbox_name], check=False)


if __name__ == "__main__":
    app()
