import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from guard.cli import app


class BridgeCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _state_path(self) -> Path:
        return self.workspace / ".guard" / "mcp-bridges.json"

    def test_bridge_add_records_planned_bridge(self):
        server = {
            "name": "github",
            "status": "approved",
            "transport": "streamable_http",
            "url": "https://api.githubcopilot.com/mcp/",
            "credential_env": "GITHUB_MCP_TOKEN",
            "purpose": "GitHub MCP",
        }
        with patch("guard.cli._find_mcp_server", return_value=server):
            result = self.runner.invoke(
                app,
                [
                    "bridge",
                    "add",
                    "github",
                    "--sandbox",
                    "my-assistant",
                    "--workspace",
                    str(self.workspace),
                ],
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("recorded planned MCP bridge 'github'", result.stdout)
        saved = json.loads(self._state_path().read_text(encoding="utf-8"))
        entry = saved["sandboxes"]["my-assistant"]["bridges"]["github"]
        self.assertEqual(entry["execution_model"], "compatibility-bridge")
        self.assertEqual(entry["transport"], "streamable-http")
        self.assertEqual(entry["credential_env"], "GITHUB_MCP_TOKEN")
        self.assertEqual(entry["allowed_hosts"], ["api.githubcopilot.com"])

    def test_bridge_add_accepts_custom_host_alias(self):
        server = {
            "name": "github",
            "status": "approved",
            "transport": "streamable_http",
            "url": "https://api.githubcopilot.com/mcp/",
            "purpose": "GitHub MCP",
        }
        with patch("guard.cli._find_mcp_server", return_value=server):
            result = self.runner.invoke(
                app,
                [
                    "bridge",
                    "add",
                    "github",
                    "--sandbox",
                    "my-assistant",
                    "--workspace",
                    str(self.workspace),
                    "--host-alias",
                    "172.31.12.246",
                ],
            )

        self.assertEqual(result.exit_code, 0)
        saved = json.loads(self._state_path().read_text(encoding="utf-8"))
        entry = saved["sandboxes"]["my-assistant"]["bridges"]["github"]
        self.assertEqual(entry["host_alias"], "172.31.12.246")
        self.assertIn("Host alias: 172.31.12.246", result.stdout)

    def test_bridge_list_prints_saved_records(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "planned",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app, ["bridge", "list", "--workspace", str(self.workspace)]
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("my-assistant", result.stdout)
        self.assertIn("github", result.stdout)
        self.assertIn("streamable-http", result.stdout)

    def test_bridge_restart_marks_record(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {"github": {"status": "running"}}
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "restart",
                "github",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["sandboxes"]["my-assistant"]["bridges"]["github"]["status"], "planned"
        )

    def test_bridge_render_openclaw_outputs_planned_host_url(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "planned",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                    "credential_env": "GITHUB_MCP_TOKEN",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "render",
                "github",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
                "--format",
                "openclaw",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("http://host.openshell.internal:8090/mcp/github/", result.stdout)
        self.assertIn("openclaw mcp set github", result.stdout)
        self.assertIn('"transport": "streamable-http"', result.stdout)

    def test_bridge_render_openclaw_bundle_outputs_native_bundle_files(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "active",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "render-openclaw-bundle",
                "github",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn('/sandbox/.openclaw/extensions/guard-mcp-bundle/.claude-plugin/plugin.json', result.stdout)
        self.assertIn('/sandbox/.openclaw/extensions/guard-mcp-bundle/.mcp.json', result.stdout)
        self.assertIn('"name": "guard-mcp-bundle"', result.stdout)
        self.assertIn('"url": "http://host.openshell.internal:8090/mcp/github/"', result.stdout)
        self.assertIn('"transport": "streamable-http"', result.stdout)
        self.assertIn('"enabled": true', result.stdout)
        self.assertNotIn('"baseUrl"', result.stdout)

    def test_bridge_render_defaults_to_openclaw_format(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "active",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                    "credential_env": "GITHUB_MCP_TOKEN",
                                    "purpose": "GitHub MCP",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "render",
                "github",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("openclaw mcp set github", result.stdout)
        self.assertIn("http://host.openshell.internal:8090/mcp/github/", result.stdout)

    def test_bridge_render_openclaw_bundle_omits_transport_for_sse(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "docs": {
                                    "status": "active",
                                    "transport": "sse",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://example.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "render-openclaw-bundle",
                "docs",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
                "--plugin-id",
                "docs-bundle",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn('"url": "http://host.openshell.internal:8090/mcp/docs/"', result.stdout)
        self.assertNotIn('"transport": "sse"', result.stdout)
        self.assertIn('"name": "docs-bundle"', result.stdout)

    def test_bridge_stage_openclaw_bundle_writes_expected_files(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "active",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        out_dir = self.workspace / "bundle-out"
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "stage-openclaw-bundle",
                "github",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
                "--output-dir",
                str(out_dir),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        plugin_manifest = json.loads(
            (out_dir / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        bundle_config = json.loads((out_dir / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_manifest, {"name": "guard-mcp-bundle"})
        self.assertEqual(
            bundle_config,
            {
                "mcpServers": {
                    "github": {
                        "url": "http://host.openshell.internal:8090/mcp/github/",
                        "transport": "streamable-http",
                    }
                }
            },
        )
        self.assertIn("Staged OpenClaw bundle plugin", result.stdout)

    def test_bridge_stage_openclaw_bundle_omits_sse_transport(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "docs": {
                                    "status": "active",
                                    "transport": "sse",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://example.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        out_dir = self.workspace / "docs-bundle-out"
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "stage-openclaw-bundle",
                "docs",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
                "--plugin-id",
                "docs-bundle",
                "--output-dir",
                str(out_dir),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        bundle_config = json.loads((out_dir / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(
            bundle_config,
            {
                "mcpServers": {
                    "docs": {
                        "url": "http://host.openshell.internal:8090/mcp/docs/",
                        "transport": "streamable-http",
                    }
                }
            },
        )

    def test_bridge_enable_openclaw_bundle_creates_new_config_file(self):
        config_path = self.workspace / "openclaw.json"
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "enable-openclaw-bundle",
                "--plugin-id",
                "guard-mcp-bundle",
                "--config-path",
                str(config_path),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertTrue(data["plugins"]["entries"]["guard-mcp-bundle"]["enabled"])

    def test_bridge_enable_openclaw_bundle_merges_existing_config(self):
        config_path = self.workspace / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "gateway": {"auth": {"token": "dummy-token"}},
                    "plugins": {"entries": {"existing-plugin": {"enabled": False}}},
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "enable-openclaw-bundle",
                "--plugin-id",
                "guard-mcp-bundle",
                "--config-path",
                str(config_path),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["gateway"]["auth"]["token"], "dummy-token")
        self.assertFalse(data["plugins"]["entries"]["existing-plugin"]["enabled"])
        self.assertTrue(data["plugins"]["entries"]["guard-mcp-bundle"]["enabled"])

    def test_bridge_enable_openclaw_bundle_rejects_non_object_json(self):
        config_path = self.workspace / "openclaw.json"
        config_path.write_text('["not-an-object"]\n', encoding="utf-8")
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "enable-openclaw-bundle",
                "--plugin-id",
                "guard-mcp-bundle",
                "--config-path",
                str(config_path),
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("must contain a JSON object", result.stdout)

    def test_bridge_render_json_outputs_machine_readable_payload(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "planned",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "host_port": 18090,
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "render",
                "github",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
                "--format",
                "json",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "github")
        self.assertEqual(payload["host_port"], 18090)
        self.assertEqual(payload["bridge_url"], "http://host.openshell.internal:18090/mcp/github/")

    def test_bridge_activate_marks_runtime_active(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "planned",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        server = {
            "name": "github",
            "status": "approved",
            "transport": "streamable_http",
            "url": "https://api.githubcopilot.com/mcp/",
        }
        with patch("guard.cli._find_mcp_server", return_value=server):
            result = self.runner.invoke(
                app,
                [
                    "bridge",
                    "activate",
                    "github",
                    "--sandbox",
                    "my-assistant",
                    "--workspace",
                    str(self.workspace),
                ],
            )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("activated bridge 'github'", result.stdout)
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        entry = saved["sandboxes"]["my-assistant"]["bridges"]["github"]
        self.assertEqual(entry["status"], "active")
        self.assertEqual(entry["execution_model"], "gateway-http-bridge")
        self.assertEqual(entry["host_port"], 8090)

    def test_bridge_activate_can_auto_detect_host_alias(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "planned",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        server = {
            "name": "github",
            "status": "approved",
            "transport": "streamable_http",
            "url": "https://api.githubcopilot.com/mcp/",
        }
        with (
            patch("guard.cli._find_mcp_server", return_value=server),
            patch(
                "guard.cli._default_bridge_host_candidates",
                return_value=["host.openshell.internal", "172.31.12.246"],
            ),
            patch(
                "guard.cli._probe_bridge_host_alias",
                side_effect=[(False, "connect ECONNREFUSED"), (True, '{"status":"ok"}')],
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "bridge",
                    "activate",
                    "github",
                    "--sandbox",
                    "my-assistant",
                    "--workspace",
                    str(self.workspace),
                    "--auto-detect-host-alias",
                    "--force-probe",
                ],
            )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("FAILED   host.openshell.internal", result.stdout)
        self.assertIn("OK       172.31.12.246", result.stdout)
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        entry = saved["sandboxes"]["my-assistant"]["bridges"]["github"]
        self.assertEqual(entry["host_alias"], "172.31.12.246")



    def test_bridge_print_sandbox_steps_includes_verification_commands(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "active",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "print-sandbox-steps",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("guard bridge activate github --sandbox my-assistant", result.stdout)
        self.assertIn("Stage the OpenClaw native MCP bundle", result.stdout)
        self.assertIn(
            "python -m guard.cli bridge render-openclaw-bundle github --sandbox my-assistant --workspace",
            result.stdout,
        )
        self.assertIn("keep the default proxy environment enabled", result.stdout)

    def test_bridge_verify_runtime_reports_success(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "active",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        server = {
            "name": "github",
            "status": "approved",
            "transport": "streamable_http",
            "url": "https://api.githubcopilot.com/mcp/",
        }
        with patch("guard.cli._find_mcp_server", return_value=server):
            result = self.runner.invoke(
                app,
                [
                    "bridge",
                    "verify-runtime",
                    "github",
                    "--sandbox",
                    "my-assistant",
                    "--workspace",
                    str(self.workspace),
                ],
            )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("OK       bridge is active", result.stdout)
        self.assertIn("OK       gateway MCP server approved", result.stdout)
        self.assertIn("stage-openclaw-bundle", result.stdout)

    def test_bridge_print_sandbox_steps_uses_custom_host_alias(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "active",
                                    "transport": "streamable-http",
                                    "host_alias": "172.31.12.246",
                                    "host_port": 18090,
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.runner.invoke(
            app,
            [
                "bridge",
                "print-sandbox-steps",
                "--sandbox",
                "my-assistant",
                "--workspace",
                str(self.workspace),
                "--gateway-port",
                "18090",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Stage the OpenClaw native MCP bundle", result.stdout)
        self.assertNotIn("NO_PROXY=", result.stdout)
        self.assertIn("http://172.31.12.246:18090/mcp/github/", result.stdout)

    def test_bridge_detect_host_alias_updates_state(self):
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sandboxes": {
                        "my-assistant": {
                            "bridges": {
                                "github": {
                                    "status": "active",
                                    "transport": "streamable-http",
                                    "host_alias": "host.openshell.internal",
                                    "upstream_url": "https://api.githubcopilot.com/mcp/",
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch(
            "guard.cli._probe_bridge_host_alias",
            side_effect=[(False, "connect ECONNREFUSED"), (True, '{"status":"ok"}')],
        ):
            result = self.runner.invoke(
                app,
                [
                    "bridge",
                    "detect-host-alias",
                    "--sandbox",
                    "my-assistant",
                    "--workspace",
                    str(self.workspace),
                    "--name",
                    "github",
                    "--candidates",
                    "host.openshell.internal,172.17.0.1",
                ],
            )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("FAILED   host.openshell.internal", result.stdout)
        self.assertIn("OK       172.17.0.1", result.stdout)
        self.assertIn("Detected bridge host alias: 172.17.0.1", result.stdout)
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        entry = saved["sandboxes"]["my-assistant"]["bridges"]["github"]
        self.assertEqual(entry["host_alias"], "172.17.0.1")


if __name__ == "__main__":
    unittest.main()
