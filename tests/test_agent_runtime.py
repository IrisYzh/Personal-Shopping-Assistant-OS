import asyncio
import json
import unittest

from shopping_agent.multiagent.provider import ScriptedProvider
from shopping_agent.multiagent.runtime import AgentRunner
from shopping_agent.multiagent.types import AgentSpec, FunctionCall, ModelResponse, ToolSpec


class AgentRuntimeTests(unittest.TestCase):
    def test_agent_selects_tool_and_continues_to_structured_result(self):
        provider = ScriptedProvider(
            {
                "test_agent": [
                    ModelResponse(
                        id="r1",
                        function_calls=[FunctionCall("c1", "read_fact", "{}")],
                    ),
                    ModelResponse(id="r2", output_text=json.dumps({"answer": 42})),
                ]
            }
        )
        runner = AgentRunner(provider)
        result = asyncio.run(
            runner.run(
                AgentSpec("test_agent", "test-model", "Use tools"),
                input_payload={"question": "value"},
                tools=[
                    ToolSpec(
                        name="read_fact",
                        description="Read a fact",
                        parameters={"type": "object", "properties": {}},
                        handler=lambda _: {"value": 42},
                    )
                ],
            )
        )
        self.assertEqual(result.output, {"answer": 42})
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(provider.requests[1]["tool_outputs"][0]["call_id"], "c1")

    def test_write_tool_is_blocked_inside_agent_loop(self):
        provider = ScriptedProvider(
            {
                "test_agent": [
                    ModelResponse(
                        id="r1",
                        function_calls=[FunctionCall("c1", "save_item", "{}")],
                    ),
                    ModelResponse(id="r2", output_text=json.dumps({"saved": False})),
                ]
            }
        )
        result = asyncio.run(
            AgentRunner(provider).run(
                AgentSpec("test_agent", "test-model", "Do not write"),
                input_payload={},
                tools=[
                    ToolSpec(
                        name="save_item",
                        description="Unsafe write",
                        parameters={"type": "object", "properties": {}},
                        handler=lambda _: {"saved": True},
                        permission="write",
                    )
                ],
            )
        )
        tool_output = json.loads(provider.requests[1]["tool_outputs"][0]["output"])
        self.assertFalse(tool_output["ok"])
        self.assertIn("not_allowed", tool_output["error"])
        self.assertEqual(result.output, {"saved": False})

    def test_final_output_must_match_agent_json_schema(self):
        provider = ScriptedProvider(
            {"test_agent": [ModelResponse(id="r1", output_text='{"verdict":"maybe"}')]}
        )
        spec = AgentSpec(
            "test_agent",
            "test-model",
            "Return a verdict",
            output_schema={
                "type": "object",
                "properties": {"verdict": {"type": "string", "enum": ["approve", "reject"]}},
                "required": ["verdict"],
                "additionalProperties": False,
            },
            max_turns=1,
        )
        with self.assertRaisesRegex(ValueError, "must be one of"):
            asyncio.run(AgentRunner(provider).run(spec, input_payload={}))

    def test_schema_error_gets_one_self_repair_turn(self):
        provider = ScriptedProvider(
            {
                "test_agent": [
                    ModelResponse(id="r1", output_text='{"verdict":"maybe"}'),
                    ModelResponse(id="r2", output_text='{"verdict":"approve"}'),
                ]
            }
        )
        spec = AgentSpec(
            "test_agent",
            "test-model",
            "Return a verdict",
            output_schema={
                "type": "object",
                "properties": {"verdict": {"type": "string", "enum": ["approve", "reject"]}},
                "required": ["verdict"],
                "additionalProperties": False,
            },
            max_turns=2,
        )
        result = asyncio.run(AgentRunner(provider).run(spec, input_payload={"case": "x"}))
        self.assertEqual(result.output, {"verdict": "approve"})
        self.assertIn("schema_error", result.trace[0])
        self.assertIn("repair_instruction", provider.requests[1]["input_text"])


if __name__ == "__main__":
    unittest.main()
