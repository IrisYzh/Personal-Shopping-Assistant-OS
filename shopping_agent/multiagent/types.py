from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    permission: str = "read"

    def api_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass(slots=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ModelResponse:
    id: str
    output_text: str = ""
    function_calls: list[FunctionCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentSpec:
    name: str
    model: str
    instructions: str
    output_schema: dict[str, Any] | None = None
    max_turns: int = 6
    max_tool_calls: int = 10


@dataclass(slots=True)
class AgentRunResult:
    agent_name: str
    output: dict[str, Any]
    raw_text: str
    turns: int
    tool_calls: int
    usage: dict[str, int] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ProductPageSnapshot:
    url: str
    title: str = ""
    html: str = ""
    visible_text: str = ""


class EvidenceBoard:
    """Shared blackboard containing facts and agent outputs, never hidden reasoning."""

    def __init__(self):
        self.entries: list[dict[str, Any]] = []

    def add(self, *, source: str, kind: str, payload: Any) -> str:
        evidence_id = f"ev_{len(self.entries) + 1}"
        self.entries.append(
            {"id": evidence_id, "source": source, "kind": kind, "payload": payload}
        )
        return evidence_id

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.entries)


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a model JSON response, tolerating a fenced wrapper but not prose guesses."""
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Agent output is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Agent output must be a JSON object")
    return parsed


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the strict subset of JSON Schema used by agent contracts."""
    expected = schema.get("type")
    allowed_types = expected if isinstance(expected, list) else [expected] if expected else []
    if allowed_types and not any(_matches_json_type(value, item) for item in allowed_types):
        raise ValueError(f"{path} must be of type {allowed_types}, got {type(value).__name__}")
    if value is None:
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"{path} missing required fields: {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = [key for key in value if key not in properties]
            if extras:
                raise ValueError(f"{path} has unexpected fields: {extras}")
        for key, child_schema in properties.items():
            if key in value:
                validate_json_schema(value[key], child_schema, f"{path}.{key}")
    elif isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_json_schema(item, schema["items"], f"{path}[{index}]")


def _matches_json_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(expected, True)
