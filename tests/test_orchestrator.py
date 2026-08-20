import tempfile
import unittest
from pathlib import Path

from shopping_agent.models import Availability, ProductContext, Route
from shopping_agent.orchestrator import ShoppingOrchestrator
from shopping_agent.storage import JsonRepository


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = JsonRepository(Path(self.temp.name) / "state.json")
        self.orchestrator = ShoppingOrchestrator(repository=self.repository)
        self.product = ProductContext(
            name="复古跑鞋",
            url="https://shop.example.com/shoe",
            current_price=629,
            availability=Availability.IN_STOCK,
            confidence=0.95,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_fuzzy_interest_only_persists_after_confirmation(self):
        first = self.orchestrator.handle_expression("这双鞋有点心动", self.product)
        self.assertEqual(first.route, Route.ASK_CONFIRMATION)
        self.assertEqual(self.repository.list_items(), [])

        confirmed = self.orchestrator.confirm(first.pending.id, True, target_price=550)
        self.assertEqual(confirmed.route, Route.AUTO_SAVE)
        self.assertEqual(len(self.repository.list_items()), 1)
        self.assertEqual(confirmed.item.policy.target_price, 550)

    def test_decline_never_writes_item(self):
        first = self.orchestrator.handle_expression("这双鞋挺喜欢", self.product)
        result = self.orchestrator.confirm(first.pending.id, False)
        self.assertEqual(result.route, Route.IGNORE)
        self.assertEqual(self.repository.list_items(), [])

    def test_duplicate_url_is_idempotent(self):
        first = self.orchestrator.handle_expression("我想买，降到500提醒我", self.product)
        second = self.orchestrator.handle_expression("帮我监控，降到480提醒我", self.product)
        self.assertEqual(first.item.id, second.item.id)
        self.assertEqual(len(self.repository.list_items()), 1)


if __name__ == "__main__":
    unittest.main()
