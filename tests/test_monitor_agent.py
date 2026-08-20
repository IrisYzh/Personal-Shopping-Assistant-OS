import tempfile
import unittest
from pathlib import Path

from shopping_agent.models import (
    Availability,
    Observation,
    ObservationSource,
    ProductContext,
)
from shopping_agent.orchestrator import ShoppingOrchestrator, utc_now
from shopping_agent.storage import JsonRepository


class MonitorAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = JsonRepository(Path(self.temp.name) / "state.json")
        self.orchestrator = ShoppingOrchestrator(repository=self.repository)
        result = self.orchestrator.handle_expression(
            "帮我盯一下，降到650提醒我",
            ProductContext(
                name="外套",
                url="https://shop.example.com/jacket",
                current_price=799,
                availability=Availability.IN_STOCK,
                confidence=0.95,
            ),
        )
        self.item_id = result.item.id

    def tearDown(self):
        self.temp.cleanup()

    def observation(self, price, confidence=0.99, source=ObservationSource.STRUCTURED, evidence=None):
        return Observation(
            price=price,
            availability=Availability.IN_STOCK,
            confidence=confidence,
            source=source,
            checked_at=utc_now(),
            evidence=evidence or ["schema.org/Product.offers.price"],
        )

    def test_price_alert_only_when_crossing_threshold(self):
        above = self.orchestrator.check_item(self.item_id, self.observation(700))
        crossing = self.orchestrator.check_item(self.item_id, self.observation(640))
        still_below = self.orchestrator.check_item(self.item_id, self.observation(630))
        self.assertEqual(above.alert_types, [])
        self.assertEqual(crossing.alert_types, ["price_drop"])
        self.assertEqual(still_below.alert_types, [])

    def test_low_confidence_model_uses_structured_fallback(self):
        decision = self.orchestrator.check_item(
            self.item_id,
            self.observation(640, confidence=0.4, source=ObservationSource.MODEL, evidence=[]),
            self.observation(645),
        )
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.used_fallback)
        self.assertEqual(decision.alert_types, ["price_drop"])

    def test_low_confidence_without_fallback_does_not_update_or_alert(self):
        decision = self.orchestrator.check_item(
            self.item_id,
            self.observation(600, confidence=0.4, source=ObservationSource.MODEL, evidence=[]),
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.state, "unknown")
        self.assertEqual(decision.alert_types, [])

    def test_three_failures_escalate_to_manual_review(self):
        result = None
        for _ in range(3):
            result = self.orchestrator.check_item(
                self.item_id,
                self.observation(600, confidence=0.2, source=ObservationSource.MODEL, evidence=[]),
            )
        self.assertEqual(result.state, "manual_review")
        self.assertGreaterEqual(result.next_check_hours, 24)


if __name__ == "__main__":
    unittest.main()
