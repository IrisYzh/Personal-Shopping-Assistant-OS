from __future__ import annotations

from typing import Any

from ..intent_agent import IntentAgent
from ..models import ProductContext, to_primitive
from ..storage import JsonRepository
from .product_tools import inspect_meta, inspect_visible_text, parse_jsonld
from .runtime import AgentRunner
from .types import AgentRunResult, AgentSpec, EvidenceBoard, ProductPageSnapshot, ToolSpec


OBJECT_SCHEMA = {"type": "object", "additionalProperties": True}
NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_NUMBER = {"type": ["number", "null"]}
STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

PRODUCT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": NULLABLE_STRING,
        "url": NULLABLE_STRING,
        "price": NULLABLE_NUMBER,
        "currency": {"type": "string"},
        "availability": {"type": "string", "enum": ["in_stock", "out_of_stock", "unknown"]},
        "confidence": {"type": "number"},
        "extraction_method": {"type": "string"},
        "evidence": STRING_ARRAY,
        "uncertainties": STRING_ARRAY,
    },
    "required": ["name", "url", "price", "currency", "availability", "confidence", "extraction_method", "evidence"],
    "additionalProperties": False,
}

VERIFIER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "ask_user", "reject"]},
        "confidence": {"type": "number"},
        "reasons": STRING_ARRAY,
        "missing_fields": STRING_ARRAY,
        "recommended_question": NULLABLE_STRING,
    },
    "required": ["verdict", "confidence", "reasons", "missing_fields", "recommended_question"],
    "additionalProperties": False,
}

SUPERVISOR_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["explicit_purchase", "fuzzy_interest", "unknown"]},
                "confidence": {"type": "number"},
                "target_price": NULLABLE_NUMBER,
                "notify_restock": {"type": "boolean"},
                "evidence": STRING_ARRAY,
            },
            "required": ["kind", "confidence", "target_price", "notify_restock", "evidence"],
            "additionalProperties": False,
        },
        "product": PRODUCT_OUTPUT_SCHEMA,
        "recommended_route": {"type": "string", "enum": ["auto_save", "ask_confirmation", "ignore"]},
        "question": NULLABLE_STRING,
        "plan_summary": STRING_ARRAY,
    },
    "required": ["intent", "product", "recommended_route", "question", "plan_summary"],
    "additionalProperties": False,
}


class ProductIntelligenceAgent:
    """Autonomous product researcher used for both identification and monitoring."""

    def __init__(self, runner: AgentRunner, model: str):
        self.runner = runner
        self.spec = AgentSpec(
            name="product_intelligence",
            model=model,
            output_schema=PRODUCT_OUTPUT_SCHEMA,
            max_turns=6,
            max_tool_calls=8,
            instructions="""
You are the Product Intelligence Agent for a shopping assistant.
Your task mode is either identify (resolve a product for first capture) or observe
(find its current price and availability). Autonomously select the least expensive
read-only tools needed. Prefer parse_jsonld, then inspect_meta, then visible text.
Never invent a price, stock state, variant, or URL. If evidence conflicts, return
unknown and list the conflict. Do not save data or decide whether to notify.

Return JSON only with this shape:
{
  "name": string|null, "url": string|null, "price": number|null,
  "currency": string, "availability": "in_stock"|"out_of_stock"|"unknown",
  "confidence": number, "extraction_method": "jsonld"|"meta"|"model"|"unknown",
  "evidence": [string], "uncertainties": [string]
}
""".strip(),
        )

    async def run(
        self,
        *,
        mode: str,
        expression: str,
        product: ProductContext,
        snapshot: ProductPageSnapshot,
        previous_state: dict[str, Any] | None,
        board: EvidenceBoard,
    ) -> AgentRunResult:
        tools = [
            ToolSpec(
                name="parse_jsonld",
                description="Parse schema.org Product JSON-LD from the current page. Use first.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _: parse_jsonld(snapshot),
            ),
            ToolSpec(
                name="inspect_meta",
                description="Read Open Graph and product meta tags when JSON-LD is absent or incomplete.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _: inspect_meta(snapshot),
            ),
            ToolSpec(
                name="inspect_visible_text",
                description="Read a bounded visible-text excerpt only when structured sources are insufficient.",
                parameters={
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 500, "maximum": 6000}},
                    "additionalProperties": False,
                },
                handler=lambda args: inspect_visible_text(snapshot, args.get("limit", 4000)),
            ),
            ToolSpec(
                name="read_previous_state",
                description="Read the previously accepted product state in observe mode.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _: previous_state or {},
            ),
        ]
        try:
            result = await self.runner.run(
                self.spec,
                input_payload={
                    "mode": mode,
                    "user_expression": expression,
                    "known_product": to_primitive(product),
                    "page": {"url": snapshot.url, "title": snapshot.title},
                    "previous_state_available": previous_state is not None,
                },
                tools=tools,
            )
        except (RuntimeError, ValueError):
            # The deterministic source is an operational fallback, not another model opinion.
            parsed = parse_jsonld(snapshot)
            if parsed.get("found"):
                output = {
                    "name": parsed.get("name"),
                    "url": parsed.get("url"),
                    "price": parsed.get("price"),
                    "currency": parsed.get("currency", "CNY"),
                    "availability": parsed.get("availability", "unknown"),
                    "confidence": 0.98,
                    "extraction_method": "jsonld",
                    "evidence": parsed.get("evidence", []),
                    "uncertainties": [],
                }
                result = AgentRunResult(
                    agent_name=self.spec.name,
                    output=output,
                    raw_text="",
                    turns=0,
                    tool_calls=0,
                    trace=[{"fallback": "deterministic_jsonld"}],
                )
            else:
                raise
        board.add(source=self.spec.name, kind="product_hypothesis", payload=result.output)
        return result


