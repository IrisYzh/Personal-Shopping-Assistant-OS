import tempfile
import unittest
from pathlib import Path

from shopping_agent.behavior_intent import BehaviorIntentAgent, parse_time
from shopping_agent.models import (
    Availability,
    BehaviorEvent,
    BehaviorEventType,
    IntentKind,
    Observation,
    ObservationSource,
    ProductContext,
    Route,
)
from shopping_agent.orchestrator import ShoppingOrchestrator
from shopping_agent.multiagent.product_tools import parse_jsonld
from shopping_agent.multiagent.types import ProductPageSnapshot
from shopping_agent.storage import JsonRepository


class BehaviorIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = JsonRepository(Path(self.temp.name) / "state.json")
        self.orchestrator = ShoppingOrchestrator(repository=self.repository)
        self.product = ProductContext(
            name="复古跑鞋",
            url="https://shop.example.com/retro-runner",
            current_price=629,
            availability=Availability.IN_STOCK,
            confidence=0.95,
        )

    def tearDown(self):
        self.temp.cleanup()

    def record(self, event_type, session_id, occurred_at):
        return self.orchestrator.record_behavior(
            event_type,
            self.product,
            session_id=session_id,
            occurred_at=occurred_at,
        )

    def test_low_frequency_view_stays_silent(self):
        result = self.record(BehaviorEventType.VIEW, "session-a", "2026-09-01T10:00:00+00:00")
        self.assertEqual(result.route, Route.IGNORE)
        self.assertEqual(result.intent.kind, IntentKind.UNKNOWN)
        self.assertEqual(self.repository.list_items(), [])

    def test_refreshes_in_one_session_do_not_create_fuzzy_interest(self):
        result = None
        for hour in (10, 11, 12):
            result = self.record(
                BehaviorEventType.VIEW,
                "same-session",
                f"2026-09-01T{hour}:00:00+00:00",
            )
        self.assertEqual(result.route, Route.IGNORE)
        self.assertEqual(result.behavior_summary.view_count, 3)
        self.assertEqual(result.behavior_summary.distinct_session_count, 1)

    def test_third_cross_session_view_prompts_on_the_page(self):
        self.record(BehaviorEventType.VIEW, "session-a", "2026-09-01T10:00:00+00:00")
        self.record(BehaviorEventType.VIEW, "session-a", "2026-09-01T11:00:00+00:00")
        result = self.record(BehaviorEventType.VIEW, "session-b", "2026-09-02T10:00:00+00:00")
        self.assertEqual(result.route, Route.ASK_CONFIRMATION)
        self.assertEqual(result.intent.kind, IntentKind.FUZZY_INTEREST)
        self.assertTrue(result.prompt_now)
        self.assertFalse(result.deferred_to_daily_digest)
        self.assertIn("尺码", result.pending.question)
        self.assertEqual(len(self.repository.snapshot()["pending_confirmations"]), 1)

    def test_each_product_is_asked_about_only_once(self):
        for session, at in (("a", "2026-09-01T10:00:00+00:00"), ("a", "2026-09-01T11:00:00+00:00"),
                            ("b", "2026-09-02T10:00:00+00:00")):
            first = self.record(BehaviorEventType.VIEW, session, at)
        self.orchestrator.confirm(first.pending.id, False)
        again = self.record(BehaviorEventType.VIEW, "c", "2026-09-03T10:00:00+00:00")
        self.assertEqual(again.route, Route.IGNORE)
        self.assertFalse(again.prompt_now)
        self.assertEqual(self.repository.list_items(), [])

    def test_at_most_one_prompt_per_day(self):
        other = ProductContext(
            name="Align 高腰裤", url="https://shop.example.com/align", current_price=98,
            availability=Availability.IN_STOCK, confidence=0.95,
        )
        for session, at in (("a", "2026-09-01T10:00:00+00:00"), ("a", "2026-09-01T11:00:00+00:00")):
            self.record(BehaviorEventType.VIEW, session, at)
            self.orchestrator.record_behavior(BehaviorEventType.VIEW, other, session_id=session, occurred_at=at)
        first = self.record(BehaviorEventType.VIEW, "b", "2026-09-02T10:00:00+00:00")
        second = self.orchestrator.record_behavior(
            BehaviorEventType.VIEW, other, session_id="b", occurred_at="2026-09-02T10:05:00+00:00"
        )
        self.assertTrue(first.prompt_now)
        self.assertFalse(second.prompt_now)
        self.assertTrue(second.deferred_to_daily_digest)

    def test_prompt_confirmation_saves_size_and_color(self):
        for session, at in (("a", "2026-09-01T10:00:00+00:00"), ("a", "2026-09-01T11:00:00+00:00"),
                            ("b", "2026-09-02T10:00:00+00:00")):
            result = self.record(BehaviorEventType.VIEW, session, at)
        confirmed = self.orchestrator.confirm(result.pending.id, True, size=" M ", color="Black")
        self.assertEqual(confirmed.item.size, "M")
        self.assertEqual(confirmed.item.color, "Black")
        self.assertEqual(confirmed.item.added_via, "prompt")
        self.assertTrue(confirmed.item.policy.notify_restock)
        stored = self.repository.get_item(confirmed.item.id)
        self.assertEqual((stored.size, stored.color), ("M", "Black"))

    def test_add_to_cart_creates_default_price_stock_and_promotion_monitoring(self):
        result = self.record(
            BehaviorEventType.ADD_TO_CART,
            "session-a",
            "2026-09-01T10:00:00+00:00",
        )
        self.assertEqual(result.route, Route.AUTO_SAVE)
        self.assertEqual(result.intent.kind, IntentKind.EXPLICIT_PURCHASE)
        self.assertEqual(result.item.policy.drop_percent, 0.10)
        self.assertTrue(result.item.policy.notify_restock)
        self.assertTrue(result.item.policy.notify_promotions)
        self.assertEqual(result.item.policy.frequency_hours, 6)

    def test_add_to_wishlist_is_an_explicit_signal(self):
        result = self.record(
            BehaviorEventType.ADD_TO_WISHLIST,
            "session-a",
            "2026-09-01T10:00:00+00:00",
        )
        self.assertEqual(result.route, Route.AUTO_SAVE)
        self.assertIn("behavior:add_to_wishlist", result.intent.signals)

    def test_remove_action_cancels_the_strong_signal(self):
        agent = BehaviorIntentAgent()
        raw = [
            (BehaviorEventType.ADD_TO_CART, "s1", "2026-09-01T09:00:00+00:00"),
            (BehaviorEventType.REMOVE_FROM_CART, "s1", "2026-09-01T10:00:00+00:00"),
            (BehaviorEventType.VIEW, "s1", "2026-09-01T11:00:00+00:00"),
            (BehaviorEventType.VIEW, "s1", "2026-09-01T12:00:00+00:00"),
            (BehaviorEventType.VIEW, "s2", "2026-09-02T11:00:00+00:00"),
        ]
        events = [
            BehaviorEvent(str(index), kind, self.product, session, occurred_at)
            for index, (kind, session, occurred_at) in enumerate(raw)
        ]
        decision, summary = agent.classify(
            events, self.product, now=parse_time("2026-09-02T12:00:00+00:00")
        )
        self.assertFalse(summary.cart_active)
        self.assertEqual(decision.kind, IntentKind.FUZZY_INTEREST)

    def test_daily_digest_asks_once_and_confirmation_activates_monitoring(self):
        # Simulate the daily popup cap already being used, so this product goes to the digest.
        self.repository.append_prompt_record(
            {"id": "x", "product_url": "https://shop.example.com/other", "prompt_date": "2026-09-02"}
        )
        self.record(BehaviorEventType.VIEW, "session-a", "2026-09-01T10:00:00+00:00")
        self.record(BehaviorEventType.VIEW, "session-a", "2026-09-01T11:00:00+00:00")
        deferred = self.record(BehaviorEventType.VIEW, "session-b", "2026-09-02T10:00:00+00:00")
        self.assertTrue(deferred.deferred_to_daily_digest)

        prompts = self.orchestrator.build_daily_interest_digest(
            now="2026-09-02T20:00:00+00:00"
        )
        repeated = self.orchestrator.build_daily_interest_digest(
            now="2026-09-02T21:00:00+00:00"
        )
        self.assertEqual(len(prompts), 1)
        self.assertEqual(repeated, [])
        self.assertIn("浏览了“复古跑鞋” 3 次", prompts[0].question)
        self.assertIn("希望降到多少元", prompts[0].question)

        confirmed = self.orchestrator.confirm(prompts[0].id, True, target_price=520)
        self.assertEqual(confirmed.item.intent_kind, IntentKind.EXPLICIT_PURCHASE)
        self.assertEqual(confirmed.item.policy.target_price, 520)
        self.assertTrue(confirmed.item.policy.notify_restock)
        self.assertTrue(confirmed.item.policy.notify_promotions)

    def test_promotion_change_alerts_once(self):
        item = self.record(
            BehaviorEventType.ADD_TO_CART,
            "session-a",
            "2026-09-01T10:00:00+00:00",
        ).item
        first = Observation(
            price=629,
            availability=Availability.IN_STOCK,
            confidence=0.99,
            source=ObservationSource.STRUCTURED,
            checked_at="2026-09-02T10:00:00+00:00",
            promotion_text="满 500 减 80",
            evidence=["schema.org/Product.offers.description"],
        )
        repeated = Observation(
            price=629,
            availability=Availability.IN_STOCK,
            confidence=0.99,
            source=ObservationSource.STRUCTURED,
            checked_at="2026-09-02T16:00:00+00:00",
            promotion_text="满 500 减 80",
            evidence=["schema.org/Product.offers.description"],
        )
        self.assertEqual(self.orchestrator.check_item(item.id, first).alert_types, ["promotion"])
        self.assertEqual(self.orchestrator.check_item(item.id, repeated).alert_types, [])

    def test_jsonld_only_treats_promotion_language_as_a_promotion(self):
        ordinary = ProductPageSnapshot(
            url=self.product.url,
            html='''<script type="application/ld+json">{
              "@type":"Product", "name":"复古跑鞋",
              "offers":{"price":"629", "description":"轻量透气鞋面"}
            }</script>''',
        )
        promotion = ProductPageSnapshot(
            url=self.product.url,
            html='''<script type="application/ld+json">{
              "@type":"Product", "name":"复古跑鞋",
              "offers":{"price":"629", "description":"限时活动，满500减80"}
            }</script>''',
        )
        self.assertIsNone(parse_jsonld(ordinary)["promotion_text"])
        self.assertEqual(parse_jsonld(promotion)["promotion_text"], "限时活动，满500减80")


if __name__ == "__main__":
    unittest.main()
