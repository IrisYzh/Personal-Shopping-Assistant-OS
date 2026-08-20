from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..storage import JsonRepository
from .provider import ScriptedProvider
from .system import MultiAgentShoppingSystem
from .types import FunctionCall, ModelResponse, ProductPageSnapshot


DEMO_PRODUCT = {
    "name": "羊毛混纺短外套",
    "url": "https://shop.example.com/wool-jacket",
    "price": 799,
    "currency": "CNY",
    "availability": "in_stock",
    "confidence": 0.98,
    "extraction_method": "jsonld",
    "evidence": ["schema.org/Product", "schema.org/Product.offers"],
    "uncertainties": [],
}
DEMO_INTENT = {
    "kind": "explicit_purchase",
    "confidence": 0.92,
    "target_price": 650,
    "notify_restock": False,
    "evidence": ["帮我盯一下", "降到650提醒我"],
}


def call(response_id: str, call_id: str, name: str, arguments: dict) -> ModelResponse:
    return ModelResponse(
        id=response_id,
        function_calls=[
            FunctionCall(call_id=call_id, name=name, arguments=json.dumps(arguments, ensure_ascii=False))
        ],
    )


def final(response_id: str, payload: dict) -> ModelResponse:
    return ModelResponse(id=response_id, output_text=json.dumps(payload, ensure_ascii=False))


def build_demo_provider() -> ScriptedProvider:
    proposal = {"intent": DEMO_INTENT, "product": DEMO_PRODUCT}
    return ScriptedProvider(
        {
            "shopping_supervisor": [
                call("sup_1", "sup_call_1", "rule_intent_analysis", {}),
                call("sup_2", "sup_call_2", "investigate_product", {"mode": "identify"}),
                call(
                    "sup_3",
                    "sup_call_3",
                    "verify_action",
                    {"action_type": "save_item", "proposal": proposal},
                ),
                final(
                    "sup_4",
                    {
                        "intent": DEMO_INTENT,
                        "product": DEMO_PRODUCT,
                        "recommended_route": "auto_save",
                        "question": None,
                        "plan_summary": [
                            "调用规则意图基线",
                            "委派 Product Intelligence 解析商品",
                            "委派 Independent Verifier 复核写入",
                        ],
                    },
                ),
            ],
            "product_intelligence": [
                call("product_1", "product_call_1", "parse_jsonld", {}),
                final("product_2", DEMO_PRODUCT),
            ],
            "independent_verifier": [
                call("verify_1", "verify_call_1", "read_evidence_board", {}),
                call("verify_2", "verify_call_2", "check_hard_constraints", {}),
                final(
                    "verify_3",
                    {
                        "verdict": "approve",
                        "confidence": 0.96,
                        "reasons": ["意图明确", "商品来自结构化证据", "目标价由用户提供"],
                        "missing_fields": [],
                        "recommended_question": None,
                    },
                ),
            ],
        }
    )


async def run_scripted_demo() -> dict:
    html = """
    <html><head><title>羊毛混纺短外套</title>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product",
     "name":"羊毛混纺短外套","url":"https://shop.example.com/wool-jacket",
     "offers":{"@type":"Offer","price":"799","priceCurrency":"CNY",
               "availability":"https://schema.org/InStock"}}
    </script></head><body>羊毛混纺短外套 ¥799</body></html>
    """
    provider = build_demo_provider()
    with tempfile.TemporaryDirectory(prefix="shopping-multiagent-demo-") as directory:
        repository = JsonRepository(Path(directory) / "state.json")
        system = MultiAgentShoppingSystem(provider=provider, repository=repository)
        result = await system.handle_expression(
            "帮我盯一下这件外套，降到650提醒我",
            snapshot=ProductPageSnapshot(
                url="https://shop.example.com/wool-jacket",
                title="羊毛混纺短外套",
                html=html,
            ),
        )
        state = repository.snapshot()
        return {
            "result": result,
            "agent_invocations": [request["agent_name"] for request in provider.requests],
            "final_state": {
                "items": len(state["items"]),
                "pending_confirmations": len(state["pending_confirmations"]),
                "audit_events": len(state["audit_log"]),
            },
        }
