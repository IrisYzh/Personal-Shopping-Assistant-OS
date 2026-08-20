import unittest

from shopping_agent.intent_agent import IntentAgent
from shopping_agent.models import Availability, IntentKind, ProductContext, Route


PRODUCT = ProductContext(
    name="测试商品",
    url="https://shop.example.com/item",
    current_price=799,
    availability=Availability.IN_STOCK,
    confidence=0.92,
)


class IntentAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = IntentAgent()

    def test_explicit_intent_with_resolved_product_auto_saves(self):
        decision = self.agent.classify("帮我盯一下，降到 650 提醒我", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.EXPLICIT_PURCHASE)
        self.assertEqual(decision.route, Route.AUTO_SAVE)
        self.assertEqual(decision.target_price, 650)

    def test_fuzzy_interest_always_asks_before_persisting(self):
        decision = self.agent.classify("这个有点心动，先看看", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.FUZZY_INTEREST)
        self.assertEqual(decision.route, Route.ASK_CONFIRMATION)

    def test_explicit_intent_missing_product_asks_confirmation(self):
        decision = self.agent.classify("我想买这个")
        self.assertEqual(decision.route, Route.ASK_CONFIRMATION)
        self.assertIn("product_url", decision.missing_fields)

    def test_negative_expression_is_not_saved(self):
        decision = self.agent.classify("算了，我不想买这个了", PRODUCT)
        self.assertEqual(decision.route, Route.IGNORE)

    def test_price_directive_is_explicit_without_buy_phrase(self):
        decision = self.agent.classify("降到 650 提醒我", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.EXPLICIT_PURCHASE)
        self.assertEqual(decision.route, Route.AUTO_SAVE)

    def test_restock_directive_is_explicit(self):
        decision = self.agent.classify("补货提醒", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.EXPLICIT_PURCHASE)
        self.assertTrue(decision.notify_restock)

    def test_expanded_trigger_paraphrase_is_recalled(self):
        decision = self.agent.classify("替我留意价格，跌破650通知我", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.EXPLICIT_PURCHASE)
        self.assertEqual(decision.route, Route.AUTO_SAVE)
        self.assertEqual(decision.target_price, 650)

    def test_expanded_fuzzy_phrase_still_requires_confirmation(self):
        decision = self.agent.classify("纠结要不要买", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.FUZZY_INTEREST)
        self.assertEqual(decision.route, Route.ASK_CONFIRMATION)

    def test_expanded_negative_phrase_is_ignored(self):
        decision = self.agent.classify("已经买过了，不需要提醒", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.UNKNOWN)
        self.assertEqual(decision.route, Route.IGNORE)

    def test_informational_question_is_ignored_without_prompting(self):
        decision = self.agent.classify("这件是什么材质", PRODUCT)
        self.assertEqual(decision.kind, IntentKind.UNKNOWN)
        self.assertEqual(decision.route, Route.IGNORE)


if __name__ == "__main__":
    unittest.main()
