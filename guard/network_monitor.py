"""
NetworkMonitor — application-layer network authorization and audit.

Reads `network.install` and `network.runtime` sections from blueprint.yaml,
exposes `authorize(host, port, scope)` to other components (gateway upstream
calls, install proxy, eBPF capture), and persists every decision into the
shared `security_audit.db` SQLite database alongside the existing audit_log
table.

Design notes:
  * No new dependencies (stdlib + pyyaml, both already required by gateway).
  * Decisions are: ALLOW / WARN / MONITOR / BLOCK.
      - ALLOW    -> entry matched, enforcement=enforce.
      - WARN     -> entry matched OR default=warn, recorded with reason.
      - MONITOR  -> entry matched OR default=monitor, recorded silently.
      - BLOCK    -> default=deny and no entry matched, OR rate limit exceeded.
  * Sliding-window rate limit per host (rpm = requests/minute).
  * `record(...)` is best-effort: any DB error is logged but never raises so
    the request path is not impacted by audit failures.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("network-monitor")

ALLOW = "allow"
WARN = "warn"
MONITOR = "monitor"
BLOCK = "block"

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS network_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    pid INTEGER,
    host TEXT,
    port INTEGER,
    method TEXT,
    path TEXT,
    status INTEGER,
    bytes_in INTEGER,
    bytes_out INTEGER,
    latency_ms INTEGER,
    decision TEXT NOT NULL,
    reason TEXT
)
"""


@dataclass
class Decision:
    verdict: str        # ALLOW / WARN / MONITOR / BLOCK
    reason: str
    enforcement: str    # the configured enforcement that produced the verdict


@dataclass
class _Entry:
    host: str
    ports: tuple[int, ...]
    enforcement: str
    purpose: str
    rpm: int | None  # None = unlimited

    def matches(self, host: str, port: int) -> bool:
        if not _host_matches(self.host, host):
            return False
        if not self.ports:
            return True
        return port in self.ports


def _host_matches(pattern: str, host: str) -> bool:
    """Case-insensitive host match. Leading '*.' is treated as suffix wildcard."""
    pattern = pattern.lower()
    host = host.lower()
    if pattern.startswith("*."):
        return host == pattern[2:] or host.endswith(pattern[1:])
    return pattern == host


def _coerce_ports(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, int):
        return (value,)
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(item))
            except Exception:
                continue
        return tuple(out)
    return ()


