import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from shopping_agent.multiagent.provider import (
    ClaudeMessagesProvider,
    default_models,
)
from shopping_agent.multiagent.runtime import AgentRunner
from shopping_agent.multiagent.types import AgentSpec, ToolSpec


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(block_id, name, arguments):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=arguments)


def message(message_id, blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        id=message_id,
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **params):
        # Copy the list so later appends by the provider do not change what we saw.
        self.calls.append({**params, "messages": list(params["messages"])})
        return self.replies.pop(0)


class ClaudeProviderTests(unittest.TestCase):
    def test_tool_round_trip_replays_history_with_tool_results(self):
        fake = FakeMessages(
            [
                message("msg_1", [tool_block("toolu_1", "read_page", {"url": "https://shop.lululemon.com/p/1"})], "tool_use"),
                message("msg_2", [text_block('{"in_stock": true}')]),
            ]
        )
        provider = ClaudeMessagesProvider(client=SimpleNamespace(messages=fake))
        runner = AgentRunner(provider)
        tool = ToolSpec(
            name="read_page",
            description="Read a product page",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            handler=lambda args: {"title": "Align Pant", "url": args["url"]},
        )
        spec = AgentSpec(name="product_intelligence", model="claude-haiku-5-5", instructions="Return JSON.")

        result = asyncio.run(runner.run(spec, input_payload={"ask": "stock"}, tools=[tool]))

        self.assertEqual(result.output, {"in_stock": True})
        first, second = fake.calls
        self.assertEqual(first["system"], "Return JSON.")
        self.assertEqual(first["model"], "claude-haiku-5-5")
        self.assertEqual(first["tools"][0]["input_schema"]["required"], ["url"])
        self.assertNotIn("parameters", first["tools"][0])
        # Second call carries the user turn, the assistant tool_use turn, then the tool result.
        self.assertEqual([m["role"] for m in second["messages"]], ["user", "assistant", "user"])
        tool_result = second["messages"][2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "toolu_1")
        self.assertTrue(json.loads(tool_result["content"])["ok"])
        self.assertEqual(len(provider.telemetry), 2)
        self.assertEqual(provider.telemetry[0]["tool_calls"], ["read_page"])

    def test_refusal_raises_instead_of_returning_empty_text(self):
        fake = FakeMessages([message("msg_1", [], "refusal")])
        provider = ClaudeMessagesProvider(client=SimpleNamespace(messages=fake))
        with self.assertRaises(RuntimeError):
            asyncio.run(
                provider.respond(
                    agent_name="verifier", model="claude-sonnet-5-5", instructions="x", input_text="y", tools=[]
                )
            )

    def test_default_models_are_claude_and_can_be_overridden(self):
        with mock.patch.dict(os.environ, {"CLAUDE_VERIFIER_MODEL": "claude-opus-5-5"}, clear=False):
            os.environ.pop("SHOPPING_AGENT_PROVIDER", None)
            models = default_models()
        self.assertEqual(models["product"], "claude-haiku-5-5")
        self.assertEqual(models["supervisor"], "claude-haiku-5-5")
        self.assertEqual(models["verifier"], "claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()
