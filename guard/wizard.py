"""
OpenClaw Guard - Interactive Setup Wizard.

Reads API keys from .env, tests provider connectivity, lets the user
choose a provider then a model in two steps, and writes the result into:
  1. nemoclaw-blueprint/blueprint.yaml  (inference.profiles.default.model)
  2. gateway.yaml  (network.install.default and network.runtime.default)
  3. .env  (PROVIDER_ID=... and MODEL_ID=...)

Designed to run BEFORE gateway starts and BEFORE nemoclaw onboard.
Embed in install_blueprint_ec2.sh as:
    "$VENV_PYTHON" -m guard.wizard --project-dir "$PROJECT_DIR"
"""

import os
import re
import sys
from pathlib import Path

import httpx

from guard import blueprint_io, gateway_config

# ── Provider / Model catalogue ───────────────────────────────────────────────

PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "display": "OpenRouter (any model)",
        "default_models": [
            "nvidia/nemotron-3-super-120b-a12b:free",
            "openrouter/auto",
            "openrouter/google/gemini-2.5-pro-preview",
            "openrouter/deepseek/deepseek-chat-v3-0324:free",
        ],
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "display": "OpenAI",
        "default_models": [
            "gpt-4o",
            "gpt-4o-mini",
            "o3-mini",
        ],
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "extra_headers": {"anthropic-version": "2023-06-01"},
        "display": "Anthropic",
        "default_models": [
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-6",
        ],
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "display": "NVIDIA NIM",
        "default_models": [
            "nvidia/nemotron-3-super-120b-a12b",
            "moonshotai/kimi-k2.5",
            "z-ai/glm5",
            "minimaxai/minimax-m2.5",
            "openai/gpt-oss-120b",
        ],
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_dotenv(env_path: Path) -> dict[str, str]:
    """Minimal .env parser — returns {KEY: VALUE} for non-comment, non-empty lines."""
    result = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if value:
            result[key] = value
    return result


def detect_available(env_vars: dict[str, str]) -> dict[str, dict]:
    """Return providers whose API key is present and non-empty."""
    available = {}
    for name, cfg in PROVIDERS.items():
        key = env_vars.get(cfg["api_key_env"], "") or os.environ.get(cfg["api_key_env"], "")
        if key:
            available[name] = {**cfg, "_key": key}
    return available


def test_provider(name: str, cfg: dict) -> bool:
    """Quick connectivity check — hit /models (or /messages for anthropic)."""
    headers = {cfg["auth_header"]: f"{cfg['auth_prefix']}{cfg['_key']}"}
    headers.update(cfg.get("extra_headers", {}))
    try:
        if name == "anthropic":
            # Anthropic doesn't have /models; do a minimal messages call
            resp = httpx.post(
                f"{cfg['base_url']}/messages",
                headers={**headers, "content-type": "application/json"},
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=15,
            )
            return resp.status_code in (200, 201)
        else:
            resp = httpx.get(
                f"{cfg['base_url']}/models",
                headers=headers,
                timeout=15,
            )
            return resp.status_code == 200
    except Exception:
        return False


def update_blueprint(project_dir: Path, model_id: str) -> None:
    """Patch nemoclaw-blueprint/blueprint.yaml with chosen model."""
    bp_path = project_dir / "nemoclaw-blueprint" / "blueprint.yaml"
    try:
        blueprint_io.set_default_model(bp_path, model_id)
    except blueprint_io.BlueprintError as exc:
        print(f"  WARN {exc}")
        return
    print(f"  OK blueprint.yaml updated: model = {model_id}")


def update_network_policy(
    project_dir: Path,
    install_default: str,
    runtime_default: str,
) -> None:
    """Patch gateway.yaml `network.{install,runtime}.default` in place."""
    gw_path = project_dir / "gateway.yaml"
    try:
        gateway_config.set_defaults(gw_path, install_default, runtime_default)
    except gateway_config.GatewayConfigError as exc:
        print(f"  WARN {exc}")
        return
    print(f"  OK gateway.yaml updated: network.install.default={install_default}, "
          f"network.runtime.default={runtime_default}")


