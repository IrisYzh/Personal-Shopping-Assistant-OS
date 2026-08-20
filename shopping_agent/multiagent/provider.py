from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Protocol

from .types import FunctionCall, ModelResponse


class ModelProvider(Protocol):
    async def respond(
        self,
        *,
        agent_name: str,
        model: str,
        instructions: str,
        input_text: str | None,
        tools: list[dict[str, Any]],
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
    ) -> ModelResponse: ...


class ArkResponsesProvider:
    """Minimal async adapter for Volcengine Ark's Responses API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        timeout_seconds: int = 60,
        store_responses: bool = True,
        max_retries: int = 2,
    ):
        self._load_local_env()
        self.api_key = api_key or os.environ.get("ARK_API_KEY")
        if not self.api_key:
            raise RuntimeError("ARK_API_KEY is required for real Doubao agent runs")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.store_responses = store_responses
        self.max_retries = max_retries
        self.telemetry: list[dict[str, Any]] = []

    @staticmethod
    def _load_local_env() -> None:
        """Load a gitignored local .env without printing or overriding shell values."""
        path = Path.cwd() / ".env"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

    async def respond(self, **kwargs) -> ModelResponse:
        return await asyncio.to_thread(self._respond_sync, **kwargs)

    def _respond_sync(
        self,
        *,
        agent_name: str,
        model: str,
        instructions: str,
        input_text: str | None,
        tools: list[dict[str, Any]],
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": model,
            "store": self.store_responses,
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
            payload["input"] = tool_outputs or []
        else:
            payload["input"] = [
                {
                    "type": "message",
                    "role": "user",
                    "content": f"<instructions>\n{instructions}\n</instructions>\n\n<input>\n{input_text or ''}\n</input>",
                }
            ]
        if tools:
            payload["tools"] = tools

        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Ark Responses API failed ({exc.code}): {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                reason = getattr(exc, "reason", str(exc))
                raise RuntimeError(f"Ark Responses API network error: {reason}") from exc

        calls: list[FunctionCall] = []
        text_parts: list[str] = []
        for item in raw.get("output", []):
            if item.get("type") == "function_call":
                calls.append(
                    FunctionCall(
                        call_id=item["call_id"],
                        name=item["name"],
                        arguments=item.get("arguments", "{}"),
                    )
                )
            elif item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") in ("output_text", "text"):
                        text_parts.append(content.get("text", ""))
        usage = {
            key: int(raw.get("usage", {}).get(key, 0) or 0)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        result = ModelResponse(
            id=raw.get("id", ""),
            output_text="\n".join(text_parts),
            function_calls=calls,
            usage=usage,
            raw=raw,
        )
        self.telemetry.append(
            {
                "agent_name": agent_name,
                "model": model,
                "response_id": result.id,
                "usage": usage,
                "tool_calls": [call.name for call in calls],
                "stop_output": not bool(calls),
            }
        )
        return result

    def reset_telemetry(self) -> None:
        self.telemetry.clear()


class ScriptedProvider:
    """Deterministic provider used to test the same agent loop without an API key."""

    def __init__(self, scripts: dict[str, list[ModelResponse]]):
        self.scripts = defaultdict(deque)
        for agent_name, responses in scripts.items():
            self.scripts[agent_name].extend(responses)
        self.requests: list[dict[str, Any]] = []

    async def respond(self, **kwargs) -> ModelResponse:
        agent_name = kwargs["agent_name"]
        self.requests.append(kwargs)
        if not self.scripts[agent_name]:
            raise RuntimeError(f"No scripted response left for {agent_name}")
        return self.scripts[agent_name].popleft()
