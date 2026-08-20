import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from shopping_agent.multiagent.demo import build_demo_provider, call, final
from shopping_agent.multiagent.provider import ScriptedProvider
from shopping_agent.multiagent.system import MultiAgentShoppingSystem
from shopping_agent.multiagent.types import ProductPageSnapshot
from shopping_agent.models import Availability, ProductContext
from shopping_agent.orchestrator import ShoppingOrchestrator
from shopping_agent.storage import JsonRepository


class MultiAgentSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = JsonRepository(Path(self.temp.name) / "state.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_three_agents_delegate_verify_and_commit_once(self):
        provider = build_demo_provider()
        system = MultiAgentShoppingSystem(provider=provider, repository=self.repository)
        html = """
        <script type="application/ld+json">
        {"@type":"Product","name":"羊毛混纺短外套",
         "url":"https://shop.example.com/wool-jacket",
         "offers":{"price":"799","priceCurrency":"CNY",
                   "availability":"https://schema.org/InStock"}}
        </script>
        """
        result = asyncio.run(
            system.handle_expression(
                "帮我盯一下这件外套，降到650提醒我",
                snapshot=ProductPageSnapshot(
                    url="https://shop.example.com/wool-jacket", html=html
                ),
            )
        )
        agents = [request["agent_name"] for request in provider.requests]
        self.assertEqual(result["route"], "auto_save")
        self.assertIn("shopping_supervisor", agents)
        self.assertIn("product_intelligence", agents)
        self.assertIn("independent_verifier", agents)
        self.assertEqual(len(self.repository.list_items()), 1)

    def test_fuzzy_interest_never_calls_write_or_verifier(self):
        provider = ScriptedProvider(
            {
                "shopping_supervisor": [
                    final(
                        "sup_fuzzy",
                        {
                            "intent": {
                                "kind": "fuzzy_interest",
                                "confidence": 0.68,
                                "target_price": None,
                                "notify_restock": False,
                                "evidence": ["有点心动"],
                            },
                            "product": {
                                "name": "复古跑鞋",
                                "url": "https://shop.example.com/shoe",
                                "price": 629,
                                "currency": "CNY",
                                "availability": "in_stock",
                                "confidence": 0.9,
                                "extraction_method": "jsonld",
                                "evidence": ["schema.org/Product"],
                            },
                            "recommended_route": "ask_confirmation",
                            "question": "要把这双鞋加入监控吗？",
                            "plan_summary": ["识别为模糊兴趣，直接询问用户"],
                        },
                    )
                ]
            }
        )
        system = MultiAgentShoppingSystem(provider=provider, repository=self.repository)
        result = asyncio.run(system.handle_expression("这双鞋有点心动"))
        self.assertEqual(result["route"], "ask_confirmation")
        self.assertEqual(self.repository.list_items(), [])
        self.assertEqual([r["agent_name"] for r in provider.requests], ["shopping_supervisor"])

    def test_monitoring_reuses_product_agent_and_verifies_alert(self):
        item = ShoppingOrchestrator(repository=self.repository).handle_expression(
            "帮我盯一下，降到650提醒我",
            ProductContext(
                name="外套",
                url="https://shop.example.com/jacket",
                current_price=799,
                availability=Availability.IN_STOCK,
                confidence=0.98,
            ),
        ).item
        observed = {
            "name": "外套",
            "url": item.url,
            "price": 640,
            "currency": "CNY",
            "availability": "in_stock",
            "confidence": 0.98,
            "extraction_method": "jsonld",
            "evidence": ["schema.org/Product.offers.price"],
            "uncertainties": [],
        }
        provider = ScriptedProvider(
            {
                "product_intelligence": [
                    call("p1", "pc1", "parse_jsonld", {}),
                    final("p2", observed),
                ],
                "independent_verifier": [
                    final(
                        "v1",
                        {
                            "verdict": "approve",
                            "confidence": 0.95,
                            "reasons": ["结构化价格首次越过用户阈值"],
                            "missing_fields": [],
                            "recommended_question": None,
                        },
                    )
                ],
            }
        )
        system = MultiAgentShoppingSystem(provider=provider, repository=self.repository)
        html = """
        <script type="application/ld+json">
        {"@type":"Product","name":"外套","url":"https://shop.example.com/jacket",
         "offers":{"price":"640","priceCurrency":"CNY",
                   "availability":"https://schema.org/InStock"}}
        </script>
        """
        result = asyncio.run(
            system.monitor_item(item.id, snapshot=ProductPageSnapshot(url=item.url, html=html))
        )
        self.assertEqual(result["state"], "alert")
        self.assertEqual(result["alert_types"], ["price_drop"])
        self.assertEqual(result["verification"]["verdict"], "approve")

    def test_changed_proposal_is_verified_again_before_write(self):
        first_intent = {
            "kind": "explicit_purchase",
            "confidence": 0.92,
            "target_price": 650,
            "notify_restock": False,
            "evidence": ["降到650提醒我"],
        }
        changed_intent = {**first_intent, "target_price": 500}
        product = {
            "name": "外套",
            "url": "https://shop.example.com/jacket",
            "price": 799,
            "currency": "CNY",
            "availability": "in_stock",
            "confidence": 0.95,
            "extraction_method": "provided",
            "evidence": ["trusted content-script context"],
        }
        provider = ScriptedProvider(
            {
                "shopping_supervisor": [
                    call(
                        "s1",
                        "sc1",
                        "verify_action",
                        {
                            "action_type": "save_item",
                            "proposal": {"intent": first_intent, "product": product},
                        },
                    ),
                    final(
                        "s2",
                        {
                            "intent": changed_intent,
                            "product": product,
                            "recommended_route": "auto_save",
                            "question": None,
                            "plan_summary": ["changed proposal"],
                        },
                    ),
                ],
                "independent_verifier": [
                    final(
                        "v1",
                        {
                            "verdict": "approve",
                            "confidence": 0.95,
                            "reasons": ["first proposal approved"],
                            "missing_fields": [],
                            "recommended_question": None,
                        },
                    ),
                    final(
                        "v2",
                        {
                            "verdict": "ask_user",
                            "confidence": 0.4,
                            "reasons": ["target price differs from user expression"],
                            "missing_fields": [],
                            "recommended_question": "目标价是650元吗？",
                        },
                    ),
                ],
            }
        )
        system = MultiAgentShoppingSystem(provider=provider, repository=self.repository)
        result = asyncio.run(
            system.handle_expression(
                "降到650提醒我",
                product=ProductContext(
                    name="外套",
                    url=product["url"],
                    current_price=799,
                    availability=Availability.IN_STOCK,
                    confidence=0.95,
                ),
            )
        )
        verifier_calls = [
            request for request in provider.requests if request["agent_name"] == "independent_verifier"
        ]
        self.assertEqual(len(verifier_calls), 2)
        self.assertEqual(result["route"], "ask_confirmation")
        self.assertEqual(result["message"], "目标价是650元吗？")
        self.assertEqual(self.repository.list_items(), [])

    def test_verifier_outage_blocks_alert_and_requests_manual_review(self):
        item = ShoppingOrchestrator(repository=self.repository).handle_expression(
            "降到650提醒我",
            ProductContext(
                name="外套",
                url="https://shop.example.com/jacket",
                current_price=799,
                availability=Availability.IN_STOCK,
                confidence=0.98,
            ),
        ).item
        provider = ScriptedProvider(
            {
                "product_intelligence": [
                    final(
                        "p1",
                        {
                            "name": "外套",
                            "url": item.url,
                            "price": 640,
                            "currency": "CNY",
                            "availability": "in_stock",
                            "confidence": 0.98,
                            "extraction_method": "jsonld",
                            "evidence": ["schema.org/Product.offers.price"],
                            "uncertainties": [],
                        },
                    )
                ]
            }
        )
        system = MultiAgentShoppingSystem(provider=provider, repository=self.repository)
        result = asyncio.run(
            system.monitor_item(
                item.id,
                snapshot=ProductPageSnapshot(url=item.url, html="<html></html>"),
            )
        )
        self.assertEqual(result["state"], "manual_review")
        self.assertEqual(result["alert_types"], [])
        self.assertEqual(result["verification"]["verdict"], "ask_user")

    def test_strong_price_directive_cannot_be_downgraded_to_fuzzy_by_supervisor(self):
        product = {
            "name": "外套",
            "url": "https://shop.example.com/jacket",
            "price": 799,
            "currency": "CNY",
            "availability": "in_stock",
            "confidence": 0.95,
            "extraction_method": "from known product context",
            "evidence": ["trusted content-script context"],
        }
        provider = ScriptedProvider(
            {
                "shopping_supervisor": [
                    final(
                        "s1",
                        {
                            "intent": {
                                "kind": "fuzzy_interest",
                                "confidence": 0.65,
                                "target_price": 700,
                                "notify_restock": False,
                                "evidence": ["model was uncertain"],
                            },
                            "product": product,
                            "recommended_route": "ask_confirmation",
                            "question": "要确认吗？",
                            "plan_summary": ["uncertain"],
                        },
                    )
                ],
                "independent_verifier": [
                    final(
                        "v1",
                        {
                            "verdict": "approve",
                            "confidence": 0.95,
                            "reasons": ["价格阈值是明确指令"],
                            "missing_fields": [],
                            "recommended_question": None,
                        },
                    )
                ],
            }
        )
        system = MultiAgentShoppingSystem(provider=provider, repository=self.repository)
        result = asyncio.run(
            system.handle_expression(
                "低于700通知我",
                product=ProductContext(
                    name="外套", url=product["url"], current_price=799,
                    availability=Availability.IN_STOCK, confidence=0.95,
                ),
            )
        )
        self.assertEqual(result["intent"]["kind"], "explicit_purchase")
        self.assertEqual(result["route"], "auto_save")


if __name__ == "__main__":
    unittest.main()
