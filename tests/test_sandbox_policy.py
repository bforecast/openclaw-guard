import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import yaml

from guard import sandbox_policy


class TestGeneratePreset(unittest.TestCase):
    def test_basic_preset_structure(self):
        """Default mode is access: full (CONNECT tunnel)."""
        preset = sandbox_policy.generate_preset(
            "github",
            description="MCP github upstream access",
            hosts=["api.githubcopilot.com", "api.github.com"],
        )
        self.assertEqual(preset["preset"]["name"], "mcp_github")
        self.assertEqual(preset["preset"]["description"], "MCP github upstream access")
        np = preset["network_policies"]["mcp_github"]
        self.assertEqual(np["name"], "mcp_github")
        self.assertEqual(len(np["endpoints"]), 2)
        self.assertEqual(np["endpoints"][0]["host"], "api.githubcopilot.com")
        self.assertEqual(np["endpoints"][0]["port"], 443)
        self.assertEqual(np["endpoints"][0]["access"], "full")
        self.assertNotIn("protocol", np["endpoints"][0])
        # default binaries
        self.assertEqual(len(np["binaries"]), 2)
        self.assertEqual(np["binaries"][0]["path"], "/usr/local/bin/openclaw")

    def test_rest_tls_mode(self):
        """access_full=False uses protocol: rest + tls: terminate."""
        preset = sandbox_policy.generate_preset(
            "ws-server",
            description="REST MCP",
            hosts=["mcp.example.com"],
            access_full=False,
        )
        ep = preset["network_policies"]["mcp_ws-server"]["endpoints"][0]
        self.assertEqual(ep["protocol"], "rest")
        self.assertEqual(ep["tls"], "terminate")
        self.assertEqual(len(ep["rules"]), 3)

    def test_access_full_mode(self):
        preset = sandbox_policy.generate_preset(
            "ws-server",
            description="WebSocket MCP",
            hosts=["mcp.example.com"],
            access_full=True,
        )
        ep = preset["network_policies"]["mcp_ws-server"]["endpoints"][0]
        self.assertEqual(ep["access"], "full")
        self.assertNotIn("protocol", ep)
        self.assertNotIn("rules", ep)

    def test_custom_binaries(self):
        bins = [{"path": "/usr/bin/python3"}]
        preset = sandbox_policy.generate_preset(
            "custom",
            description="Custom MCP",
            hosts=["custom.example.com"],
            binaries=bins,
        )
        self.assertEqual(
            preset["network_policies"]["mcp_custom"]["binaries"],
            bins,
        )

    def test_yaml_round_trip(self):
        preset = sandbox_policy.generate_preset(
            "earnings",
            description="Earnings MCP",
            hosts=["earnings-mcp-server.brilliantforecast.workers.dev"],
        )
        dumped = yaml.dump(preset, default_flow_style=False, sort_keys=False)
        loaded = yaml.safe_load(dumped)
        self.assertEqual(loaded, preset)


class TestHostsFromUrl(unittest.TestCase):
    def test_https_url(self):
        self.assertEqual(
            sandbox_policy.hosts_from_url("https://api.githubcopilot.com/mcp/"),
            ["api.githubcopilot.com"],
        )

    def test_sse_url(self):
        self.assertEqual(
            sandbox_policy.hosts_from_url("https://mcp.linear.app/sse"),
            ["mcp.linear.app"],
        )

    def test_empty_url(self):
        self.assertEqual(sandbox_policy.hosts_from_url(""), [])

    def test_no_scheme(self):
        self.assertEqual(sandbox_policy.hosts_from_url("not-a-url"), [])


class TestPresetFileIO(unittest.TestCase):
    def test_write_and_remove_preset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "presets"
            with patch.object(sandbox_policy, "_presets_dir", return_value=tmp_path), \
                 patch.object(sandbox_policy, "_nemoclaw_presets_dir", return_value=None):
                preset = sandbox_policy.generate_preset(
                    "test-mcp",
                    description="Test MCP",
                    hosts=["mcp.test.dev"],
                )

                written = sandbox_policy.write_preset_file("test-mcp", preset)
                self.assertEqual(len(written), 1)
                self.assertTrue(written[0].exists())

                # Verify file content
                with written[0].open() as f:
                    loaded = yaml.safe_load(f)
                self.assertEqual(loaded["preset"]["name"], "mcp_test-mcp")

                # Remove
                removed = sandbox_policy.remove_preset_file("test-mcp")
                self.assertEqual(len(removed), 1)
                self.assertFalse(removed[0].exists())

    def test_list_installed_mcp_presets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "mcp-github.yaml").touch()
            (tmp_path / "mcp-earnings.yaml").touch()
            (tmp_path / "slack.yaml").touch()  # not mcp- prefix
            with patch.object(sandbox_policy, "_presets_dir", return_value=tmp_path):
                presets = sandbox_policy.list_installed_mcp_presets()
                self.assertEqual(presets, ["earnings", "github"])


