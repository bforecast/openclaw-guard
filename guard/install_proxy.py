"""
install_proxy — minimal HTTP/HTTPS forward proxy that enforces the
`network.install` allowlist from blueprint.yaml during install scripts.

Behaviour
  * Listens on 127.0.0.1:8091 by default.
  * Handles two protocols:
      - HTTP CONNECT  (HTTPS tunnel) — checks host:port against the allowlist,
        then splices raw bytes between client and upstream. No TLS interception,
        no CA injection.
      - HTTP plain    — checks the absolute-form Request-URI host, forwards
        the raw request line + headers + body to the upstream and pipes the
        response back.
  * Every decision is recorded via NetworkMonitor.record(scope="install").
  * Designed to be wrapped by install_blueprint_*.sh and exported via
    http_proxy / https_proxy env vars so that curl, pip, npm and bash all
    flow through it transparently.

No new dependencies — pure stdlib + the existing NetworkMonitor.
"""

from __future__ import annotations

import argparse
import logging
import os
import select
import socket
import threading
import time
from pathlib import Path

from guard.network_monitor import (
    BLOCK,
    Decision,
    NetworkMonitor,
    get_default,
)

log = logging.getLogger("install-proxy")

CRLF = b"\r\n"
DEFAULT_PORT = 8091
BUFFER_SIZE = 65536
IDLE_TIMEOUT = 60.0


def _read_headers(sock: socket.socket) -> bytes:
    """Read until end-of-headers (CRLFCRLF). Returns raw bytes including the terminator."""
    data = b""
    sock.settimeout(IDLE_TIMEOUT)
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(BUFFER_SIZE)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:  # 64 KiB header cap
            break
    return data


def _parse_request_line(line: bytes) -> tuple[str, str, str]:
    parts = line.split(b" ")
    if len(parts) < 3:
        return ("", "", "")
    return (parts[0].decode("latin-1"), parts[1].decode("latin-1"), parts[2].decode("latin-1"))


def _parse_host_port(target: str, default_port: int) -> tuple[str, int]:
    # Strip scheme if present
    if "://" in target:
        target = target.split("://", 1)[1]
    # Strip path
    if "/" in target:
        target = target.split("/", 1)[0]
    if ":" in target:
        host, _, port_s = target.rpartition(":")
        try:
            return host, int(port_s)
        except ValueError:
            return target, default_port
    return target, default_port


def _send_status(sock: socket.socket, code: int, reason: str, body: bytes = b"") -> None:
    headers = (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Proxy-Agent: guard-install-proxy\r\n"
        "\r\n"
    ).encode("latin-1")
    try:
        sock.sendall(headers + body)
    except OSError:
        pass


def _splice(a: socket.socket, b: socket.socket) -> tuple[int, int]:
    """Bidirectional copy until either side closes. Returns (bytes_a_to_b, bytes_b_to_a)."""
    a.setblocking(False)
    b.setblocking(False)
    a_to_b = 0
    b_to_a = 0
    socks = [a, b]
    while True:
        try:
            r, _, x = select.select(socks, [], socks, IDLE_TIMEOUT)
        except (ValueError, OSError):
            break
        if x or not r:
            break
        closed = False
        for s in r:
            try:
                data = s.recv(BUFFER_SIZE)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                closed = True
                break
            if not data:
                closed = True
                break
            try:
                if s is a:
                    b.sendall(data)
                    a_to_b += len(data)
                else:
                    a.sendall(data)
                    b_to_a += len(data)
            except OSError:
                closed = True
                break
        if closed:
            break
    return a_to_b, b_to_a


