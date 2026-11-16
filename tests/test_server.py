import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from shopping_agent.digest import build_weekly_digest
from shopping_agent.server import ApiError, ShoppingApi, make_handler, parse_pilot_keys, search_sites


class FakeSheets:
    def __init__(self):
        self.enabled = True
        self.calls = []

    def upsert(self, item, *, user=None):
        self.calls.append(("upsert", user, item.id, item.size))
        return True

    def remove(self, item_id):
        self.calls.append(("remove", item_id))
        return True


ALIGN = {
    "name": "Align High-Rise Pant 25\"",
    "url": "https://shop.lululemon.com/p/women-pants/Align-Pant-2/_/prod2020012?color=0001&sz=4",
    "price": 98,
    "currency": "USD",
    "availability": "out_of_stock",
    "confidence": 0.95,
}


def view(api, user, session, when, product=ALIGN, event="view"):
    return api.record_event(
        user, {"event_type": event, "session_id": session, "occurred_at": when, "product": product}
    )


class ShoppingApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sheets = FakeSheets()
        self.api = ShoppingApi(
            Path(self.temp.name),
            pilot_keys=parse_pilot_keys("iris:key-for-iris-123,amy:key-for-amy-4567"),
            sheets=self.sheets,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_third_view_prompts_then_confirm_saves_size_and_syncs_sheet(self):
        self.assertFalse(view(self.api, "iris", "s1", "2026-11-10T10:00:00+00:00")["prompt_now"])
        self.assertFalse(view(self.api, "iris", "s1", "2026-11-10T10:05:00+00:00")["prompt_now"])
        third = view(self.api, "iris", "s2", "2026-11-12T20:00:00+00:00")
        self.assertTrue(third["prompt_now"])
        self.assertEqual(third["prompt"]["site"], "Lululemon")
        self.assertEqual(third["prompt"]["view_count"], 3)

        saved = self.api.confirm("iris", {"pending_id": third["prompt"]["pending_id"], "approved": True, "size": "6", "color": "Black"})
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["item"]["size"], "6")
        self.assertEqual(saved["item"]["added_via"], "prompt")
        # Tracking query params are dropped so one product stays one item.
        self.assertEqual(saved["item"]["url"], "https://shop.lululemon.com/p/women-pants/Align-Pant-2/_/prod2020012")
        self.assertEqual(self.sheets.calls[-1][:2], ("upsert", "iris"))

        # Asked once: a fourth view does not prompt again.
        self.assertFalse(view(self.api, "iris", "s3", "2026-11-13T09:00:00+00:00")["prompt_now"])

    def test_add_to_cart_saves_without_popup(self):
        result = view(self.api, "iris", "s1", "2026-11-10T10:00:00+00:00", event="add_to_cart")
        self.assertTrue(result["saved"])
        self.assertFalse(result["prompt_now"])
        self.assertEqual(result["item"]["added_via"], "add_to_cart")

    def test_users_are_isolated(self):
        view(self.api, "iris", "s1", "2026-11-10T10:00:00+00:00", event="add_to_wishlist")
        self.assertEqual(len(self.api.list_wishlist("iris")["items"]), 1)
        self.assertEqual(self.api.list_wishlist("amy")["items"], [])

    def test_unsupported_sites_are_rejected(self):
        other = dict(ALIGN, url="https://www.amazon.com/dp/B000")
        with self.assertRaises(ApiError):
            view(self.api, "iris", "s1", "2026-11-10T10:00:00+00:00", product=other)

    def test_edit_delete_and_clear_history(self):
        item = view(self.api, "iris", "s1", "2026-11-10T10:00:00+00:00", event="add_to_cart")["item"]
        updated = self.api.update_item("iris", item["id"], {"size": "M"})
        self.assertEqual(updated["item"]["size"], "M")
        self.assertEqual(self.api.clear_history("iris")["removed_events"], 1)
        self.assertTrue(self.api.delete_item("iris", item["id"])["removed"])
        self.assertEqual(self.sheets.calls[-1], ("remove", item["id"]))
        with self.assertRaises(ApiError):
            self.api.delete_item("iris", item["id"])

    def test_old_history_is_pruned_after_30_days(self):
        view(self.api, "iris", "s1", "2026-09-01T10:00:00+00:00")
        view(self.api, "iris", "s2", "2026-11-10T10:00:00+00:00")
        events = self.api.context("iris").repository.list_behavior_events()
        self.assertEqual(len(events), 1)

    def test_email_choices_are_saved_per_user(self):
        saved = self.api.set_preferences("amy", {"email": {"daily_digest": True, "sale_start": True}})["preferences"]["email"]
        self.assertEqual(saved, {"weekly_digest": True, "daily_digest": True, "size_back": True, "price_drop": True, "sale_start": True})
        self.assertEqual(self.api.context("iris").repository.get_preferences(), {})
        with self.assertRaises(ApiError):
            self.api.set_preferences("amy", {"email": {"hourly": True}})
        with self.assertRaises(ApiError):
            self.api.set_preferences("amy", {"email": {"weekly_digest": "yes"}})

    def test_site_search_suggests_supported_sites_only(self):
        self.assertEqual([s["name"] for s in search_sites("Lulu")], ["Lululemon"])
        self.assertEqual([s["name"] for s in search_sites("nord")], ["Nordstrom"])
        self.assertEqual([s["name"] for s in search_sites("sephora.com")], ["Sephora"])
        self.assertEqual(search_sites("zara"), [])
        self.assertEqual(search_sites("  "), [])

    def test_unknown_key_is_refused(self):
        with self.assertRaises(ApiError):
            self.api.authenticate("wrong")
        self.assertEqual(self.api.authenticate("key-for-amy-4567"), "amy")


