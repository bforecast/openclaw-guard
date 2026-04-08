"""Unit tests for NetworkMonitor — policy decisions, rate limiting, audit IO."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from guard import network_monitor as nm

BLUEPRINT = """\
network:
  install:
    default: deny
    allow:
      - host: github.com
        ports: [443]
        purpose: source tarball
  runtime:
    default: warn
    allow:
      - host: api.openai.com
        ports: [443]
        enforcement: enforce
        rate_limit: { rpm: 2 }
      - host: openrouter.ai
        ports: [443]
        enforcement: monitor
"""


class _Tmp:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.bp = root / "blueprint.yaml"
        self.db = root / "audit.db"
        self.bp.write_text(BLUEPRINT, encoding="utf-8")

    def cleanup(self):
        self.dir.cleanup()


class NetworkMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _Tmp()
        nm.reset_default()
        self.monitor = nm.NetworkMonitor(self.tmp.bp, self.tmp.db)

    def tearDown(self):
        self.tmp.cleanup()
        nm.reset_default()

    def test_install_allowed_host(self):
        d = self.monitor.authorize("github.com", 443, scope="install")
        self.assertEqual(d.verdict, nm.ALLOW)

    def test_install_unknown_host_blocked_by_deny_default(self):
        d = self.monitor.authorize("evil.test", 443, scope="install")
        self.assertEqual(d.verdict, nm.BLOCK)
        self.assertIn("default=deny", d.reason)

    def test_runtime_unknown_host_warns(self):
        d = self.monitor.authorize("strange.example", 443, scope="runtime")
        self.assertEqual(d.verdict, nm.WARN)

    def test_runtime_enforced_host_allowed(self):
        d = self.monitor.authorize("api.openai.com", 443, scope="runtime")
        self.assertEqual(d.verdict, nm.ALLOW)

    def test_runtime_monitor_enforcement(self):
        d = self.monitor.authorize("openrouter.ai", 443, scope="runtime")
        self.assertEqual(d.verdict, nm.MONITOR)

    def test_rate_limit_blocks_when_exceeded(self):
        # rpm=2 → first two allowed, third blocked
        d1 = self.monitor.authorize("api.openai.com", 443)
        d2 = self.monitor.authorize("api.openai.com", 443)
        d3 = self.monitor.authorize("api.openai.com", 443)
        self.assertEqual(d1.verdict, nm.ALLOW)
        self.assertEqual(d2.verdict, nm.ALLOW)
        self.assertEqual(d3.verdict, nm.BLOCK)
        self.assertIn("rate limit", d3.reason)

    def test_record_persists_event(self):
        d = self.monitor.authorize("github.com", 443, scope="install")
        self.monitor.record(
            source="install_proxy", scope="install",
            host="github.com", port=443, decision=d, method="CONNECT",
        )
        rows = self.monitor.recent_events(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "github.com")
        self.assertEqual(rows[0]["decision"], nm.ALLOW)
        # network_events table physically exists
        conn = sqlite3.connect(str(self.tmp.db))
        cnt = conn.execute("SELECT COUNT(*) FROM network_events").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 1)

    def test_policy_summary_counts(self):
        s = self.monitor.policy_summary()
        self.assertEqual(s["install"]["default"], nm.BLOCK)
        self.assertEqual(s["install"]["entries"], 1)
        self.assertEqual(s["runtime"]["default"], nm.WARN)
        self.assertEqual(s["runtime"]["entries"], 2)


if __name__ == "__main__":
    unittest.main()
