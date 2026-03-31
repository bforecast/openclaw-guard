import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gateway  # noqa: E402


class ResolveProviderTests(unittest.TestCase):
    def test_routes_claude_models_to_anthropic(self):
        provider_name, provider_cfg, cleaned_model = gateway.resolve_provider(
            "claude-3-5-sonnet-20241022"
        )

        self.assertEqual(provider_name, "anthropic")
        self.assertEqual(cleaned_model, "claude-3-5-sonnet-20241022")
        self.assertEqual(provider_cfg["endpoint"], "/messages")

    def test_strips_openrouter_prefix(self):
        provider_name, provider_cfg, cleaned_model = gateway.resolve_provider(
            "openrouter/deepseek/deepseek-chat"
        )

        self.assertEqual(provider_name, "openrouter")
        self.assertEqual(cleaned_model, "deepseek/deepseek-chat")
        self.assertEqual(provider_cfg["endpoint"], "/chat/completions")


class MessageScanningTests(unittest.TestCase):
    def test_extracts_text_from_structured_content(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "please inspect this"},
                    {"type": "input_text", "text": "rm -rf /tmp/demo"},
                ],
            }
        ]

        self.assertIn("rm -rf /tmp/demo", gateway.extract_text_from_messages(messages))
        self.assertEqual(
            gateway.scan_messages(messages),
            (False, "Blocked: dangerous pattern 'rm -rf' detected"),
        )

    def test_scans_dangerous_output_text(self):
        is_safe, reason = gateway.scan_text_output("Do this: rm -rf /tmp/demo")
        self.assertFalse(is_safe)
        self.assertIn("Blocked output", reason)

    def test_extracts_text_from_responses_payload(self):
        payload = {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": "hello"},
                        {"type": "output_text", "text": "rm -rf /tmp/demo"},
                    ]
                }
            ]
        }
        text = gateway.extract_text_from_response_payload(payload)
        self.assertIn("hello", text)
        self.assertIn("rm -rf /tmp/demo", text)

    def test_latest_user_input_for_scan_uses_last_user_turn(self):
        body = {
            "input": [
                {"role": "user", "content": "old safe prompt"},
                {"role": "assistant", "content": "here is text with rm -rf /tmp/x"},
                {"role": "user", "content": "latest safe prompt"},
            ]
        }
        latest = gateway._latest_user_input_for_scan(body)
        self.assertEqual(latest, "latest safe prompt")

    def test_chat_to_responses_payload_conversion(self):
        chat_payload = {
            "id": "chatcmpl_x",
            "model": "openrouter/stepfun/step-3.5-flash:free",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }
        resp_payload = gateway._chat_to_responses_payload(chat_payload)
        self.assertEqual(resp_payload["object"], "response")
        self.assertEqual(resp_payload["output_text"], "hello")
        self.assertEqual(resp_payload["status"], "completed")
        self.assertEqual(resp_payload["output"][0]["type"], "message")
        self.assertEqual(resp_payload["usage"]["total_tokens"], 7)
        self.assertTrue(resp_payload["id"].startswith("resp_"))

    def test_responses_stream_event_contains_event_and_data_lines(self):
        event = {"type": "response.created", "response": {"id": "resp_1"}}
        payload = gateway._responses_stream_event(event).decode("utf-8")
        self.assertIn("event: response.created", payload)
        self.assertIn('data: {"type": "response.created"', payload)

    def test_retry_delay_prefers_retry_after_header(self):
        self.assertEqual(gateway._retry_delay_seconds(1, "3"), 3.0)
        self.assertEqual(gateway._retry_delay_seconds(2, "bad"), 2)


class AnthropicTransformTests(unittest.TestCase):
    def test_converts_openai_style_messages_to_anthropic_payload(self):
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "stream": True,
            "temperature": 0.2,
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": "You are a careful reviewer."},
                {"role": "user", "content": "Review this patch."},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I can help with that."}],
                },
            ],
        }

        transformed = gateway.transform_anthropic_request(
            body, "claude-3-5-sonnet-20241022"
        )

        self.assertEqual(transformed["model"], "claude-3-5-sonnet-20241022")
        self.assertTrue(transformed["stream"])
        self.assertEqual(transformed["temperature"], 0.2)
        self.assertEqual(transformed["max_tokens"], 2048)
        self.assertEqual(transformed["system"], "You are a careful reviewer.")
        self.assertEqual(len(transformed["messages"]), 2)
        self.assertEqual(transformed["messages"][0]["role"], "user")
        self.assertEqual(
            transformed["messages"][0]["content"],
            [{"type": "text", "text": "Review this patch."}],
        )
        self.assertEqual(transformed["messages"][1]["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
