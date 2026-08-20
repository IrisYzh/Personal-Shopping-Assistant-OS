from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from .provider import ModelProvider
from .types import AgentRunResult, AgentSpec, ToolSpec, extract_json_object, validate_json_schema


class AgentRunner:
    """A bounded observe-plan-tool-evaluate loop shared by all three LLM agents."""

    def __init__(self, provider: ModelProvider):
        self.provider = provider

    async def run(
        self,
        spec: AgentSpec,
        *,
        input_payload: dict[str, Any],
        tools: list[ToolSpec] | None = None,
    ) -> AgentRunResult:
        tools = tools or []
        tool_map = {tool.name: tool for tool in tools}
        trace: list[dict[str, Any]] = []
        previous_response_id: str | None = None
        tool_outputs: list[dict[str, Any]] | None = None
        tool_call_count = 0
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        next_input_text = json.dumps(input_payload, ensure_ascii=False)

        for turn in range(1, spec.max_turns + 1):
            response = await self.provider.respond(
                agent_name=spec.name,
                model=spec.model,
                instructions=spec.instructions,
                input_text=next_input_text,
                tools=[tool.api_schema() for tool in tools],
                previous_response_id=previous_response_id,
                tool_outputs=tool_outputs,
            )
            next_input_text = None
            for key in usage:
                usage[key] += int(response.usage.get(key, 0) or 0)
            trace.append(
                {
                    "turn": turn,
                    "response_id": response.id,
                    "requested_tools": [call.name for call in response.function_calls],
                    "usage": response.usage,
                }
            )
            if not response.function_calls:
                try:
                    output = extract_json_object(response.output_text)
                    if spec.output_schema:
                        validate_json_schema(output, spec.output_schema)
                except ValueError as exc:
                    trace[-1]["schema_error"] = str(exc)
                    if turn >= spec.max_turns:
                        raise
                    previous_response_id = None
                    tool_outputs = None
                    next_input_text = json.dumps(
                        {
                            "original_input": input_payload,
                            "repair_instruction": (
                                "Your previous final answer failed the required JSON schema. "
                                "Return a corrected JSON object only."
                            ),
                            "validation_error": str(exc),
                        },
                        ensure_ascii=False,
                    )
                    continue
                return AgentRunResult(
                    agent_name=spec.name,
                    output=output,
                    raw_text=response.output_text,
                    turns=turn,
                    tool_calls=tool_call_count,
                    usage=usage,
                    trace=trace,
                )

            tool_call_count += len(response.function_calls)
            if tool_call_count > spec.max_tool_calls:
                raise RuntimeError(f"{spec.name} exceeded max_tool_calls={spec.max_tool_calls}")

            async def execute(call):
                tool = tool_map.get(call.name)
                if not tool:
                    result = {"ok": False, "error": f"unknown_tool:{call.name}"}
                elif tool.permission != "read":
                    result = {"ok": False, "error": "side_effect_tool_not_allowed_in_agent_loop"}
                else:
                    try:
                        arguments = json.loads(call.arguments or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be an object")
                        value = tool.handler(arguments)
                        if inspect.isawaitable(value):
                            value = await value
                        result = {"ok": True, "data": value}
                    except Exception as exc:  # returned to the model so it can repair or degrade
                        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
                return {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                }

            tool_outputs = await asyncio.gather(*(execute(call) for call in response.function_calls))
            previous_response_id = response.id

        raise RuntimeError(f"{spec.name} exceeded max_turns={spec.max_turns}")
