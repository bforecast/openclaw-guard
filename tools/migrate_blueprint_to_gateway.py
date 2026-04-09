"""
One-shot migration: move ``network:`` from ``nemoclaw-blueprint/blueprint.yaml``
into a new ``gateway.yaml`` at the project root.

Why
---
``blueprint.yaml`` is the artefact NemoClaw consumes — it should only contain
fields the NemoClaw blueprint runner actually understands (sandbox, inference,
policy, mappings). Guard's ``network.install`` and ``network.runtime``
allowlists were stuffed under the same file for historical convenience but
NemoClaw never reads them, so they belong in a guard-owned config.

Behaviour
---------
* Refuses to run if ``gateway.yaml`` already exists (idempotent).
* Pops the ``network:`` block from ``blueprint.yaml``.
* Writes ``gateway.yaml`` containing ``{version: 1, network: <popped>}``.
* Saves the slimmed blueprint.yaml back in place.
* Preserves YAML key order (``sort_keys=False``).

Usage::

    python tools/migrate_blueprint_to_gateway.py            # default paths
    python tools/migrate_blueprint_to_gateway.py --dry-run  # print, don't write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def migrate(blueprint_path: Path, gateway_path: Path, *, dry_run: bool = False) -> int:
    if not blueprint_path.exists():
        print(f"ERROR: blueprint not found: {blueprint_path}", file=sys.stderr)
        return 2
    if gateway_path.exists():
        print(
            f"ERROR: {gateway_path} already exists — refusing to overwrite. "
            "Delete it first if you really want to re-run the migration.",
            file=sys.stderr,
        )
        return 2

    data = yaml.safe_load(blueprint_path.read_text(encoding="utf-8")) or {}
    network_block = data.pop("network", None)
    if network_block is None:
        print(f"INFO: no `network:` section in {blueprint_path} — nothing to migrate")
        return 0

    gateway_data = {"version": 1, "network": network_block}

    if dry_run:
        print(f"--- would write {gateway_path} ---")
        print(yaml.dump(gateway_data, sort_keys=False))
        print(f"--- would rewrite {blueprint_path} (network: removed) ---")
        return 0

    gateway_path.write_text(
        yaml.dump(gateway_data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    blueprint_path.write_text(
        yaml.dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"OK wrote {gateway_path}")
    print(f"OK rewrote {blueprint_path} (network: removed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blueprint",
        type=Path,
        default=project_root / "nemoclaw-blueprint" / "blueprint.yaml",
    )
    parser.add_argument(
        "--gateway",
        type=Path,
        default=project_root / "gateway.yaml",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return migrate(args.blueprint, args.gateway, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
