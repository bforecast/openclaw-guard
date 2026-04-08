"""
blueprint_io — pure YAML read/write helpers for nemoclaw-blueprint/blueprint.yaml.

Single source of truth for blueprint mutations. Both `guard.wizard` (install
wizard) and `guard.cli` (operational `guard net ...` subcommands) import
these functions so we never have two divergent YAML parsers in the codebase.

Conventions:
  * All functions take a `bp_path: Path` and operate on disk in place.
  * Reads/writes preserve key order via `sort_keys=False`.
  * Mutators return None on success and raise `BlueprintError` on
    structural problems (missing file, malformed YAML, unknown scope).
  * No printing — callers decide how to surface results.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

VALID_SCOPES = ("install", "runtime")
VALID_DEFAULTS = ("deny", "warn", "monitor", "allow")
VALID_ENFORCEMENTS = ("enforce", "warn", "monitor")


class BlueprintError(Exception):
    """Raised on missing/malformed blueprint or invalid arguments."""


@dataclass
class NetEntry:
    host: str
    ports: list[int]
    enforcement: str | None
    purpose: str
    rpm: int | None

    @classmethod
    def from_dict(cls, raw: dict) -> "NetEntry":
        ports_raw = raw.get("ports") or raw.get("port") or []
        if isinstance(ports_raw, int):
            ports_raw = [ports_raw]
        rate = raw.get("rate_limit") or {}
        rpm = rate.get("rpm") if isinstance(rate, dict) else None
        return cls(
            host=str(raw.get("host", "")),
            ports=[int(p) for p in ports_raw],
            enforcement=raw.get("enforcement"),
            purpose=str(raw.get("purpose", "")),
            rpm=int(rpm) if rpm is not None else None,
        )


# ── core read/write ─────────────────────────────────────────────────────────
def load(bp_path: Path) -> dict:
    if not bp_path.exists():
        raise BlueprintError(f"blueprint not found: {bp_path}")
    try:
        return yaml.safe_load(bp_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BlueprintError(f"blueprint parse error: {exc}") from exc


def save(bp_path: Path, data: dict) -> None:
    bp_path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ── inference profile (used by setup wizard) ────────────────────────────────
def set_default_model(bp_path: Path, model_id: str) -> None:
    """Patch components.inference.profiles.default.model."""
    data = load(bp_path)
    try:
        data["components"]["inference"]["profiles"]["default"]["model"] = model_id
    except (KeyError, TypeError) as exc:
        raise BlueprintError(
            "components.inference.profiles.default missing or malformed"
        ) from exc
    save(bp_path, data)


# ── network defaults ────────────────────────────────────────────────────────
def _check_scope(scope: str) -> None:
    if scope not in VALID_SCOPES:
        raise BlueprintError(f"invalid scope {scope!r}, expected one of {VALID_SCOPES}")


def set_default(bp_path: Path, scope: str, value: str) -> None:
    """Set network.{scope}.default."""
    _check_scope(scope)
    if value not in VALID_DEFAULTS:
        raise BlueprintError(
            f"invalid default {value!r}, expected one of {VALID_DEFAULTS}"
        )
    data = load(bp_path)
    net = data.setdefault("network", {})
    section = net.setdefault(scope, {})
    section["default"] = value
    save(bp_path, data)


def set_defaults(bp_path: Path, install_default: str, runtime_default: str) -> None:
    """Convenience used by setup wizard."""
    set_default(bp_path, "install", install_default)
    set_default(bp_path, "runtime", runtime_default)


# ── network entries ─────────────────────────────────────────────────────────
def list_entries(bp_path: Path, scope: str) -> list[NetEntry]:
    _check_scope(scope)
    data = load(bp_path)
    raw = (((data.get("network") or {}).get(scope) or {}).get("allow")) or []
    if not isinstance(raw, list):
        return []
    return [NetEntry.from_dict(item) for item in raw if isinstance(item, dict)]


def get_default(bp_path: Path, scope: str) -> str:
    _check_scope(scope)
    data = load(bp_path)
    section = (data.get("network") or {}).get(scope) or {}
    return str(section.get("default", "deny" if scope == "install" else "warn"))


def add_entry(
    bp_path: Path,
    scope: str,
    host: str,
    ports: list[int] | None = None,
    enforcement: str | None = None,
    purpose: str = "",
    rpm: int | None = None,
) -> bool:
    """Add a host entry. Returns True if added, False if (host,scope) already
    exists (caller can decide whether to update instead)."""
    _check_scope(scope)
    if not host:
        raise BlueprintError("host is required")
    if enforcement is not None and enforcement not in VALID_ENFORCEMENTS:
        raise BlueprintError(
            f"invalid enforcement {enforcement!r}, expected one of {VALID_ENFORCEMENTS}"
        )

    data = load(bp_path)
    net = data.setdefault("network", {})
    section = net.setdefault(scope, {})
    allow = section.setdefault("allow", [])
    if not isinstance(allow, list):
        raise BlueprintError(f"network.{scope}.allow is not a list")

    for item in allow:
        if isinstance(item, dict) and item.get("host") == host:
            return False  # already present

    entry: dict[str, Any] = {"host": host}
    if ports:
        entry["ports"] = list(ports)
    if enforcement:
        entry["enforcement"] = enforcement
    if purpose:
        entry["purpose"] = purpose
    if rpm is not None:
        entry["rate_limit"] = {"rpm": int(rpm)}
    allow.append(entry)
    save(bp_path, data)
    return True


def remove_entry(bp_path: Path, scope: str, host: str) -> bool:
    """Remove an entry by host. Returns True if removed, False if not found."""
    _check_scope(scope)
    data = load(bp_path)
    section = (data.get("network") or {}).get(scope) or {}
    allow = section.get("allow")
    if not isinstance(allow, list):
        return False
    new_allow = [
        item for item in allow
        if not (isinstance(item, dict) and item.get("host") == host)
    ]
    if len(new_allow) == len(allow):
        return False
    section["allow"] = new_allow
    save(bp_path, data)
    return True


__all__ = [
    "BlueprintError",
    "NetEntry",
    "VALID_SCOPES",
    "VALID_DEFAULTS",
    "VALID_ENFORCEMENTS",
    "load",
    "save",
    "set_default_model",
    "set_default",
    "set_defaults",
    "list_entries",
    "get_default",
    "add_entry",
    "remove_entry",
]
