"""End-to-end tests for guard.gateway MCP routes (admin + reverse proxy).

We spin up a tiny stdlib HTTP server to stand in for the upstream MCP server,
register/approve it through the gateway HTTP API, and verify the reverse proxy
honours status, network policy, and audit logging.
"""
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from guard import gateway, network_monitor as nm


class _EchoHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"upstream-ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):  # silence
        pass


def _start_echo() -> tuple[HTTPServer, int, threading.Thread]:
    srv = HTTPServer(("127.0.0.1", 0), _EchoHandler)
    port = srv.server_address[1]
    thr = threading.Thread(target=srv.serve_forever, daemon=True)
    thr.start()
    return srv, port, thr


ADMIN_TOKEN = "test-admin-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class McpProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.cfg = root / "gateway.yaml"
        self.db = root / "audit.db"

        # Seed gateway.yaml with empty network section so default monitor loads
        self.cfg.write_text(
            "version: 1\nnetwork:\n  install:\n    default: deny\n  runtime:\n    default: warn\n",
            encoding="utf-8",
        )

        # Echo upstream
        self.echo, self.echo_port, _ = _start_echo()

        # Reset and rewire gateway module-level state
        nm.reset_default()
        self.monitor = nm.NetworkMonitor(self.cfg, self.db)
        self._orig_cfg = gateway.GATEWAY_CONFIG_PATH
        self._orig_db = gateway.DB_PATH
        self._orig_monitor = gateway.network_monitor
        self._orig_token_env = (
            os.environ.get("GUARD_ADMIN_TOKEN"),
            os.environ.get("OPENCLAW_GATEWAY_TOKEN"),
        )
        gateway.GATEWAY_CONFIG_PATH = self.cfg
        gateway.DB_PATH = self.db
        gateway.network_monitor = self.monitor
        gateway._mcp_cache = {}
        os.environ["GUARD_ADMIN_TOKEN"] = ADMIN_TOKEN
        os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)

        gateway._init_db().close()
        self.client = TestClient(gateway.app)
        # TestClient context manager triggers startup; we manually invoke
        # _refresh_mcp_cache so the cache reflects our patched cfg.
        gateway._refresh_mcp_cache()

    def tearDown(self):
        try:
            self.echo.shutdown()
            self.echo.server_close()
        except Exception:
            pass
        gateway.GATEWAY_CONFIG_PATH = self._orig_cfg
        gateway.DB_PATH = self._orig_db
        gateway.network_monitor = self._orig_monitor
        old_admin, old_oc = self._orig_token_env
        if old_admin is None:
            os.environ.pop("GUARD_ADMIN_TOKEN", None)
        else:
            os.environ["GUARD_ADMIN_TOKEN"] = old_admin
        if old_oc is not None:
            os.environ["OPENCLAW_GATEWAY_TOKEN"] = old_oc
        nm.reset_default()
        self.tmpdir.cleanup()

    # ── helpers ────────────────────────────────────────────────────────────
    def _register(self, name="echo", purpose="test"):
        return self.client.post(
            "/v1/mcp/servers",
            headers=ADMIN_HEADERS,
            json={
                "name": name,
                "url": f"http://127.0.0.1:{self.echo_port}",
                "transport": "streamable_http",
                "purpose": purpose,
            },
        )

    def _approve(self, name="echo", auto_allow=True):
        return self.client.post(
            f"/v1/mcp/servers/{name}/approve",
            headers=ADMIN_HEADERS,
            json={"actor": "alice", "auto_allow": auto_allow},
        )

    def _events(self):
        conn = sqlite3.connect(str(self.db))
        rows = conn.execute(
            "SELECT server_name, action, decision FROM mcp_events ORDER BY id"
        ).fetchall()
        conn.close()
        return rows

    # ── tests ──────────────────────────────────────────────────────────────
    def test_register_creates_pending_server(self):
        resp = self._register()
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["status"], "pending")

        listed = self.client.get("/v1/mcp/servers", headers=ADMIN_HEADERS).json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "echo")

    def test_proxy_blocked_while_pending(self):
        self._register()
        resp = self.client.get("/mcp/echo/")
        self.assertEqual(resp.status_code, 403)

    def test_approve_then_proxy_succeeds(self):
        self._register()
        self._approve()
        resp = self.client.get("/mcp/echo/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "upstream-ok")

        actions = [r[1] for r in self._events()]
        self.assertIn("register", actions)
        self.assertIn("approve", actions)
        self.assertIn("call", actions)

    def test_revoke_blocks_subsequent_calls(self):
        self._register()
        self._approve()
        self.assertEqual(self.client.get("/mcp/echo/").status_code, 200)

        revoke = self.client.post(
            "/v1/mcp/servers/echo/revoke",
            headers=ADMIN_HEADERS,
            json={"actor": "alice", "reason": "rotation"},
        )
        self.assertEqual(revoke.status_code, 200)
        self.assertEqual(self.client.get("/mcp/echo/").status_code, 403)

    def test_duplicate_registration_returns_409(self):
        self.assertEqual(self._register().status_code, 201)
        self.assertEqual(self._register().status_code, 409)

    def test_unauthenticated_admin_call_returns_401(self):
        resp = self.client.get("/v1/mcp/servers")
        self.assertEqual(resp.status_code, 401)

    def test_auto_allow_adds_runtime_entry(self):
        self._register()
        self._approve(auto_allow=True)
        entries = [
            e.host for e in
            __import__("guard.gateway_config", fromlist=["list_entries"])
            .list_entries(self.cfg, "runtime")
        ]
        self.assertIn("127.0.0.1", entries)

    def test_remove_then_proxy_404(self):
        self._register()
        self._approve()
        rm = self.client.delete("/v1/mcp/servers/echo", headers=ADMIN_HEADERS)
        self.assertEqual(rm.status_code, 204)
        self.assertEqual(self.client.get("/mcp/echo/").status_code, 404)


if __name__ == "__main__":
    unittest.main()
