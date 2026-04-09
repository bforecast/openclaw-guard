"""
blueprint_io — read/write helpers for ``nemoclaw-blueprint/blueprint.yaml``.

Scope: ONLY fields the NemoClaw blueprint runner consumes. Anything that is
specific to the guard gateway (network allowlists, MCP registry) lives in
``guard/gateway_config.py`` and is persisted to ``gateway.yaml`` instead.

Currently the only mutator we need on this file is ``set_default_model``,
called by the setup wizard to patch
``components.inference.profiles.default.model``.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class BlueprintError(Exception):
    """Raised on missing/malformed blueprint or invalid arguments."""


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


__all__ = ["BlueprintError", "load", "save", "set_default_model"]