def _parse_entries(raw: Any) -> list[_Entry]:
    if not isinstance(raw, list):
        return []
    out: list[_Entry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        host = item.get("host")
        if not isinstance(host, str) or not host:
            continue
        ports = _coerce_ports(item.get("ports") or item.get("port"))
        enforcement = str(item.get("enforcement", "")).strip().lower() or ""
        purpose = str(item.get("purpose", ""))
        rpm = None
        rate = item.get("rate_limit")
        if isinstance(rate, dict):
            try:
                rpm = int(rate.get("rpm")) if rate.get("rpm") is not None else None
            except Exception:
                rpm = None
        out.append(_Entry(host=host, ports=ports, enforcement=enforcement,
                          purpose=purpose, rpm=rpm))
    return out


def _normalize_default(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("deny", "block"):
        return BLOCK
    if text in ("warn",):
        return WARN
    if text in ("monitor",):
        return MONITOR
    if text in ("allow", "enforce"):
        return ALLOW
    return WARN


class NetworkMonitor:
    """Singleton-style monitor; instantiate once via `get_default(...)`."""

    def __init__(self, blueprint_path: Path | None, db_path: Path) -> None:
        self.blueprint_path = blueprint_path
        self.db_path = db_path
        self._lock = threading.Lock()
        self._install_entries: list[_Entry] = []
        self._runtime_entries: list[_Entry] = []
        self._install_default = BLOCK
        self._runtime_default = WARN
        self._rate_window: dict[str, deque] = defaultdict(deque)
        self._init_db()
        self.reload()

    # ── Persistence ────────────────────────────────────────────────────────
    def _init_db(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            conn.execute(_TABLE_DDL)
            conn.commit()
            conn.close()
        except Exception as exc:  # pragma: no cover - best-effort
            log.warning("network_events table init failed: %s", exc)

    def record(
        self,
        *,
        source: str,
        scope: str,
        host: str,
        port: int,
        decision: Decision,
        method: str = "",
        path: str = "",
        status: int | None = None,
        bytes_in: int | None = None,
        bytes_out: int | None = None,
        latency_ms: int | None = None,
        pid: int | None = None,
    ) -> None:
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute(
                "INSERT INTO network_events "
                "(timestamp, source, scope, pid, host, port, method, path, "
                " status, bytes_in, bytes_out, latency_ms, decision, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    source,
                    scope,
                    pid,
                    host,
                    port,
                    method,
                    path[:200] if path else "",
                    status,
                    bytes_in,
                    bytes_out,
                    latency_ms,
                    decision.verdict,
                    decision.reason[:300] if decision.reason else "",
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("network_events write failed: %s", exc)

    def recent_events(self, limit: int = 100) -> list[dict]:
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM network_events ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("network_events read failed: %s", exc)
            return []

    # ── Policy loading ─────────────────────────────────────────────────────
    def reload(self) -> None:
        if not self.blueprint_path or not self.blueprint_path.exists():
            log.info("No blueprint at %s; using defaults", self.blueprint_path)
            return
        try:
            data = yaml.safe_load(self.blueprint_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            log.warning("blueprint load failed: %s", exc)
            return
        net = data.get("network") or {}
        install = net.get("install") or {}
        runtime = net.get("runtime") or {}
        with self._lock:
            self._install_entries = _parse_entries(install.get("allow"))
            self._runtime_entries = _parse_entries(runtime.get("allow"))
            self._install_default = _normalize_default(install.get("default", "deny"))
            self._runtime_default = _normalize_default(runtime.get("default", "warn"))
            self._rate_window.clear()
        log.info(
            "network policy loaded: install(default=%s, %d entries) runtime(default=%s, %d entries)",
            self._install_default, len(self._install_entries),
            self._runtime_default, len(self._runtime_entries),
        )

    # ── Authorization ──────────────────────────────────────────────────────
    def authorize(self, host: str, port: int, scope: str = "runtime") -> Decision:
        with self._lock:
            if scope == "install":
                entries = self._install_entries
                default = self._install_default
            else:
                entries = self._runtime_entries
                default = self._runtime_default

            matched: _Entry | None = None
            for entry in entries:
                if entry.matches(host, port):
                    matched = entry
                    break

            if matched is None:
                if default == BLOCK:
                    return Decision(BLOCK, f"no entry for {host}:{port} (default=deny)", "deny")
                if default == WARN:
                    return Decision(WARN, f"no entry for {host}:{port} (default=warn)", "warn")
                if default == MONITOR:
                    return Decision(MONITOR, f"no entry for {host}:{port} (default=monitor)", "monitor")
                return Decision(ALLOW, "default=allow", "allow")

            # rate limit check (sliding 60s window)
            if matched.rpm:
                bucket = self._rate_window[matched.host.lower()]
                now = time.monotonic()
                cutoff = now - 60.0
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if len(bucket) >= matched.rpm:
                    return Decision(
                        BLOCK,
                        f"rate limit exceeded for {matched.host} ({matched.rpm} rpm)",
                        matched.enforcement or "enforce",
                    )
                bucket.append(now)

            enforcement = matched.enforcement or "enforce"
            if enforcement == "enforce":
                return Decision(ALLOW, matched.purpose or "allowed", enforcement)
            if enforcement == "warn":
                return Decision(WARN, matched.purpose or "warn-only", enforcement)
            if enforcement == "monitor":
                return Decision(MONITOR, matched.purpose or "monitor-only", enforcement)
            return Decision(ALLOW, matched.purpose or "allowed", enforcement)

    def get_scope_hosts(self, scope: str = "runtime") -> list[str]:
        """Return all hostnames declared in the given scope's allow list.
        Used by network_capture to build the DNS forward cache."""
        with self._lock:
            entries = self._runtime_entries if scope == "runtime" else self._install_entries
            return [e.host for e in entries]

    def policy_summary(self) -> dict:
        with self._lock:
            return {
                "install": {
                    "default": self._install_default,
                    "entries": len(self._install_entries),
                    "enforce": sum(
                        1 for e in self._install_entries
                        if (e.enforcement or "enforce") == "enforce"
                    ),
                },
                "runtime": {
                    "default": self._runtime_default,
                    "entries": len(self._runtime_entries),
                    "enforce": sum(
                        1 for e in self._runtime_entries
                        if (e.enforcement or "enforce") == "enforce"
                    ),
                },
            }


_DEFAULT: NetworkMonitor | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default(blueprint_path: Path | None = None, db_path: Path | None = None) -> NetworkMonitor:
    """Return process-wide singleton, creating it on first call."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            if db_path is None:
                db_path = Path(__file__).parent.parent / "logs" / "security_audit.db"
            if blueprint_path is None:
                blueprint_path = Path(__file__).parent.parent / "nemoclaw-blueprint" / "blueprint.yaml"
            _DEFAULT = NetworkMonitor(blueprint_path, db_path)
        return _DEFAULT


def reset_default() -> None:
    """Test-only helper to drop the singleton."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


__all__ = [
    "ALLOW",
    "WARN",
    "MONITOR",
    "BLOCK",
    "Decision",
    "NetworkMonitor",
    "get_default",
    "reset_default",
]
