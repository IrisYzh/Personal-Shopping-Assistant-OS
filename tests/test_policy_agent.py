import unittest

from shopping_agent.intent_agent import IntentAgent
from shopping_agent.models import Availability, ProductContext
from shopping_agent.policy_agent import PolicyAgent


class PolicyAgentTests(unittest.TestCase):
    def setUp(self):
        self.intent = IntentAgent()
        self.policy = PolicyAgent()
        self.product = ProductContext(
            name="商品",
            url="https://shop.example.com/item",
            current_price=1000,
            availability=Availability.IN_STOCK,
            confidence=0.95,
        )

    def test_restock_policy_checks_every_six_hours(self):
        decision = self.intent.classify("补货提醒", self.product)
        policy = self.policy.build(decision, self.product)
        self.assertEqual(policy.frequency_hours, 6)
        self.assertTrue(policy.notify_restock)

    def test_target_price_policy_checks_daily(self):
        decision = self.intent.classify("降到800提醒我", self.product)
        policy = self.policy.build(decision, self.product)
        self.assertEqual(policy.frequency_hours, 24)
        self.assertEqual(policy.target_price, 800)
        self.assertEqual(policy.source, "user_threshold")

    def test_default_policy_is_ten_percent_and_seventy_two_hours(self):
        decision = self.intent.classify("我想买这个", self.product)
        policy = self.policy.build(decision, self.product)
        self.assertEqual(policy.frequency_hours, 72)
        self.assertEqual(policy.drop_percent, 0.10)
        self.assertEqual(policy.source, "deterministic_default_10_percent")


if __name__ == "__main__":
    unittest.main()
