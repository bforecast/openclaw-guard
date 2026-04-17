import unittest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner

from guard.cli import app, MCP_INSTALL_TEMPLATES
from guard import gateway_config


class CliMcpCommandTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    # ── mcp status ────────────────────────────────────────────────────

    def test_mcp_status_prints_server_details_and_stats(self):
        server = {
            "name": "github",
            "status": "approved",
            "transport": "streamable_http",
            "url": "https://mcp.example.test/github",
            "credential_env": "GITHUB_MCP_TOKEN",
            "purpose": "GitHub MCP",
            "registered_at": "2026-04-09T12:00:00Z",
            "approved_at": "2026-04-09T12:01:00Z",
            "approved_by": "alice",
        }
        events = [
            {
                "server_name": "github",
                "action": "call",
                "decision": "allow",
                "upstream_status": 200,
                "latency_ms": 42,
            },
            {
                "server_name": "github",
                "action": "call",
                "decision": "allow",
                "upstream_status": 200,
                "latency_ms": 58,
            },
            {
                "server_name": "github",
                "action": "call",
                "decision": "block",
                "upstream_status": None,
                "latency_ms": None,
            },
            {
                "server_name": "github",
                "action": "approve",
                "decision": "allow",
            },
        ]

        def fake_request(method, path, **kwargs):
            if path == "/v1/mcp/servers":
                return [server]
            if path == "/v1/mcp/events":
                return events
            return {}

        allowlist_entry = gateway_config.NetEntry(
            host="mcp.example.test", ports=[443], enforcement="enforce",
            purpose="GitHub MCP upstream", rpm=600,
        )

        with patch("guard.cli._gateway_admin_request", side_effect=fake_request), \
             patch("guard.cli._find_allowlist_entry", return_value=allowlist_entry):
            result = self.runner.invoke(app, ["mcp", "status", "github"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Name:           github", result.stdout)
        self.assertIn("Status:         approved", result.stdout)
        self.assertIn("Credential env: GITHUB_MCP_TOKEN", result.stdout)
        # allowlist details
        self.assertIn("Runtime allow:  yes", result.stdout)
        self.assertIn("ports=443", result.stdout)
        self.assertIn("enforcement=enforce", result.stdout)
        self.assertIn("rpm=600", result.stdout)
        self.assertIn("Allow purpose:  GitHub MCP upstream", result.stdout)
        # event stats
        self.assertIn("Event summary:", result.stdout)
        self.assertIn("Total calls:    3", result.stdout)
        self.assertIn("Allowed:        2", result.stdout)
        self.assertIn("Blocked:        1", result.stdout)
        self.assertIn("Avg latency:    50ms", result.stdout)
        # recent events still present
        self.assertIn("Recent events:", result.stdout)
        self.assertIn("call / allow (http=200, latency=42ms)", result.stdout)
        self.assertIn("guard mcp sync --workspace . --sandbox my-assistant", result.stdout)
        self.assertIn("openshell provider create --name github-mcp --type generic --credential GITHUB_MCP_TOKEN", result.stdout)

    def test_mcp_status_no_allowlist_entry(self):
        server = {
            "name": "custom",
            "status": "pending",
            "transport": "sse",
            "url": "https://custom.example.test/sse",
        }

        def fake_request(method, path, **kwargs):
            if path == "/v1/mcp/servers":
                return [server]
            if path == "/v1/mcp/events":
                return []
            return {}

        with patch("guard.cli._gateway_admin_request", side_effect=fake_request), \
             patch("guard.cli._find_allowlist_entry", return_value=None):
            result = self.runner.invoke(app, ["mcp", "status", "custom"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Runtime allow:  no", result.stdout)
        self.assertNotIn("Event summary:", result.stdout)

    # ── mcp install ───────────────────────────────────────────────────

    def test_mcp_install_registers_then_approves(self):
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "POST" and path == "/v1/mcp/servers":
                return {
                    "name": "github",
                    "status": "pending",
                    "transport": "streamable_http",
                    "url": "https://mcp.example.test/github",
                    "credential_env": "GITHUB_MCP_TOKEN",
                }
            if method == "POST" and path == "/v1/mcp/servers/github/approve":
                return {}
            return {}

        find_calls = []

        def fake_find(name, gateway_url):
            find_calls.append((name, gateway_url))
            if len(find_calls) == 1:
                return None
            return {
                "name": name,
                "status": "approved",
                "transport": "streamable_http",
                "url": "https://mcp.example.test/github",
                "credential_env": "GITHUB_MCP_TOKEN",
            }

        with patch("guard.cli._gateway_admin_request", side_effect=fake_request), patch(
            "guard.cli._find_mcp_server", side_effect=fake_find
        ):
            result = self.runner.invoke(
                app,
                [
                    "mcp",
                    "install",
                    "github",
                    "https://mcp.example.test/github",
                    "--transport",
                    "streamable_http",
                    "--credential-env",
                    "GITHUB_MCP_TOKEN",
                    "--by",
                    "alice",
                    "--no-sandbox-policy",
                ],
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("installed MCP server 'github' and approved it", result.stdout)
        self.assertEqual(calls[0][0:2], ("POST", "/v1/mcp/servers"))
        self.assertEqual(calls[1][0:2], ("POST", "/v1/mcp/servers/github/approve"))
        self.assertIn("guard mcp sync --workspace . --sandbox my-assistant", result.stdout)
        self.assertIn("openshell provider create --name github-mcp --type generic --credential GITHUB_MCP_TOKEN", result.stdout)

    def test_mcp_install_template_uses_defaults(self):
        """Install a known template without explicit URL or credential-env."""
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {}

        def fake_find(name, gateway_url):
            if len(calls) < 2:
                return None
            return {"name": name, "status": "approved", "transport": "sse",
                    "url": "https://mcp.linear.app/sse"}

        with patch("guard.cli._gateway_admin_request", side_effect=fake_request), \
             patch("guard.cli._find_mcp_server", side_effect=fake_find):
            result = self.runner.invoke(
                app,
                ["mcp", "install", "linear", "--by", "bob", "--no-sandbox-policy"],
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("installed MCP server 'linear'", result.stdout)
        self.assertIn("Template: linear", result.stdout)
        self.assertIn("guard mcp sync --workspace . --sandbox my-assistant", result.stdout)
        # Verify register body used template defaults
        register_body = calls[0][2]["json"]
        self.assertEqual(register_body["url"], "https://mcp.linear.app/sse")
        self.assertEqual(register_body["transport"], "sse")
        self.assertEqual(register_body["credential_env"], "LINEAR_MCP_TOKEN")

    def test_mcp_install_custom_public_server_allows_missing_credential_env(self):
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {}

        def fake_find(name, gateway_url):
            if len(calls) < 2:
                return None
            return {
                "name": name,
                "status": "approved",
                "transport": "streamable_http",
                "url": "https://earnings.example.test/mcp",
            }

        with patch("guard.cli._gateway_admin_request", side_effect=fake_request), \
             patch("guard.cli._find_mcp_server", side_effect=fake_find):
            result = self.runner.invoke(
                app,
                [
                    "mcp",
                    "install",
                    "earnings",
                    "https://earnings.example.test/mcp",
                    "--transport",
                    "streamable_http",
                    "--purpose",
                    "Public earnings MCP",
                    "--by",
                    "alice",
                    "--no-sandbox-policy",
                ],
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("installed MCP server 'earnings' and approved it", result.stdout)
        register_body = calls[0][2]["json"]
        self.assertEqual(register_body["url"], "https://earnings.example.test/mcp")
        self.assertEqual(register_body["transport"], "streamable_http")
        self.assertNotIn("credential_env", register_body)
        self.assertIn("No provider credential is required for this MCP server", result.stdout)

    def test_mcp_install_unknown_template_requires_url(self):
        """Unknown name without URL should fail with helpful message."""
        def fake_find(name, gateway_url):
            return None

        with patch("guard.cli._find_mcp_server", side_effect=fake_find):
            result = self.runner.invoke(
                app,
                ["mcp", "install", "unknown-mcp", "--credential-env", "TOK", "--by", "alice",
                 "--no-sandbox-policy"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no built-in install template", result.stdout)
        self.assertIn("Available templates:", result.stdout)

    # ── mcp uninstall ─────────────────────────────────────────────────

    def test_mcp_uninstall_removes_existing_server(self):
        calls = []

        def fake_find(name, gateway_url):
            return {"name": name, "status": "approved"}

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {}

        with patch("guard.cli._find_mcp_server", side_effect=fake_find), patch(
            "guard.cli._gateway_admin_request", side_effect=fake_request
        ):
            result = self.runner.invoke(app, ["mcp", "uninstall", "github",
                                              "--no-sandbox-policy"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("uninstalled MCP server 'github'", result.stdout)
        self.assertEqual(calls, [("DELETE", "/v1/mcp/servers/github", {"gateway_url": "http://127.0.0.1:8090"})])

    # ── mcp templates ─────────────────────────────────────────────────

    def test_mcp_templates_lists_all(self):
        result = self.runner.invoke(app, ["mcp", "templates"])
        self.assertEqual(result.exit_code, 0)
        for name in MCP_INSTALL_TEMPLATES:
            self.assertIn(name, result.stdout)
        self.assertIn("TRANSPORT", result.stdout)
        self.assertIn("CREDENTIAL_ENV", result.stdout)


    def test_mcp_sync_stages_host_side_config(self):
        approved_server = {
            "name": "github",
            "status": "approved",
            "transport": "streamable_http",
            "url": "https://api.githubcopilot.com/mcp/",
            "credential_env": "GITHUB_MCP_TOKEN",
        }
        staged = MagicMock()
        staged.immutable_openclaw_dir = "D:/ag-projects/guard/sandbox_workspace/openclaw"
        staged.policy_path = "D:/ag-projects/guard/policies/my-assistant.yaml"

        with patch("guard.cli._gateway_admin_request", return_value=[approved_server]), \
             patch("guard.cli.prepare_onboarding", return_value=staged):
            result = self.runner.invoke(app, ["mcp", "sync", "--sandbox", "my-assistant"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("staged host-side OpenClaw config", result.stdout)
        self.assertIn("Approved MCP servers for sandbox 'my-assistant':", result.stdout)
        self.assertIn("transport=streamable-http", result.stdout)
        self.assertIn("credential_env=GITHUB_MCP_TOKEN", result.stdout)
        self.assertIn(
            "Guard wrote the MCP registry into the host-side mapped OpenClaw config.",
            result.stdout,
        )
        self.assertIn(
            "Recreate the sandbox if it is already running so the refreshed config is mounted.",
            result.stdout,
        )
if __name__ == "__main__":
    unittest.main()
