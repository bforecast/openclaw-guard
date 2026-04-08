"""End-to-end tests for the install-time proxy.

We don't make real network calls — instead we point the proxy at a tiny
in-process echo server bound on 127.0.0.1, and verify allow/deny decisions
plus that audit rows are written.
"""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from guard import network_monitor as nm
from guard.install_proxy import InstallProxy


def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def _serve():
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                return
            try:
                client.recv(4096)
                client.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                )
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    threading.Thread(target=_serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class InstallProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.bp = root / "blueprint.yaml"
        self.db = root / "audit.db"
        # Echo server stands in for "github.com"
        self.echo, echo_port = _echo_server()
        self.echo_port = echo_port
        self.bp.write_text(
            f"""\
network:
  install:
    default: deny
    allow:
      - host: 127.0.0.1
        ports: [{echo_port}]
        purpose: test echo
""",
            encoding="utf-8",
        )
        nm.reset_default()
        self.monitor = nm.NetworkMonitor(self.bp, self.db)
        self.proxy_port = _free_port()
        self.proxy = InstallProxy(self.monitor, "127.0.0.1", self.proxy_port)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        time.sleep(0.1)

    def tearDown(self):
        try:
            self.echo.close()
        except OSError:
            pass
        self.tmpdir.cleanup()
        nm.reset_default()

    # ── helpers ────────────────────────────────────────────────────────────
    def _http_request(self, host: str, port: int, path: str = "/") -> tuple[int, bytes]:
        s = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5)
        try:
            req = (
                f"GET http://{host}:{port}{path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode()
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        try:
            status = int(head.split(b" ", 2)[1])
        except Exception:
            status = -1
        return status, body

    # ── tests ──────────────────────────────────────────────────────────────
    def test_allowed_host_passes_through(self):
        status, body = self._http_request("127.0.0.1", self.echo_port)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")

    def test_unlisted_host_blocked_with_403(self):
        # Use a port not in the allowlist
        status, body = self._http_request("127.0.0.1", 1)
        self.assertEqual(status, 403)
        self.assertIn(b"guard-install-proxy", body)

    def test_block_recorded_in_audit_db(self):
        self._http_request("127.0.0.1", 1)
        events = self.monitor.recent_events(limit=10)
        self.assertTrue(any(e["decision"] == nm.BLOCK for e in events))


if __name__ == "__main__":
    unittest.main()
