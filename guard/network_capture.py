"""
network_capture — kernel-layer egress observer for the gateway and the
OpenClaw sandbox container.

Two backends:
  1. eBPF (preferred, Linux only) — kprobes `tcp_v4_connect` and
     `tcp_v6_connect` via `bcc` (`apt install bpfcc-tools python3-bpfcc`),
     filters by PID, and emits one event per outbound connect.
  2. ss-polling (fallback) — periodically reads `ss -tnp -o state established`
     output, diffs against the previous snapshot, and emits new connections.

Both feed `NetworkMonitor.record(source="ebpf"|"ss", scope="runtime", ...)`,
correlating decisions with the runtime allow-list. The runtime default is
typically `warn`, so this captures evidence rather than blocking traffic.

Watched PIDs:
  * The current gateway.py PID (located via `pgrep -f "gateway.py"`).
  * The sandbox container's pid namespace root (via
    `docker inspect openclaw-sandbox --format '{{.State.Pid}}'`).

Designed to degrade safely on WSL / non-Linux:
  * Missing bcc           -> falls back to ss polling
  * Missing ss            -> sleeps forever (no-op) and logs a warning
  * Missing docker target -> only watches the gateway PID
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

from guard.network_monitor import (
    NetworkMonitor,
    get_default,
)

log = logging.getLogger("network-capture")

GATEWAY_PROC_HINT = "gateway.py"
SANDBOX_CONTAINER = "openclaw-sandbox"
SS_POLL_INTERVAL = 5.0


# ── PID discovery ──────────────────────────────────────────────────────────
def find_gateway_pids() -> list[int]:
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", GATEWAY_PROC_HINT],
            capture_output=True, text=True, check=False,
        )
        for line in out.stdout.split():
            try:
                pids.append(int(line))
            except ValueError:
                continue
    except FileNotFoundError:
        log.debug("pgrep not available; skipping gateway PID discovery")
    return pids


def find_sandbox_pid() -> int | None:
    try:
        out = subprocess.run(
            ["docker", "inspect", SANDBOX_CONTAINER, "--format", "{{.State.Pid}}"],
            capture_output=True, text=True, check=False,
        )
        text = out.stdout.strip()
        if text and text != "0":
            return int(text)
    except (FileNotFoundError, ValueError):
        log.debug("docker inspect unavailable; skipping sandbox PID discovery")
    return None


def discover_pids() -> set[int]:
    pids = set(find_gateway_pids())
    sb = find_sandbox_pid()
    if sb:
        pids.add(sb)
    return pids


# ── Hostname resolution cache (rDNS) ──────────────────────────────────────
class _HostCache:
    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def lookup(self, ip: str) -> str:
        if ip in self._cache:
            return self._cache[ip]
        try:
            host, *_ = socket.gethostbyaddr(ip)
        except OSError:
            host = ip
        self._cache[ip] = host
        return host


# ── DNS Forward Cache (whitelist hostname → IP set) ────────────────────────
class _DNSForwardCache:
    """Pre-resolves all blueprint whitelist hostnames to their current IP
    addresses, building an ip→canonical_host mapping.

    When eBPF captures a raw IP (e.g. 115.2.18.104 from a CDN), this cache
    translates it back to the declared hostname (e.g. openrouter.ai) so that
    NetworkMonitor.authorize() can find the correct whitelist entry instead of
    falling through to the default verdict.

    Refreshes every `ttl` seconds (default 300 s) to handle CDN IP rotation.
    A manual call to `refresh(force=True)` bypasses the TTL guard.
    """

    def __init__(self, monitor: NetworkMonitor, ttl: float = 300.0) -> None:
        self._monitor = monitor
        self._ttl = ttl
        self._ip_to_host: dict[str, str] = {}
        self._last_refresh: float = 0.0
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_refresh) < self._ttl:
            return

        hosts = self._monitor.get_scope_hosts("runtime")
        new_map: dict[str, str] = {}
        resolved = 0
        for host in hosts:
            if host.startswith("*."):
                continue  # wildcard — skip forward resolution
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET)
                for info in infos:
                    ip = info[4][0]
                    new_map[ip] = host
                    resolved += 1
                    log.debug("dns-fwd: %s -> %s", host, ip)
            except OSError as exc:
                log.debug("dns-fwd: %s unresolvable (%s)", host, exc)

        self._ip_to_host = new_map
        self._last_refresh = now
        log.info("dns-forward-cache refreshed: %d hostname(s) -> %d IP mapping(s)",
                 len(hosts), resolved)

    def translate(self, ip: str) -> str:
        """Return the canonical whitelist hostname for `ip`, or `ip` itself."""
        self.refresh()          # no-op if TTL not expired
        return self._ip_to_host.get(ip, ip)


# ── ss polling backend ─────────────────────────────────────────────────────
def _ss_snapshot() -> set[tuple[int, str, int]]:
    """Return {(pid, peer_ip, peer_port)} for ESTABLISHED tcp sockets."""
    out = subprocess.run(
        ["ss", "-tnp", "-o", "state", "established"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return set()
    snapshot: set[tuple[int, str, int]] = set()
    for line in out.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 5:
            continue
        peer = cols[3]
        if ":" not in peer:
            continue
        ip, _, port = peer.rpartition(":")
        ip = ip.strip("[]")
        try:
            port_n = int(port)
        except ValueError:
            continue
        pid = -1
        users_field = cols[-1] if cols[-1].startswith("users:") else ""
        if users_field:
            # Format: users:(("name",pid=1234,fd=5))
            for token in users_field.split("pid="):
                if token == "users:((":
                    continue
                num = ""
                for ch in token:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                if num:
                    pid = int(num)
                    break
        snapshot.add((pid, ip, port_n))
    return snapshot


def run_ss_polling(monitor: NetworkMonitor, watched_pids: set[int]) -> None:
    log.warning("eBPF unavailable, falling back to ss polling every %.1fs", SS_POLL_INTERVAL)
    rdns = _HostCache()
    dns_cache = _DNSForwardCache(monitor)
    previous: set[tuple[int, str, int]] = set()
    while True:
        try:
            snap = _ss_snapshot()
        except FileNotFoundError:
            log.error("ss not found; install iproute2 or bcc to enable network capture")
            return
        new = snap - previous
        previous = snap
        for pid, ip, port in new:
            if watched_pids and pid not in watched_pids and pid != -1:
                continue
            # Forward-DNS: translate captured IP to canonical whitelist hostname.
            # Falls back to reverse-DNS (or raw IP) when not in cache.
            host = dns_cache.translate(ip)
            if host == ip:
                host = rdns.lookup(ip)
            decision = monitor.authorize(host, port, scope="runtime")
            monitor.record(
                source="ss", scope="runtime",
                host=host, port=port, pid=pid if pid >= 0 else None,
                decision=decision, method="CONNECT",
            )
            log.info("ss-event pid=%s host=%s port=%d -> %s",
                     pid, host, port, decision.verdict)
        # Refresh PID list periodically
        watched_pids = discover_pids() or watched_pids
        time.sleep(SS_POLL_INTERVAL)


# ── eBPF backend ───────────────────────────────────────────────────────────
# Uses sock:inet_sock_set_state tracepoint (kernel 4.16+, stable on 6.x).
# Fires when TCP state changes; we capture TCP_SYN_SENT (= 2) for new outbound
# connections.  This avoids touching net/sock.h struct internals which changed
# on kernel 6.8+ and broke older kprobe-based programs.
_BPF_PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <uapi/linux/tcp.h>
#include <uapi/linux/in.h>

BPF_PERF_OUTPUT(events);

struct event_t {
    u32 pid;
    u32 saddr;
    u32 daddr;
    u16 sport;
    u16 dport;
    u16 family;
};

// tracepoint format: /sys/kernel/debug/tracing/events/sock/inet_sock_set_state/format
TRACEPOINT_PROBE(sock, inet_sock_set_state) {
    // Only care about new outbound SYN (TCP_SYN_SENT = 2)
    if (args->newstate != 2)
        return 0;
    // Only IPv4 (AF_INET = 2)
    if (args->family != 2)
        return 0;

    struct event_t e = {};
    e.pid  = bpf_get_current_pid_tgid() >> 32;
    // args->saddr / daddr are u8[4] arrays in newer kernels; read as u32
    bpf_probe_read_kernel(&e.saddr, sizeof(e.saddr), args->saddr);
    bpf_probe_read_kernel(&e.daddr, sizeof(e.daddr), args->daddr);
    e.sport  = args->sport;
    e.dport  = args->dport;
    e.family = args->family;
    events.perf_submit(args, &e, sizeof(e));
    return 0;
}
"""


