"""Tests for guard.gateway_config and the blueprint→gateway migration script."""
import tempfile
import unittest
from pathlib import Path

import yaml

from guard import gateway_config
from guard.gateway_config import GatewayConfigError
from tools.migrate_blueprint_to_gateway import migrate


class NetworkEntriesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmpdir.name) / "gateway.yaml"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_defaults_round_trip(self):
        gateway_config.set_defaults(self.cfg, "deny", "warn")
        self.assertEqual(gateway_config.get_default(self.cfg, "install"), "deny")
        self.assertEqual(gateway_config.get_default(self.cfg, "runtime"), "warn")

    def test_invalid_default_rejected(self):
        with self.assertRaises(GatewayConfigError):
            gateway_config.set_default(self.cfg, "install", "nonsense")

    def test_invalid_scope_rejected(self):
        with self.assertRaises(GatewayConfigError):
            gateway_config.get_default(self.cfg, "bogus")

    def test_add_and_remove_entry(self):
        added = gateway_config.add_entry(
            self.cfg, scope="runtime", host="api.example.com",
            ports=[443], enforcement="enforce", purpose="testing",
        )
        self.assertTrue(added)
        entries = gateway_config.list_entries(self.cfg, "runtime")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].host, "api.example.com")
        self.assertEqual(entries[0].ports, [443])
        self.assertEqual(entries[0].enforcement, "enforce")

        # duplicate add returns False
        again = gateway_config.add_entry(self.cfg, scope="runtime", host="api.example.com")
        self.assertFalse(again)

        removed = gateway_config.remove_entry(self.cfg, "runtime", "api.example.com")
        self.assertTrue(removed)
        self.assertEqual(gateway_config.list_entries(self.cfg, "runtime"), [])

        # removing absent host returns False
        self.assertFalse(
            gateway_config.remove_entry(self.cfg, "runtime", "ghost.example.com")
        )

    def test_invalid_enforcement_rejected(self):
        with self.assertRaises(GatewayConfigError):
            gateway_config.add_entry(
                self.cfg, scope="runtime", host="api.example.com",
                enforcement="bogus",
            )


class McpRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmpdir.name) / "gateway.yaml"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_list_servers_empty_when_missing(self):
        self.assertEqual(gateway_config.list_servers(self.cfg), [])

    def test_register_and_find(self):
        srv = gateway_config.register_server(
            self.cfg, name="filesystem",
            url="https://mcp.example.com/sse",
            transport="sse", purpose="repo browsing",
        )
        self.assertEqual(srv.status, "pending")
        self.assertTrue(srv.registered_at)

        found = gateway_config.find_server(self.cfg, "filesystem")
        self.assertIsNotNone(found)
        self.assertEqual(found.url, "https://mcp.example.com/sse")

    def test_duplicate_registration_rejected(self):
        gateway_config.register_server(
            self.cfg, name="filesystem", url="https://x.example.com",
        )
        with self.assertRaises(GatewayConfigError):
            gateway_config.register_server(
                self.cfg, name="filesystem", url="https://y.example.com",
            )

    def test_invalid_name_rejected(self):
        with self.assertRaises(GatewayConfigError):
            gateway_config.register_server(
                self.cfg, name="has spaces", url="https://x.example.com",
            )

    def test_invalid_transport_rejected(self):
        with self.assertRaises(GatewayConfigError):
            gateway_config.register_server(
                self.cfg, name="filesystem", url="https://x.example.com",
                transport="websocket",
            )

    def test_status_transitions(self):
        gateway_config.register_server(
            self.cfg, name="filesystem", url="https://x.example.com",
        )
        approved = gateway_config.set_server_status(
            self.cfg, "filesystem", "approved", actor="alice",
        )
        self.assertEqual(approved.status, "approved")
        self.assertEqual(approved.approved_by, "alice")
        self.assertTrue(approved.approved_at)

        revoked = gateway_config.set_server_status(
            self.cfg, "filesystem", "revoked", actor="bob", reason="key leaked",
        )
        self.assertEqual(revoked.status, "revoked")
        self.assertEqual(revoked.denied_reason, "key leaked")

    def test_set_status_unknown_server(self):
        with self.assertRaises(GatewayConfigError):
            gateway_config.set_server_status(
                self.cfg, "ghost", "approved", actor="alice",
            )

    def test_set_status_invalid_value(self):
        gateway_config.register_server(
            self.cfg, name="filesystem", url="https://x.example.com",
        )
        with self.assertRaises(GatewayConfigError):
            gateway_config.set_server_status(
                self.cfg, "filesystem", "ghosted", actor="alice",
            )

    def test_remove_server(self):
        gateway_config.register_server(
            self.cfg, name="filesystem", url="https://x.example.com",
        )
        self.assertTrue(gateway_config.remove_server(self.cfg, "filesystem"))
        self.assertFalse(gateway_config.remove_server(self.cfg, "filesystem"))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.bp = self.root / "blueprint.yaml"
        self.gw = self.root / "gateway.yaml"
        self.bp.write_text(
            yaml.dump(
                {
                    "version": "0.1.0",
                    "components": {"sandbox": {"name": "openclaw"}},
                    "network": {
                        "install": {
                            "default": "deny",
                            "allow": [{"host": "github.com", "ports": [443]}],
                        },
                        "runtime": {
                            "default": "warn",
                            "allow": [{"host": "api.openai.com", "ports": [443]}],
                        },
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_migrate_moves_network_block(self):
        rc = migrate(self.bp, self.gw)
        self.assertEqual(rc, 0)
        self.assertTrue(self.gw.exists())

        bp_after = yaml.safe_load(self.bp.read_text(encoding="utf-8"))
        self.assertNotIn("network", bp_after)
        self.assertIn("components", bp_after)

        gw_after = yaml.safe_load(self.gw.read_text(encoding="utf-8"))
        self.assertEqual(gw_after["version"], 1)
        self.assertIn("network", gw_after)
        self.assertEqual(
            gw_after["network"]["install"]["allow"][0]["host"], "github.com",
        )

    def test_migrate_idempotent(self):
        self.assertEqual(migrate(self.bp, self.gw), 0)
        # Second run must refuse
        self.assertNotEqual(migrate(self.bp, self.gw), 0)


if __name__ == "__main__":
    unittest.main()