class TestBuildFullPolicy(unittest.TestCase):
    def test_merges_mcp_presets_into_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            # Write base policy
            base_policy = {
                "version": 1,
                "network_policies": {
                    "claude_code": {"name": "claude_code", "endpoints": []},
                },
            }
            base_path = tmpdir / "base.yaml"
            with base_path.open("w") as f:
                yaml.dump(base_policy, f)

            # Write preset
            presets_dir = tmpdir / "presets"
            presets_dir.mkdir()
            preset = sandbox_policy.generate_preset(
                "github", description="GitHub MCP", hosts=["api.githubcopilot.com"],
            )
            with (presets_dir / "mcp-github.yaml").open("w") as f:
                yaml.dump(preset, f)

            with patch.object(sandbox_policy, "_presets_dir", return_value=presets_dir):
                merged = sandbox_policy._build_full_policy(base_path, ["github"])

            self.assertIn("claude_code", merged["network_policies"])
            self.assertIn("mcp_github", merged["network_policies"])
            ep = merged["network_policies"]["mcp_github"]["endpoints"][0]
            self.assertEqual(ep["host"], "api.githubcopilot.com")


class TestApplySandboxPolicy(unittest.TestCase):
    def test_applies_via_openshell(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            base_path = tmpdir / "base.yaml"
            with base_path.open("w") as f:
                yaml.dump({"version": 1, "network_policies": {}}, f)

            mock_result = MagicMock()
            mock_result.returncode = 0

            with patch.object(sandbox_policy, "_base_policy_path", return_value=base_path), \
                 patch.object(sandbox_policy, "_presets_dir", return_value=tmpdir / "presets"), \
                 patch("subprocess.run", return_value=mock_result) as mock_run:
                (tmpdir / "presets").mkdir()
                ok, msg = sandbox_policy.apply_sandbox_policy("test-sandbox")

            self.assertTrue(ok)
            self.assertIn("sandbox policy applied", msg)
            # Verify openshell was called with correct args
            call_args = mock_run.call_args[0][0]
            self.assertEqual(call_args[0], "openshell")
            self.assertEqual(call_args[1], "policy")
            self.assertEqual(call_args[2], "set")
            self.assertEqual(call_args[3], "test-sandbox")

    def test_missing_openshell(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "base.yaml"
            with base_path.open("w") as f:
                yaml.dump({"version": 1, "network_policies": {}}, f)

            with patch.object(sandbox_policy, "_base_policy_path", return_value=base_path), \
                 patch.object(sandbox_policy, "_presets_dir", return_value=Path(tmpdir) / "presets"), \
                 patch("subprocess.run", side_effect=FileNotFoundError):
                (Path(tmpdir) / "presets").mkdir()
                ok, msg = sandbox_policy.apply_sandbox_policy("test-sandbox")

            self.assertFalse(ok)
            self.assertIn("openshell CLI not found", msg)


class TestBuildMcpServersConfig(unittest.TestCase):
    """Tests for guard.onboard._build_mcp_servers_config()."""

    def test_reads_approved_servers_with_placeholder_secret_refs(self):
        from guard.onboard import _build_mcp_servers_config

        with tempfile.TemporaryDirectory() as tmpdir:
            gw_path = Path(tmpdir) / "gateway.yaml"
            gw_data = {
                "mcp": {
                    "servers": [
                        {
                            "name": "github",
                            "url": "https://api.githubcopilot.com/mcp/",
                            "transport": "streamable_http",
                            "credential_env": "GITHUB_MCP_TOKEN",
                            "status": "approved",
                        },
                        {
                            "name": "earnings",
                            "url": "https://earnings.example.com/mcp",
                            "transport": "streamable_http",
                            "status": "approved",
                        },
                        {
                            "name": "denied-one",
                            "url": "https://denied.example.com/mcp",
                            "status": "denied",
                        },
                    ]
                }
            }
            with gw_path.open("w") as f:
                yaml.dump(gw_data, f)

            result = _build_mcp_servers_config(Path(tmpdir))

        # approved servers included
        self.assertIn("github", result)
        self.assertIn("earnings", result)
        # denied server excluded
        self.assertNotIn("denied-one", result)
        # github uses an OpenShell placeholder ref instead of a resolved token
        self.assertEqual(
            result["github"]["headers"]["Authorization"],
            "Bearer openshell:resolve:env:GITHUB_MCP_TOKEN",
        )
        # earnings has no credential_env, so no headers
        self.assertNotIn("headers", result["earnings"])
        # both have type, transport, and url
        self.assertEqual(result["github"]["type"], "http")
        self.assertEqual(result["github"]["transport"], "streamable-http")
        self.assertEqual(result["earnings"]["url"], "https://earnings.example.com/mcp")
        self.assertEqual(result["earnings"]["transport"], "streamable-http")

    def test_missing_gateway_yaml(self):
        from guard.onboard import _build_mcp_servers_config

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _build_mcp_servers_config(Path(tmpdir))
        self.assertEqual(result, {})

    def test_placeholder_header_is_present_without_env_resolution(self):
        from guard.onboard import _build_mcp_servers_config

        with tempfile.TemporaryDirectory() as tmpdir:
            gw_path = Path(tmpdir) / "gateway.yaml"
            gw_data = {
                "mcp": {
                    "servers": [
                        {
                            "name": "github",
                            "url": "https://api.githubcopilot.com/mcp/",
                            "credential_env": "GITHUB_MCP_TOKEN",
                            "status": "approved",
                        },
                    ]
                }
            }
            with gw_path.open("w") as f:
                yaml.dump(gw_data, f)

            result = _build_mcp_servers_config(Path(tmpdir))

        self.assertIn("github", result)
        self.assertEqual(
            result["github"]["headers"]["Authorization"],
            "Bearer openshell:resolve:env:GITHUB_MCP_TOKEN",
        )


class TestWriteOpenclawConfigWithMcp(unittest.TestCase):
    def test_mcp_servers_included_in_output(self):
        from guard.onboard import _write_openclaw_config

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "openclaw.json"
            mcp = {
                "github": {
                    "type": "http",
                    "transport": "streamable-http",
                    "url": "https://api.githubcopilot.com/mcp/",
                    "headers": {"Authorization": "Bearer openshell:resolve:env:GITHUB_MCP_TOKEN"},
                },
            }
            _write_openclaw_config(out, "dummy-token", mcp_servers=mcp)

            import json
            with out.open() as f:
                data = json.load(f)

            self.assertIn("mcp", data)
            self.assertIn("servers", data["mcp"])
            self.assertIn("github", data["mcp"]["servers"])
            self.assertEqual(
                data["mcp"]["servers"]["github"]["headers"]["Authorization"],
                "Bearer openshell:resolve:env:GITHUB_MCP_TOKEN",
            )
            self.assertEqual(
                data["mcp"]["servers"]["github"]["transport"],
                "streamable-http",
            )
            # gateway config still present
            self.assertIn("gateway", data)
            self.assertEqual(data["gateway"]["auth"]["token"], "dummy-token")
            # allowPrivateNetwork + apiKey on all providers
            for pname, provider in data["models"]["providers"].items():
                self.assertTrue(
                    provider.get("request", {}).get("allowPrivateNetwork"),
                    f"allowPrivateNetwork missing on {pname}",
                )
                self.assertEqual(
                    provider.get("apiKey"), "guard-managed",
                    f"apiKey missing on {pname}",
                )

    def test_no_mcp_omits_key(self):
        from guard.onboard import _write_openclaw_config

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "openclaw.json"
            _write_openclaw_config(out, "dummy-token")

            import json
            with out.open() as f:
                data = json.load(f)

            self.assertNotIn("mcp", data)
            # allowPrivateNetwork + apiKey still present even without MCP
            for pname, provider in data["models"]["providers"].items():
                self.assertTrue(
                    provider.get("request", {}).get("allowPrivateNetwork"),
                )
                self.assertEqual(provider.get("apiKey"), "guard-managed")


class TestOnboardPolicy(unittest.TestCase):
    def test_write_policy_includes_guard_bridge_rule(self):
        from guard.onboard import _write_policy

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "openclaw-sandbox.yaml"
            _write_policy(out, bridge_host="bridge.example.com", gateway_port=18090)

            data = yaml.safe_load(out.read_text(encoding="utf-8"))

        self.assertIn("guard_bridge_host", data["network_policies"])
        endpoint = data["network_policies"]["guard_bridge_host"]["endpoints"][0]
        self.assertEqual(endpoint["host"], "bridge.example.com")
        self.assertEqual(endpoint["port"], 18090)
        self.assertNotIn("allowed_ips", endpoint)

    def test_write_policy_uses_env_override_allowed_ips_for_bridge(self):
        from guard.onboard import _write_policy

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"GUARD_BRIDGE_ALLOWED_IPS": "172.17.0.1,127.0.0.1"},
            clear=False,
        ):
            out = Path(tmpdir) / "openclaw-sandbox.yaml"
            _write_policy(out, bridge_host="host.openshell.internal", gateway_port=8090)
            data = yaml.safe_load(out.read_text(encoding="utf-8"))

        endpoint = data["network_policies"]["guard_bridge_host"]["endpoints"][0]
        self.assertEqual(endpoint["allowed_ips"], ["172.17.0.1"])

    def test_project_network_policies_adds_allowed_ips_for_private_ip_host(self):
        from guard.onboard import _project_network_policies

        policies = _project_network_policies(
            [
                {
                    "host": "172.31.12.246",
                    "ports": [8090],
                    "purpose": "Guard bridge",
                }
            ]
        )

        endpoint = policies["172_31_12_246"]["endpoints"][0]
        self.assertEqual(endpoint["allowed_ips"], ["172.31.12.246"])

    def test_bridge_allowed_ips_env_override_does_not_leak_to_runtime_allow_entries(self):
        from guard.onboard import _project_network_policies

        with patch.dict(
            os.environ,
            {"GUARD_BRIDGE_ALLOWED_IPS": "172.17.0.1"},
            clear=False,
        ):
            policies = _project_network_policies(
                [
                    {
                        "host": "172.31.12.246",
                        "ports": [8090],
                        "purpose": "Guard bridge",
                    }
                ]
            )

        endpoint = policies["172_31_12_246"]["endpoints"][0]
        self.assertEqual(endpoint["allowed_ips"], ["172.31.12.246"])

    def test_project_network_policies_skips_bridged_mcp_hosts(self):
        """MCP upstreams reached via the gateway bridge must not be mirrored
        into the sandbox network policy (the sandbox only talks to
        host.openshell.internal:8090 for those).
        """
        from guard.onboard import _project_network_policies

        policies = _project_network_policies(
            [
                {"host": "api.openai.com", "ports": [443], "purpose": "LLM"},
                {"host": "mcp.context7.com", "ports": [443], "purpose": "MCP context7"},
                {"host": "api.githubcopilot.com", "ports": [443], "purpose": "MCP github"},
            ],
            bridged_mcp_hosts={"mcp.context7.com", "api.githubcopilot.com"},
        )

        self.assertIn("api_openai_com", policies)
        self.assertNotIn("mcp_context7_com", policies)
        self.assertNotIn("api_githubcopilot_com", policies)

    def test_load_mcp_upstream_hosts_from_gateway_yaml(self):
        from guard.onboard import _load_mcp_upstream_hosts

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "gateway.yaml").write_text(
                yaml.dump({
                    "mcp": {"servers": [
                        {"name": "github", "url": "https://api.githubcopilot.com/mcp/"},
                        {"name": "context7", "url": "https://mcp.context7.com/mcp"},
                        {"name": "earnings", "url": "https://earnings-mcp-server.brilliantforecast.workers.dev/mcp"},
                    ]}
                }),
                encoding="utf-8",
            )

            hosts = _load_mcp_upstream_hosts(workspace)

        self.assertEqual(
            hosts,
            {"api.githubcopilot.com", "mcp.context7.com",
             "earnings-mcp-server.brilliantforecast.workers.dev"},
        )


if __name__ == "__main__":
    unittest.main()
