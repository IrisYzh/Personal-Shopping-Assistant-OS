"""Pilot features: size-level checks, tracking points, metrics, Bought, email jobs."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from shopping_agent.analytics import compute_metrics, metrics_report
from shopping_agent.jobs import PilotJobs
from shopping_agent.models import Availability
from shopping_agent.page_reader import match_variant, observation_from, parse_html
from shopping_agent.server import ApiError, ShoppingApi, make_handler, parse_pilot_keys
from shopping_agent.tracking import make_token, read_token


def product_page(price=98, sizes=None, name="Align High-Rise Pant 25\""):
    sizes = sizes or {"4": "InStock", "6": "OutOfStock", "16": "InStock"}
    variants = [
        {"@type": "Product", "name": f"{name} - Black - {size}", "size": size, "color": "Black",
         "offers": {"@type": "Offer", "price": str(price), "priceCurrency": "USD", "availability": f"https://schema.org/{stock}"}}
        for size, stock in sizes.items()
    ]
    data = {"@context": "https://schema.org", "@type": "ProductGroup", "name": name, "hasVariant": variants}
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body></body></html>'


PRODUCT = {
    "name": "Align High-Rise Pant 25\"",
    "url": "https://shop.lululemon.com/p/women-pants/Align-Pant-2/_/prod2020012",
    "price": 98,
    "currency": "USD",
    "availability": "in_stock",
    "confidence": 0.95,
}


class FakeSheets:
    enabled = False


def event(api, user, when, kind="view", product=PRODUCT, session="s1"):
    return api.record_event(user, {"event_type": kind, "session_id": session, "occurred_at": when, "product": product})


class PageReaderTests(unittest.TestCase):
    def test_reads_size_level_stock(self):
        product = parse_html(product_page())
        self.assertEqual(product.name, 'Align High-Rise Pant 25"')
        self.assertEqual(product.availability, Availability.IN_STOCK)  # Some size is in stock.
        self.assertEqual(len(product.variants), 3)
        size6 = observation_from(product, size="6", color="Black", checked_at="2026-11-15T00:00:00+00:00")
        self.assertEqual(size6.availability, Availability.OUT_OF_STOCK)
        self.assertEqual(size6.size, "6")
        # "6" must not match "16".
        self.assertEqual([v.size for v in match_variant(product.variants, "6", None)], ["6"])

    def test_unknown_size_falls_back_to_product_level_and_says_so(self):
        product = parse_html(product_page())
        observation = observation_from(product, size="XL", color=None, checked_at="2026-11-15T00:00:00+00:00")
        self.assertIsNone(observation.size)
        self.assertEqual(observation.availability, Availability.IN_STOCK)
        self.assertTrue(any("not listed" in line for line in observation.evidence))

    def test_meta_fallback_and_empty_page(self):
        html = ('<meta property="og:type" content="product"><meta property="og:title" content="Blush">'
                '<meta property="product:price:amount" content="23.00">')
        self.assertEqual(parse_html(html).price, 23.0)
        self.assertIsNone(parse_html("<html><body>Access denied</body></html>"))
        failed = observation_from(None, size=None, color=None, checked_at="x", error="HTTPError: 403")
        self.assertEqual(failed.error, "HTTPError: 403")


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = ShoppingApi(Path(self.temp.name), pilot_keys=parse_pilot_keys("iris:key-for-iris-123,amy:key-for-amy-4567"),
                               sheets=FakeSheets())

    def tearDown(self):
        self.temp.cleanup()

    def events(self, name=None):
        rows = self.api.analytics.events()
        return [row for row in rows if name is None or row["event"] == name]

    def test_prompt_funnel_is_logged_and_counted(self):
        event(self.api, "iris", "2026-11-10T10:00:00+00:00")
        event(self.api, "iris", "2026-11-11T10:00:00+00:00", session="s2")
        third = event(self.api, "iris", "2026-11-12T10:00:00+00:00", session="s3")
        self.assertTrue(third["prompt_now"])
        self.api.confirm("iris", {"pending_id": third["prompt"]["pending_id"], "approved": True, "size": "6", "color": ""})
        self.api.track("iris", {"event": "prompt_rendered", "props": {"pending_id": third["prompt"]["pending_id"]}})

        self.assertEqual(len(self.events("product_view_counted")), 3)
        accepted = self.events("prompt_accepted")[0]
        self.assertTrue(accepted["size_filled"])
        self.assertFalse(accepted["color_filled"])
        metrics = compute_metrics(self.events())
        self.assertEqual(metrics["funnel"]["prompts_shown"], 1)
        self.assertEqual(metrics["rates"]["prompt_accept_rate"], 1.0)
        self.assertEqual(metrics["rates"]["prompt_rate_per_100_views"], 33.3)
        self.assertEqual(metrics["rates"]["size_filled_rate_on_accept"], 1.0)
        self.assertEqual(metrics["rates"]["prompt_render_rate"], 1.0)
        self.assertIn("| iris | 3 | 1 | 1 | 0 |", metrics_report(self.events()))

    def test_cart_click_auto_saves_and_bought_is_a_conversion(self):
        item = event(self.api, "amy", "2026-11-10T10:00:00+00:00", kind="add_to_cart")["item"]
        self.assertEqual(self.events("item_auto_saved")[0]["item_id"], item["id"])
        bought = self.api.set_bought("amy", item["id"], True)["item"]
        self.assertEqual(bought["status"], "bought")
        self.assertTrue(bought["bought_at"])
        self.assertEqual(self.api.list_wishlist("amy")["items"][0]["status"], "bought")
        self.assertEqual(compute_metrics(self.events())["rates"]["bought_per_watched_item"], 1.0)
        # Undo takes it back out of the conversion count.
        self.api.set_bought("amy", item["id"], False)
        self.assertEqual(compute_metrics(self.events())["funnel"]["items_marked_bought"], 0)

    def test_client_events_are_whitelisted(self):
        with self.assertRaises(ApiError):
            self.api.track("iris", {"event": "prompt_accepted"})  # Only the backend may log the core funnel.
        self.api.track("iris", {"event": "site_search", "props": {"results": 0, "query": "zara", "user": "amy", "nested": {"x": 1}}})
        row = self.events("site_search")[0]
        self.assertEqual(row["user"], "iris")
        self.assertNotIn("nested", row)

    def test_visiting_a_watched_page_checks_the_saved_size(self):
        item = event(self.api, "iris", "2026-11-10T10:00:00+00:00", kind="add_to_cart")["item"]
        self.api.update_item("iris", item["id"], {"size": "6"})
        sold_out = {**PRODUCT, "variants": [{"size": "6", "availability": "out_of_stock"}, {"size": "8", "availability": "in_stock"}]}
        event(self.api, "iris", "2026-11-11T10:00:00+00:00", product=sold_out)
        back = {**PRODUCT, "variants": [{"size": "6", "availability": "in_stock"}]}
        event(self.api, "iris", "2026-11-12T10:00:00+00:00", product=back)
        alerts = self.api.context("iris").repository.snapshot()["alerts"]
        self.assertEqual([(a["type"], a["size"]) for a in alerts], [("restock", "6")])
        change = self.events("change_detected")[0]
        self.assertEqual(change["via"], "page_visit")
        self.assertTrue(change["size_level"])

    def test_email_address_is_validated(self):
        with self.assertRaises(ApiError):
            self.api.set_preferences("iris", {"email_address": "not-an-email"})
        saved = self.api.set_preferences("iris", {"email_address": "friend@example.com", "email": {"daily_digest": True}})
        self.assertEqual(saved["preferences"]["email_address"], "friend@example.com")
        self.assertTrue(self.api.get_preferences("iris")["preferences"]["email"]["weekly_digest"])


    def test_test_email_goes_to_saved_address_and_is_not_counted(self):
        with self.assertRaises(ApiError):
            self.api.send_test_email("iris", mailer=lambda *a: None)
        self.api.set_preferences("iris", {"email_address": "iris@example.com"})
        sent = []
        self.assertEqual(self.api.send_test_email("iris", mailer=lambda *a: sent.append(a))["sent_to"], "iris@example.com")
        self.assertEqual(sent[0][0], "iris@example.com")
        self.assertEqual(compute_metrics(self.events())["funnel"]["emails_sent"], 0)


class HttpTrackingTests(unittest.TestCase):
    def test_email_click_redirects_and_metrics_need_admin_key(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ADMIN_KEY": "admin-secret", "TRACKING_SECRET": "s3"}):
            api = ShoppingApi(directory, pilot_keys=parse_pilot_keys("iris:key-for-iris-123"), sheets=FakeSheets())
            item = event(api, "iris", "2026-11-10T10:00:00+00:00", kind="add_to_cart")["item"]
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            try:
                token = make_token("iris", item["id"], "email-1", "weekly_digest")
                self.assertEqual(read_token(token)["email_id"], "email-1")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(f"{base}/r/{token}")
                self.assertEqual(caught.exception.code, 302)
                self.assertEqual(caught.exception.headers["Location"], item["url"])
                forged = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{base}/r/{forged}")
                self.assertEqual(caught.exception.code, 404)

                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{base}/metrics")
                self.assertEqual(caught.exception.code, 401)
                request = urllib.request.Request(f"{base}/metrics", headers={"X-Admin-Key": "admin-secret"})
                with urllib.request.urlopen(request) as response:
                    data = json.loads(response.read())
                self.assertEqual(data["funnel"]["emails_clicked"], 1)
                with urllib.request.urlopen(f"{base}/metrics?format=md&key=admin-secret") as response:
                    self.assertIn("# Pilot metrics", response.read().decode())
            finally:
                server.shutdown()
                server.server_close()


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"PUBLIC_URL": "https://pilot.example.com", "TRACKING_SECRET": "s3",
                                                "CHECK_PAUSE_SECONDS": "0"})
        self.env.start()
        self.api = ShoppingApi(Path(self.temp.name), pilot_keys=parse_pilot_keys("iris:key-for-iris-123,amy:key-for-amy-4567"),
                               sheets=FakeSheets())
        self.sent = []
        self.page = product_page(price=98)
        self.jobs = PilotJobs(self.api, mailer=lambda *args: self.sent.append(args), fetcher=lambda url: self.page)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_price_drop_emails_only_people_who_asked_and_never_twice(self):
        now = datetime.now(timezone.utc).isoformat()
        iris_item = event(self.api, "iris", now, kind="add_to_cart")["item"]
        event(self.api, "amy", now, kind="add_to_cart")
        self.api.set_preferences("iris", {"email_address": "iris@example.com"})
        # Amy turned off price drop emails, so she gets none.
        self.api.set_preferences("amy", {"email_address": "amy@example.com", "email": {"price_drop": False}})

        self.page = product_page(price=69)
        self.assertEqual(self.jobs.check_all()["changes"], 2)
        self.assertEqual(self.jobs.send_instant_alerts(), 1)
        to, subject, body, html = self.sent[0]
        self.assertEqual(to, "iris@example.com")
        self.assertIn("$69.00", subject)
        self.assertIn("https://pilot.example.com/r/", body)
        self.assertNotIn(iris_item["url"], html)  # Links go through the click counter.
        self.assertEqual(self.jobs.send_instant_alerts(), 0)  # Already sent.
        kinds = [row["kind"] for row in self.api.analytics.events() if row["event"] == "email_sent"]
        self.assertEqual(kinds, ["instant_price_drop"])

    def test_bought_items_are_not_checked_and_blocked_sites_count_as_failed_checks(self):
        now = datetime.now(timezone.utc).isoformat()
        item = event(self.api, "iris", now, kind="add_to_cart")["item"]
        self.api.set_bought("iris", item["id"], True)
        self.assertEqual(self.jobs.check_all()["checked"], 0)
        self.api.set_bought("iris", item["id"], False)

        def blocked(url):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

        self.jobs.fetcher = blocked
        self.assertEqual(self.jobs.check_all(), {"checked": 1, "ok": 0, "changes": 0})
        check = [row for row in self.api.analytics.events() if row["event"] == "check_run"][-1]
        self.assertFalse(check["ok"])
        self.assertIn("403", check["error"])

    def test_schedule_runs_daily_and_saturday_summaries_once(self):
        when = "2026-11-14T16:30:00+00:00"  # Saturday 8:30 AM in Los Angeles.
        item = event(self.api, "iris", when, kind="add_to_cart")["item"]
        self.api.set_preferences("iris", {"email_address": "iris@example.com", "email": {"daily_digest": True, "price_drop": False}})
        self.api.context("iris").repository.append_alert({
            "id": "a1", "item_id": item["id"], "type": "price_drop", "old_price": 98, "new_price": 69,
            "created_at": "2026-11-14T10:00:00+00:00", "dedupe_key": "a1",
        })
        early = self.jobs.tick(datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc))
        self.assertNotIn("weekly", early)  # Before 9 AM local time.
        ran = self.jobs.tick(datetime(2026, 11, 14, 17, 5, tzinfo=timezone.utc))
        self.assertEqual((ran["daily"], ran["weekly"]), (1, 1))
        again = self.jobs.tick(datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc))
        self.assertNotIn("daily", again)
        self.assertNotIn("weekly", again)
        subjects = [args[1] for args in self.sent]
        self.assertIn("1 item on your wishlist changed today", subjects)
        self.assertIn("1 item on your wishlist changed this week", subjects)

    def test_no_address_means_no_email(self):
        now = datetime.now(timezone.utc).isoformat()
        event(self.api, "iris", now, kind="add_to_cart")
        self.page = product_page(price=69)
        self.jobs.check_all()
        self.assertEqual(self.jobs.send_instant_alerts(), 0)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
