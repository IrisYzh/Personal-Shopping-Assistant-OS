import json
import unittest
from unittest.mock import patch

from shopping_agent.multiagent.provider import ArkResponsesProvider


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ArkResponsesProviderTests(unittest.TestCase):
    def test_initial_response_serializes_tools_and_parses_function_call(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeHTTPResponse(
                {
                    "id": "resp_1",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "parse_jsonld",
                            "arguments": "{}",
                        }
                    ],
                }
            )

        provider = ArkResponsesProvider(api_key="test-key", timeout_seconds=7)
        with patch("urllib.request.urlopen", fake_urlopen):
            response = provider._respond_sync(
                agent_name="product_intelligence",
                model="model-id",
                instructions="Use tools.",
                input_text='{"mode":"identify"}',
                tools=[
                    {
                        "type": "function",
                        "name": "parse_jsonld",
                        "description": "Parse Product JSON-LD",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            )

        self.assertEqual(captured["url"], "https://ark.cn-beijing.volces.com/api/v3/responses")
        self.assertEqual(captured["payload"]["model"], "model-id")
        self.assertEqual(captured["payload"]["tools"][0]["name"], "parse_jsonld")
        self.assertIn("Use tools.", captured["payload"]["input"][0]["content"])
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(response.function_calls[0].name, "parse_jsonld")

    def test_continuation_uses_previous_response_and_parses_json_text(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(
                {
                    "id": "resp_2",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": '{"verdict":"approve"}'}
                            ],
                        }
                    ],
                }
            )

        tool_outputs = [
            {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'}
        ]
        provider = ArkResponsesProvider(api_key="test-key")
        with patch("urllib.request.urlopen", fake_urlopen):
            response = provider._respond_sync(
                agent_name="independent_verifier",
                model="model-id",
                instructions="ignored on continuation",
                input_text=None,
                tools=[],
                previous_response_id="resp_1",
                tool_outputs=tool_outputs,
            )

        self.assertEqual(captured["payload"]["previous_response_id"], "resp_1")
        self.assertEqual(captured["payload"]["input"], tool_outputs)
        self.assertEqual(response.output_text, '{"verdict":"approve"}')


if __name__ == "__main__":
    unittest.main()
