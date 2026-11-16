"""Claude page reading: code gates on the model's answer, and the second-model check before alerts."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shopping_agent import ai_reader
from shopping_agent.ai_reader import ClaudePageReader, page_text
from shopping_agent.analytics import compute_metrics, metrics_report
from shopping_agent.jobs import PilotJobs
from shopping_agent.server import ShoppingApi, parse_pilot_keys

PLAIN_PAGE = """<html><head><title>Wunder Puff Jacket</title><script>var x = "$1.00";</script></head>
<body><nav>Women Men Sale</nav><h1>Wunder Puff Jacket</h1>
<div class="price"><span>Sale price</span> <span>$149.00</span> <s>$198.00</s></div>
<ul class="sizes"><li>Size 4 - Sold out</li><li>Size 6 - In stock</li><li>Size 16 - In stock</li></ul>
<button>Add to Bag</button><p>Free shipping on orders over $50</p></body></html>"""


class FakeClient:
    """Stands in for anthropic.Anthropic: returns scripted tool calls, records requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **params):
        self.requests.append(params)
        answer = self.answers.pop(0)
        block = SimpleNamespace(type="tool_use", name=params["tool_choice"]["name"], input=answer, id="t1")
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=80))


GOOD_READ = {
    "is_product_page": True, "name": "Wunder Puff Jacket", "price": 149.0, "currency": "USD",
    "price_quote": "Sale price $149.00", "availability": "in_stock", "stock_quote": "Add to Bag",
    "size_listed": True, "size_availability": "in_stock", "size_quote": "Size 6 - In stock",
    "promotion_text": None, "promotion_quote": None,
}


class FakeSheets:
    enabled = False


class GateTests(unittest.TestCase):
    def test_page_text_drops_scripts_and_keeps_order(self):
        text = page_text(PLAIN_PAGE)
        self.assertNotIn("var x", text)
        self.assertLess(text.index("Wunder Puff Jacket"), text.index("$149.00"))

    def test_values_with_real_quotes_are_kept(self):
        client = FakeClient(GOOD_READ)
        reading = ClaudePageReader(client).read(PLAIN_PAGE, url="https://shop.lululemon.com/p/x", size="6")
        self.assertEqual(reading.product.price, 149.0)
        self.assertEqual(reading.product.variants[0].size, "6")
        self.assertEqual(reading.product.confidence, 0.8)
        self.assertEqual(len(reading.evidence), 3)
        self.assertEqual(client.requests[0]["model"], "claude-haiku-5-5")
        self.assertIn("<page_text>", client.requests[0]["messages"][0]["content"])

    def test_made_up_values_are_thrown_away(self):
        invented = {**GOOD_READ, "price": 99.0, "price_quote": "Now $99.00",  # not on the page
                    "size_quote": "Size 16 - In stock",  # real text, but for size 16, not 6
                    "promotion_text": "40% off", "promotion_quote": "40% off everything"}
        reading = ClaudePageReader(FakeClient(invented)).read(PLAIN_PAGE, url="u", size="6")
        self.assertIsNone(reading.product.price)
        self.assertEqual(reading.product.variants, [])
        self.assertIsNone(reading.product.promotion_text)
        self.assertEqual(len(reading.dropped), 3)

    def test_price_quote_must_contain_the_price(self):
        wrong = {**GOOD_READ, "price": 198.0, "price_quote": "Sale price $149.00"}
        reading = ClaudePageReader(FakeClient(wrong)).read(PLAIN_PAGE, url="u")
        self.assertIsNone(reading.product.price)

    def test_not_a_product_page(self):
        reading = ClaudePageReader(FakeClient({**GOOD_READ, "is_product_page": False})).read(PLAIN_PAGE, url="u")
        self.assertIsNone(reading.product)

    def test_off_without_a_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(ai_reader.enabled())
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "AI_PAGE_READER": "off"}):
            self.assertFalse(ai_reader.enabled())


class JobsWithClaudeTests(unittest.TestCase):
    PRODUCT = {"name": "Wunder Puff Jacket", "url": "https://shop.lululemon.com/p/wunder-puff/1",
               "price": 198, "currency": "USD", "availability": "out_of_stock", "confidence": 0.95}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"PUBLIC_URL": "https://pilot.example.com", "TRACKING_SECRET": "s3",
                                                "CHECK_PAUSE_SECONDS": "0"})
        self.env.start()
        self.api = ShoppingApi(Path(self.temp.name), pilot_keys=parse_pilot_keys("iris:key-for-iris-123"),
                               sheets=FakeSheets())
        self.sent = []
        now = datetime.now(timezone.utc).isoformat()
        self.item = self.api.save_current("iris", {"product": self.PRODUCT, "size": "6"})["item"]
        self.api.set_preferences("iris", {"email_address": "iris@example.com"})
        self.now = now

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def jobs(self, client):
        return PilotJobs(self.api, mailer=lambda *a: self.sent.append(a), fetcher=lambda url: PLAIN_PAGE,
                         ai=ClaudePageReader(client))

    def test_confirmed_change_becomes_an_email(self):
        client = FakeClient(GOOD_READ, {"confirmed": True, "reason": "Page shows $149 and size 6 in stock."})
        jobs = self.jobs(client)
        result = jobs.check_all()
        self.assertEqual(result["changes"], 2)  # Back in size 6, and price 198 -> 149.
        self.assertEqual(client.requests[1]["model"], "claude-sonnet-5-5")
        self.assertIn("back in stock in size 6", client.requests[1]["messages"][0]["content"])
        self.assertEqual(jobs.send_instant_alerts(), 2)
        item = self.api.list_wishlist("iris")["items"][0]
        self.assertEqual(item["last_price"], 149.0)
        metrics = compute_metrics(self.api.analytics.events())["ai"]
        self.assertEqual((metrics["pages_read_ok"], metrics["changes_confirmed"]), (1, 1))
        self.assertIn("Claude page reading", metrics_report(self.api.analytics.events()))

    def test_unconfirmed_change_is_blocked(self):
        client = FakeClient(GOOD_READ, {"confirmed": False, "reason": "The $149 is a member price."})
        jobs = self.jobs(client)
        self.assertEqual(jobs.check_all()["changes"], 0)
        self.assertEqual(jobs.send_instant_alerts(), 0)
        item = self.api.list_wishlist("iris")["items"][0]
        self.assertEqual(item["last_price"], 198.0)  # Nothing overwritten.
        check = [r for r in self.api.analytics.events() if r["event"] == "check_run"][-1]
        self.assertFalse(check["ok"])
        self.assertIn("not confirmed", check["error"])
        self.assertEqual(compute_metrics(self.api.analytics.events())["ai"]["changes_blocked"], 1)

    def test_structured_pages_never_call_claude(self):
        client = FakeClient()
        page = ('<html><script type="application/ld+json">{"@type":"Product","name":"Wunder Puff Jacket",'
                '"offers":{"price":"198","priceCurrency":"USD","availability":"https://schema.org/OutOfStock"}}</script></html>')
        jobs = PilotJobs(self.api, mailer=lambda *a: None, fetcher=lambda url: page, ai=ClaudePageReader(client))
        jobs.check_all()
        self.assertEqual(client.requests, [])

    def test_claude_error_is_a_failed_check_not_a_crash(self):
        class Broken(FakeClient):
            def _create(self, **params):
                raise RuntimeError("overloaded")
        jobs = self.jobs(Broken())
        self.assertEqual(jobs.check_all()["ok"], 0)
        reads = [r for r in self.api.analytics.events() if r["event"] == "ai_read"]
        self.assertEqual(reads[0]["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