class VerifierAgent:
    """Independently review evidence before a write or alert side effect."""

    def __init__(self, runner: AgentRunner, model: str):
        self.runner = runner
        self.spec = AgentSpec(
            name="independent_verifier",
            model=model,
            output_schema=VERIFIER_OUTPUT_SCHEMA,
            max_turns=4,
            max_tool_calls=4,
            instructions="""
You are an independent verifier. Review an action proposal against the raw user
expression, product evidence, and hard constraints. Do not trust another agent's
self-reported confidence. Approve only when the evidence directly supports every
field needed for the side effect. For fuzzy intent, missing entities, conflicting
prices, or uncertain availability, require user confirmation. Never call a write tool.
Before every verdict, you MUST call both read_evidence_board and
check_hard_constraints. For create_alert, the persisted item.policy and previous
item state are the authoritative user instruction; do not demand that a scheduled
job repeat the original user expression. Approve when the deterministic trigger
passes and the new observation matches page evidence. Reject or ask only for a
specific conflict or missing field, never merely because the action has side effects.
The label explicit_purchase includes an explicit price-monitoring or restock command;
the user does not need to literally say "buy". A provided_product_context from the
content script establishes which current-page product the user's command refers to.
The proposal is the application's final canonical candidate; a conflicting
supervisor_decision on the evidence board may be an earlier intermediate judgment.
When target_price is null, the deterministic policy assigns a 10% drop from the
baseline, so target_price is not missing. Treat a promotion/activity reminder as
this default price-drop monitoring policy. A restock alert may be registered while
the item is currently in stock; it will only fire after a future out-of-stock to
in-stock transition, so current availability is not a reason to ask again.

Return JSON only:
{
  "verdict": "approve"|"ask_user"|"reject",
  "confidence": number,
  "reasons": [string], "missing_fields": [string],
  "recommended_question": string|null
}
""".strip(),
        )

    async def verify(
        self,
        *,
        action_type: str,
        proposal: dict[str, Any],
        expression: str,
        board: EvidenceBoard,
    ) -> AgentRunResult:
        evidence_ids_at_start = [entry["id"] for entry in board.snapshot()]
        tools = [
            ToolSpec(
                name="read_evidence_board",
                description="Read raw facts and specialist outputs collected for this task.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _: board.snapshot(),
            ),
            ToolSpec(
                name="check_hard_constraints",
                description="Evaluate non-negotiable requirements for the proposed side effect.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _: self._hard_constraints(action_type, proposal),
            ),
        ]
        result = await self.runner.run(
            self.spec,
            input_payload={
                "action_type": action_type,
                "proposal": proposal,
                "raw_user_expression": expression,
            },
            tools=tools,
        )
        if result.output.get("verdict") not in ("approve", "ask_user", "reject"):
            raise ValueError("Verifier returned an invalid verdict")
        board.add(
            source=self.spec.name,
            kind="verification",
            payload={
                "action_type": action_type,
                "proposal": proposal,
                "evidence_ids_at_start": evidence_ids_at_start,
                "result": result.output,
            },
        )
        return result

    @staticmethod
    def _hard_constraints(action_type: str, proposal: dict[str, Any]) -> dict[str, Any]:
        failures: list[str] = []
        if action_type == "save_item":
            intent = proposal.get("intent", {})
            product = proposal.get("product", {})
            if intent.get("kind") != "explicit_purchase":
                failures.append("intent_not_explicit")
            if float(intent.get("confidence", 0)) < 0.80:
                failures.append("intent_confidence_below_0.80")
            if not product.get("name") or not product.get("url"):
                failures.append("product_entity_incomplete")
            if float(product.get("confidence", 0)) < 0.75:
                failures.append("product_confidence_below_0.75")
        elif action_type == "create_alert":
            if not proposal.get("alert_types"):
                failures.append("no_deterministic_trigger")
        return {"passed": not failures, "failures": failures}