class InstallProxy:
    def __init__(self, monitor: NetworkMonitor, host: str, port: int) -> None:
        self.monitor = monitor
        self.host = host
        self.port = port

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(64)
            log.info("install-proxy listening on %s:%d", self.host, self.port)
            while True:
                try:
                    client, addr = srv.accept()
                except KeyboardInterrupt:
                    log.info("install-proxy shutting down")
                    return
                except OSError:
                    continue
                t = threading.Thread(target=self._handle, args=(client, addr), daemon=True)
                t.start()

    # ── Connection handler ────────────────────────────────────────────────
    def _handle(self, client: socket.socket, addr: tuple) -> None:
        try:
            raw = _read_headers(client)
            if not raw:
                return
            head, _, rest = raw.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            if not lines:
                return
            method, target, _version = _parse_request_line(lines[0])
            if not method:
                _send_status(client, 400, "Bad Request")
                return
            if method.upper() == "CONNECT":
                host, port = _parse_host_port(target, 443)
                self._handle_connect(client, host, port)
            else:
                self._handle_plain(client, method, target, lines[1:], rest)
        except Exception as exc:
            log.warning("connection error from %s: %s", addr, exc)
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _handle_connect(self, client: socket.socket, host: str, port: int) -> None:
        decision = self.monitor.authorize(host, port, scope="install")
        if decision.verdict == BLOCK:
            log.warning("BLOCK CONNECT %s:%d (%s)", host, port, decision.reason)
            self.monitor.record(
                source="install_proxy", scope="install",
                host=host, port=port, decision=decision, method="CONNECT",
            )
            _send_status(client, 403, "Forbidden",
                         f"guard-install-proxy: {decision.reason}".encode())
            return
        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError as exc:
            log.warning("CONNECT %s:%d upstream failed: %s", host, port, exc)
            self.monitor.record(
                source="install_proxy", scope="install",
                host=host, port=port,
                decision=Decision("error", f"upstream connect failed: {exc}", "enforce"),
                method="CONNECT",
            )
            _send_status(client, 502, "Bad Gateway")
            return
        log.info("ALLOW CONNECT %s:%d (%s)", host, port, decision.verdict)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        start = time.monotonic()
        try:
            up_bytes, down_bytes = _splice(client, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass
        latency_ms = int((time.monotonic() - start) * 1000)
        self.monitor.record(
            source="install_proxy", scope="install",
            host=host, port=port, decision=decision, method="CONNECT",
            bytes_out=up_bytes, bytes_in=down_bytes, latency_ms=latency_ms,
        )

    def _handle_plain(
        self,
        client: socket.socket,
        method: str,
        target: str,
        header_lines: list[bytes],
        body_prefix: bytes,
    ) -> None:
        host = ""
        port = 80
        path = "/"
        # Absolute-form Request-URI used by HTTP proxies
        if "://" in target:
            after_scheme = target.split("://", 1)[1]
            if "/" in after_scheme:
                hostport, _, path_part = after_scheme.partition("/")
                path = "/" + path_part
            else:
                hostport = after_scheme
                path = "/"
            host, port = _parse_host_port(hostport, 80)
        else:
            path = target
            for ln in header_lines:
                if ln.lower().startswith(b"host:"):
                    host_val = ln[5:].decode("latin-1").strip()
                    host, port = _parse_host_port(host_val, 80)
                    break
        if not host:
            _send_status(client, 400, "Bad Request")
            return

        decision = self.monitor.authorize(host, port, scope="install")
        if decision.verdict == BLOCK:
            log.warning("BLOCK %s http://%s:%d%s (%s)", method, host, port, path, decision.reason)
            self.monitor.record(
                source="install_proxy", scope="install",
                host=host, port=port, decision=decision, method=method, path=path,
            )
            _send_status(client, 403, "Forbidden",
                         f"guard-install-proxy: {decision.reason}".encode())
            return

        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError as exc:
            log.warning("plain %s://%s:%d upstream failed: %s", method, host, port, exc)
            _send_status(client, 502, "Bad Gateway")
            return

        # Rewrite request line to origin-form
        new_request = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
        # Drop Proxy-* headers
        kept_headers: list[bytes] = []
        for ln in header_lines:
            lower = ln.lower()
            if lower.startswith(b"proxy-") or lower.startswith(b"connection:"):
                continue
            kept_headers.append(ln)
        kept_headers.append(b"Connection: close")
        upstream.sendall(new_request + b"\r\n".join(kept_headers) + b"\r\n\r\n" + body_prefix)
        start = time.monotonic()
        try:
            up_bytes, down_bytes = _splice(client, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass
        latency_ms = int((time.monotonic() - start) * 1000)
        self.monitor.record(
            source="install_proxy", scope="install",
            host=host, port=port, decision=decision,
            method=method, path=path,
            bytes_out=up_bytes, bytes_in=down_bytes, latency_ms=latency_ms,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenClaw Guard install-time network proxy")
    parser.add_argument("--blueprint", type=Path, default=None)
    parser.add_argument("--audit-db", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [install-proxy] %(message)s",
    )

    monitor = get_default(blueprint_path=args.blueprint, db_path=args.audit_db)
    summary = monitor.policy_summary()
    log.info("policy: install default=%s entries=%d enforce=%d",
             summary["install"]["default"], summary["install"]["entries"],
             summary["install"]["enforce"])
    proxy = InstallProxy(monitor, args.host, args.port)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