def update_dotenv(env_path: Path, provider_id: str, model_id: str) -> None:
    """Set PROVIDER_ID and MODEL_ID in .env (create or update)."""
    lines: list[str] = []
    found_provider = False
    found_model = False
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if re.match(r"^#?\s*PROVIDER_ID\s*=", line):
                lines.append(f"PROVIDER_ID={provider_id}")
                found_provider = True
            elif re.match(r"^#?\s*MODEL_ID\s*=", line):
                lines.append(f"MODEL_ID={model_id}")
                found_model = True
            else:
                lines.append(line)
    if not found_provider:
        lines.append(f"PROVIDER_ID={provider_id}")
    if not found_model:
        lines.append(f"MODEL_ID={model_id}")
    env_path.write_text("\n".join(lines) + "\n")
    print(f"  OK .env updated: PROVIDER_ID={provider_id}")
    print(f"  OK .env updated: MODEL_ID={model_id}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main(project_dir: Path, non_interactive: bool = False) -> str:
    """Run the setup wizard. Returns the chosen model ID."""
    env_path = project_dir / ".env"
    env_vars = load_dotenv(env_path)

    print("\n=== OpenClaw Guard: Model Setup ===\n")

    # Detect available providers
    available = detect_available(env_vars)
    if not available:
        print("ERROR: No API keys found in .env or environment.")
        print("       Please set at least one key in .env (see .env.example)")
        sys.exit(1)

    # Test connectivity
    print("Testing provider connectivity...\n")
    reachable: dict[str, dict] = {}
    for name, cfg in available.items():
        ok = test_provider(name, cfg)
        status = "OK reachable" if ok else "FAIL unreachable"
        print(f"  {name:12s}  {cfg['display']:30s}  {status}")
        if ok:
            reachable[name] = cfg

    if not reachable:
        print("\nERROR: All providers failed connectivity tests.")
        print("       Check your API keys and network access.")
        sys.exit(1)

    # ── Step 1: Select provider ────────────────────────────────────────────
    provider_list = list(reachable.items())

    if non_interactive:
        provider_name, provider_cfg = provider_list[0]
        print(f"Non-interactive mode: selecting provider [{1}] {provider_cfg['display']}")
    else:
        print("\nStep 1 — Select provider:\n")
        for idx, (pname, pcfg) in enumerate(provider_list, 1):
            marker = " *" if idx == 1 else ""
            print(f"  [{idx}] {pcfg['display']}{marker}")
        print(f"\n  * = default\n")

        raw = input("Select provider [1]: ").strip()
        if not raw:
            raw = "1"
        try:
            selection = int(raw)
        except ValueError:
            print("Invalid input.")
            sys.exit(1)
        if not (1 <= selection <= len(provider_list)):
            print("Selection out of range.")
            sys.exit(1)
        provider_name, provider_cfg = provider_list[selection - 1]

    # ── Step 2: Select model ─────────────────────────────────────────────
    models = provider_cfg["default_models"]

    if non_interactive:
        model_id = models[0]
        print(f"Non-interactive mode: selecting model [{1}] {model_id}")
    else:
        print(f"\nStep 2 — Select model for {provider_cfg['display']}:\n")
        for idx, model in enumerate(models, 1):
            marker = " *" if idx == 1 else ""
            print(f"  [{idx}] {model}{marker}")
        print(f"\n  [ 0] Enter a custom model ID")
        print(f"\n  * = default\n")

        raw = input("Select model [1]: ").strip()
        if not raw:
            raw = "1"
        try:
            selection = int(raw)
        except ValueError:
            print("Invalid input.")
            sys.exit(1)

        if selection == 0:
            model_id = input("Enter model ID: ").strip()
            if not model_id:
                print("Empty model ID.")
                sys.exit(1)
        elif 1 <= selection <= len(models):
            model_id = models[selection - 1]
        else:
            print("Selection out of range.")
            sys.exit(1)

    print(f"\n  Selected: {model_id} (provider: {provider_name})\n")

    # ── Step 3: Network policy ───────────────────────────────────────────
    install_default = "deny"
    runtime_default = "warn"
    if non_interactive:
        print(
            f"\nNon-interactive mode: install allowlist=strict (deny), "
            f"runtime monitoring=warn"
        )
    else:
        print("\nStep 3 — Network authorization policy:\n")
        print("  install allowlist enforces what install scripts can reach")
        print("  runtime monitor watches gateway upstream calls\n")
        raw = input("Use strict install allowlist (deny by default)? [Y/n]: ").strip().lower()
        if raw in ("n", "no"):
            install_default = "warn"
        raw = input("Runtime default for unlisted hosts [warn/monitor/deny] (warn): ").strip().lower()
        if raw in ("warn", "monitor", "deny"):
            runtime_default = raw

    # Write results
    print("\nWriting configuration...\n")
    update_blueprint(project_dir, model_id)
    update_network_policy(project_dir, install_default, runtime_default)
    update_dotenv(env_path, provider_name, model_id)

    print(f"\n=== Setup Complete ===")
    print(f"  Provider: {provider_name}")
    print(f"  Model:    {model_id}\n")

    return model_id


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OpenClaw Guard Model Setup")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root directory (default: auto-detect)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=None,
        help="Auto-select first available model without prompting",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=None,
        help="Force interactive mode even without TTY",
    )
    args = parser.parse_args()

    # Auto-detect: interactive if stdin is a TTY, otherwise non-interactive
    if args.interactive:
        non_interactive = False
    elif args.non_interactive:
        non_interactive = True
    else:
        non_interactive = not sys.stdin.isatty()
        if non_interactive:
            print("(no TTY detected, using non-interactive mode)")

    main(args.project_dir, non_interactive)