def run_ebpf(monitor: NetworkMonitor, watched_pids: set[int]) -> None:
    try:
        from bcc import BPF  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        log.info("bcc import failed (%s); using ss polling backend", exc)
        return run_ss_polling(monitor, watched_pids)

    try:
        bpf = BPF(text=_BPF_PROGRAM)
    except Exception as exc:
        log.warning("BPF compile failed (%s); falling back to ss polling", exc)
        return run_ss_polling(monitor, watched_pids)

    rdns = _HostCache()
    dns_cache = _DNSForwardCache(monitor)

    def _emit(cpu, data, size):
        event = bpf["events"].event(data)
        if watched_pids and event.pid not in watched_pids:
            return
        try:
            # daddr is a __be32 (network byte order) read into a Python int by
            # BCC on a little-endian host.  to_bytes(4, "little") recovers the
            # original network-order bytes so inet_ntoa produces the correct IP.
            ip = socket.inet_ntoa(event.daddr.to_bytes(4, "little"))
        except Exception:
            return
        # Forward-DNS: translate captured IP to canonical whitelist hostname.
        # Falls back to reverse-DNS (or raw IP) when not in forward cache.
        host = dns_cache.translate(ip)
        if host == ip:
            host = rdns.lookup(ip)
        decision = monitor.authorize(host, event.dport, scope="runtime")
        monitor.record(
            source="ebpf", scope="runtime",
            host=host, port=event.dport, pid=event.pid,
            decision=decision, method="CONNECT",
        )
        log.info("ebpf pid=%d host=%s port=%d -> %s",
                 event.pid, host, event.dport, decision.verdict)

    bpf["events"].open_perf_buffer(_emit)
    log.info("ebpf attached (tracepoint sock:inet_sock_set_state), watching pids=%s",
             sorted(watched_pids) or "<all>")
    while True:
        try:
            bpf.perf_buffer_poll(timeout=1000)
            # Refresh PID list every 60 polls (~60s)
        except KeyboardInterrupt:
            return


# ── Entrypoint ─────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenClaw Guard kernel network capture daemon")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to gateway.yaml (guard-owned config)")
    parser.add_argument("--audit-db", type=Path, default=None)
    parser.add_argument("--backend", choices=("auto", "ebpf", "ss"), default="auto")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [network-capture] %(message)s",
    )

    if not sys.platform.startswith("linux"):
        log.warning("non-Linux platform (%s); kernel capture unavailable", sys.platform)
        # Sleep forever so systemd doesn't loop-restart us.
        while True:
            time.sleep(3600)

    monitor = get_default(blueprint_path=args.config, db_path=args.audit_db)
    pids = discover_pids()
    log.info("watched pids: %s", sorted(pids) if pids else "<none discovered yet>")

    if args.backend == "ss":
        run_ss_polling(monitor, pids)
    else:
        run_ebpf(monitor, pids)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
