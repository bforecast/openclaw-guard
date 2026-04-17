"""
sandbox_policy — Generate and apply OpenShell sandbox network presets for MCP servers.

When Guard approves an MCP server, the sandbox also needs an OpenShell network
policy entry so that outbound traffic to the MCP upstream host is allowed through
the OpenShell proxy.  This module:

1. Generates a NemoClaw-compatible preset YAML for an MCP server's upstream hosts.
2. Writes it to ``nemoclaw-blueprint/policies/presets/<name>.yaml``.
3. Applies the preset to a live sandbox via ``openshell policy set``.

The preset follows the exact same format as NemoClaw's built-in presets (slack,
brave, telegram, etc.), ensuring full compatibility with ``nemoclaw policy-add``.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml


# ---------------------------------------------------------------------------
# Preset generation
# ---------------------------------------------------------------------------

def _endpoint_block(host: str, *, access_full: bool = True) -> dict:
    """Build one OpenShell endpoint entry for a host on port 443.

    Defaults to ``access: full`` (CONNECT tunnel) because Node.js and
    OpenClaw's MCP client always use HTTPS CONNECT through the proxy.
    Set *access_full=False* for ``protocol: rest`` + TLS-termination mode
    (useful when the proxy must inspect request bodies).
    """
    if access_full:
        return {"host": host, "port": 443, "access": "full"}
    return {
        "host": host,
        "port": 443,
        "protocol": "rest",
        "enforcement": "enforce",
        "tls": "terminate",
        "rules": [
            {"allow": {"method": "GET", "path": "/**"}},
            {"allow": {"method": "POST", "path": "/**"}},
            {"allow": {"method": "DELETE", "path": "/**"}},
        ],
    }


_DEFAULT_BINARIES = [
    {"path": "/usr/local/bin/openclaw"},
    {"path": "/usr/local/bin/node"},
]


def generate_preset(
    name: str,
    *,
    description: str,
    hosts: list[str],
    binaries: list[dict] | None = None,
    access_full: bool = True,
) -> dict:
    """Return an OpenShell preset dict ready for YAML serialisation.

    Parameters
    ----------
    name:
        Preset slug (e.g. ``"github_mcp"``).  Must be a valid YAML key.
    description:
        Human-readable one-liner shown by ``nemoclaw policy-add``.
    hosts:
        Upstream hostnames the sandbox needs to reach (port 443 assumed).
    binaries:
        Optional list of ``{"path": ...}`` dicts.  Defaults to openclaw + node.
    access_full:
        If *True* (default), use ``access: full`` (CONNECT tunnel).
        Node.js and OpenClaw's MCP client use HTTPS CONNECT through the
        proxy, so this must be *True* for MCP endpoints.  Set to *False*
        for ``protocol: rest`` + TLS-termination mode when the proxy
        must inspect request bodies.
    """
    policy_name = f"mcp_{name}"
    return {
        "preset": {
            "name": policy_name,
            "description": description,
        },
        "network_policies": {
            policy_name: {
                "name": policy_name,
                "endpoints": [
                    _endpoint_block(h, access_full=access_full) for h in hosts
                ],
                "binaries": binaries or list(_DEFAULT_BINARIES),
            }
        },
    }


def hosts_from_url(url: str) -> list[str]:
    """Extract the hostname from an MCP upstream URL."""
    hostname = urlparse(url).hostname
    return [hostname] if hostname else []


# ---------------------------------------------------------------------------
# Preset file I/O
# ---------------------------------------------------------------------------

def _presets_dir() -> Path:
    """Return the NemoClaw presets directory (project-local)."""
    return Path(__file__).resolve().parent.parent / "nemoclaw-blueprint" / "policies" / "presets"


def _nemoclaw_presets_dir() -> Path | None:
    """Return the NemoClaw presets directory in ~/.nemoclaw/source (deployed).

    Returns *None* if the directory doesn't exist (e.g. NemoClaw not installed).
    """
    d = Path.home() / ".nemoclaw" / "source" / "nemoclaw-blueprint" / "policies" / "presets"
    return d if d.is_dir() else None


def write_preset_file(name: str, preset: dict) -> list[Path]:
    """Write preset YAML to both project-local and deployed NemoClaw dirs.

    Returns list of paths actually written.
    """
    filename = f"mcp-{name}.yaml"
    written: list[Path] = []

    for d in (_presets_dir(), _nemoclaw_presets_dir()):
        if d is None:
            continue
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        with path.open("w", encoding="utf-8") as f:
            yaml.dump(preset, f, default_flow_style=False, sort_keys=False)
        written.append(path)

    return written


def remove_preset_file(name: str) -> list[Path]:
    """Remove preset YAML from both project-local and deployed dirs.

    Returns list of paths actually removed.
    """
    filename = f"mcp-{name}.yaml"
    removed: list[Path] = []

    for d in (_presets_dir(), _nemoclaw_presets_dir()):
        if d is None:
            continue
        path = d / filename
        if path.exists():
            path.unlink()
            removed.append(path)

    return removed


# ---------------------------------------------------------------------------
# Live sandbox policy application
# ---------------------------------------------------------------------------

def _read_current_policy(sandbox_name: str) -> dict | None:
    """Try to read the current sandbox policy via ``openshell policy get``.

    Returns the parsed YAML dict, or *None* if we can't read it.
    """
    # openshell policy get only prints metadata, not the full YAML.
    # We'll read the base policy file and merge presets on top.
    return None


def _build_full_policy(
    base_policy_path: Path,
    extra_preset_names: list[str],
) -> dict:
    """Merge the base sandbox policy with additional MCP presets.

    Reads the base policy, then for each preset name looks up the preset
    YAML file and merges its ``network_policies`` into the base.
    """
    with base_policy_path.open("r", encoding="utf-8") as f:
        policy = yaml.safe_load(f)

    if "network_policies" not in policy:
        policy["network_policies"] = {}

    presets_dir = _presets_dir()
    for pname in extra_preset_names:
        preset_path = presets_dir / f"mcp-{pname}.yaml"
        if not preset_path.exists():
            continue
        with preset_path.open("r", encoding="utf-8") as f:
            preset_data = yaml.safe_load(f)
        np = preset_data.get("network_policies", {})
        policy["network_policies"].update(np)

    return policy


def _base_policy_path() -> Path:
    """Resolve the base sandbox policy YAML."""
    return (
        Path(__file__).resolve().parent.parent
        / "nemoclaw-blueprint"
        / "policies"
        / "openclaw-sandbox.yaml"
    )


def list_installed_mcp_presets() -> list[str]:
    """Return names of MCP presets currently written to the presets dir.

    Each preset file is named ``mcp-<name>.yaml``.  Returns the ``<name>`` parts.
    """
    d = _presets_dir()
    if not d.is_dir():
        return []
    return sorted(
        p.stem.removeprefix("mcp-")
        for p in d.glob("mcp-*.yaml")
    )


def apply_sandbox_policy(
    sandbox_name: str = "my-assistant",
    *,
    wait: bool = True,
) -> tuple[bool, str]:
    """Rebuild and apply the full sandbox policy (base + all MCP presets).

    Returns ``(success, message)``.
    """
    base = _base_policy_path()
    if not base.exists():
        return False, f"base policy not found: {base}"

    mcp_presets = list_installed_mcp_presets()
    policy = _build_full_policy(base, mcp_presets)

    # Write to a temp file and apply via openshell
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8",
    ) as tmp:
        yaml.dump(policy, tmp, default_flow_style=False, sort_keys=False)
        tmp_path = tmp.name

    cmd = ["openshell", "policy", "set", sandbox_name, "--policy", tmp_path]
    if wait:
        cmd.append("--wait")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return False, "openshell CLI not found; sandbox policy not applied"
    except subprocess.TimeoutExpired:
        return False, "openshell policy set timed out"
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if result.returncode == 0:
        presets_str = ", ".join(mcp_presets) if mcp_presets else "(none)"
        return True, f"sandbox policy applied with MCP presets: {presets_str}"
    else:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        return False, f"openshell policy set failed: {detail}"