class HttpTests(unittest.TestCase):
    def test_http_round_trip_with_key(self):
        with tempfile.TemporaryDirectory() as directory:
            api = ShoppingApi(directory, pilot_keys=parse_pilot_keys("iris:key-for-iris-123"), sheets=FakeSheets())
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                body = json.dumps({"event_type": "add_to_wishlist", "session_id": "s1", "product": ALIGN}).encode()
                request = urllib.request.Request(
                    f"{base}/events", data=body, method="POST",
                    headers={"Content-Type": "application/json", "X-Pilot-Key": "key-for-iris-123"},
                )
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.loads(response.read())["saved"])
                    self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
                request = urllib.request.Request(f"{base}/wishlist")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 401)
                with urllib.request.urlopen(f"{base}/health") as response:
                    self.assertTrue(json.loads(response.read())["ok"])
            finally:
                server.shutdown()
                server.server_close()


class WeeklyDigestTests(unittest.TestCase):
    def test_digest_explains_each_change_and_skips_quiet_weeks(self):
        with tempfile.TemporaryDirectory() as directory:
            api = ShoppingApi(directory, pilot_keys={}, sheets=FakeSheets())
            item = view(api, "me", "s1", "2026-11-10T10:00:00+00:00", event="add_to_cart")["item"]
            api.update_item("me", item["id"], {"size": "6"})
            repository = api.context("me").repository
            self.assertIsNone(build_weekly_digest(repository, now="2026-11-14T09:00:00+00:00"))

            repository.append_alert({
                "id": "a1", "item_id": item["id"], "type": "restock", "old_price": 98, "new_price": 98, "size": "6",
                "availability": "in_stock", "created_at": "2026-11-12T08:00:00+00:00", "dedupe_key": "a1",
            })
            repository.append_alert({
                "id": "a2", "item_id": item["id"], "type": "price_drop", "old_price": 98, "new_price": 69,
                "availability": "in_stock", "created_at": "2026-11-13T08:00:00+00:00", "dedupe_key": "a2",
            })
            digest = build_weekly_digest(repository, now="2026-11-14T09:00:00+00:00")
            self.assertEqual(digest.change_count, 1)
            self.assertIn("Back in stock in size 6.", digest.body)
            self.assertIn("Price dropped from $98.00 to $69.00, 30% off.", digest.body)
            self.assertEqual(digest.subject, "1 item on your wishlist changed this week")
            # The HTML part highlights the change itself.
            self.assertIn("Back in stock in size 6</strong>", digest.html)
            self.assertIn("line-through\">$98.00</span>", digest.html)
            self.assertIn(">$69.00</strong>", digest.html)
            self.assertIn("30% off</strong>", digest.html)
            # A week later the same alerts are old news.
            self.assertIsNone(build_weekly_digest(repository, now="2026-11-28T09:00:00+00:00"))


if __name__ == "__main__":
    unittest.main()