class ShoppingSupervisorAgent:
    """User-facing manager that plans and delegates, but cannot persist anything."""

    def __init__(
        self,
        runner: AgentRunner,
        model: str,
        product_agent: ProductIntelligenceAgent,
        verifier: VerifierAgent,
        repository: JsonRepository,
    ):
        self.runner = runner
        self.product_agent = product_agent
        self.verifier = verifier
        self.repository = repository
        self.rule_intent = IntentAgent()
        self.spec = AgentSpec(
            name="shopping_supervisor",
            model=model,
            output_schema=SUPERVISOR_OUTPUT_SCHEMA,
            max_turns=7,
            max_tool_calls=8,
            instructions="""
You are the Shopping Supervisor. Understand the user's intent, plan the minimum
steps, and dynamically call specialists. You own the conversation but have no write
tool. Classify explicit_purchase vs fuzzy_interest vs unknown. Use the rule tool as
a baseline, not as unquestionable truth. Call Product Intelligence when the product
is missing or uncertain. Before recommending auto_save, call the independent verifier.
For fuzzy interest, ask a concise confirmation question instead of silently saving.

Return JSON only:
{
  "intent": {"kind": string, "confidence": number, "target_price": number|null,
             "notify_restock": boolean, "evidence": [string]},
  "product": {"name": string|null, "url": string|null, "price": number|null,
              "currency": string, "availability": string, "confidence": number,
              "extraction_method": string, "evidence": [string]},
  "recommended_route": "auto_save"|"ask_confirmation"|"ignore",
  "question": string|null,
  "plan_summary": [string]
}
The application will re-check all hard rules after your answer.
""".strip(),
        )

    async def run(
        self,
        *,
        expression: str,
        product: ProductContext,
        snapshot: ProductPageSnapshot,
        board: EvidenceBoard,
    ) -> AgentRunResult:
        async def investigate(args):
            mode = args.get("mode", "identify")
            result = await self.product_agent.run(
                mode=mode,
                expression=expression,
                product=product,
                snapshot=snapshot,
                previous_state=args.get("previous_state"),
                board=board,
            )
            return result.output

        async def verify(args):
            result = await self.verifier.verify(
                action_type=args.get("action_type", "save_item"),
                proposal=args.get("proposal", {}),
                expression=expression,
                board=board,
            )
            return result.output

        tools = [
            ToolSpec(
                name="rule_intent_analysis",
                description="Get a deterministic Chinese intent baseline and extracted threshold.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _: to_primitive(self.rule_intent.classify(expression, product)),
            ),
            ToolSpec(
                name="investigate_product",
                description="Delegate product identification or observation to Product Intelligence.",
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["identify", "observe"]},
                        "previous_state": OBJECT_SCHEMA,
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
                handler=investigate,
            ),
            ToolSpec(
                name="verify_action",
                description="Ask the independent verifier to review a save or alert proposal.",
                parameters={
                    "type": "object",
                    "properties": {
                        "action_type": {"type": "string", "enum": ["save_item", "create_alert"]},
                        "proposal": OBJECT_SCHEMA,
                    },
                    "required": ["action_type", "proposal"],
                    "additionalProperties": False,
                },
                handler=verify,
            ),
            ToolSpec(
                name="read_existing_item",
                description="Check whether the current product URL is already monitored.",
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
                handler=lambda args: to_primitive(self.repository.find_item_by_url(args["url"])),
            ),
        ]
        result = await self.runner.run(
            self.spec,
            input_payload={
                "user_expression": expression,
                "known_product": to_primitive(product),
                "page": {"url": snapshot.url, "title": snapshot.title},
            },
            tools=tools,
        )
        board.add(source=self.spec.name, kind="supervisor_decision", payload=result.output)
        return result
