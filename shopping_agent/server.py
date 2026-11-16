"""HTTP API for the Chrome extension.

Standard library only, so it runs anywhere Python 3.10+ runs. One shared server holds
every pilot user. Each user has a personal key and a separate JSON state file, so one
person's wishlist and browsing history never mix with another's.

Run it with:  python -m shopping_agent serve --host 0.0.0.0 --port 8787
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analytics import CLIENT_EVENTS, AnalyticsLog, compute_metrics, metrics_report
from .behavior_intent import parse_time
from .models import (
    Availability,
    BehaviorEventType,
    Observation,
    ObservationSource,
    ProductContext,
    Route,
    WishlistItem,
    to_primitive,
)
from .orchestrator import ShoppingOrchestrator, utc_now
from .page_reader import PageProduct, observation_from, variants_from_client
from .sheets import SheetsMirror, item_to_row
from .storage import JsonRepository
from .tracking import read_token

log = logging.getLogger(__name__)

SITE_CATALOG = json.loads((Path(__file__).with_name("site_catalog.json")).read_text(encoding="utf-8"))
SUPPORTED_SITES = {domain: site["name"] for site in SITE_CATALOG for domain in site["domains"]}
HISTORY_RETENTION_DAYS = 30
EMAIL_OPTIONS = ("weekly_digest", "daily_digest", "size_back", "price_drop", "sale_start")
DEFAULT_EMAIL = {"weekly_digest": True, "daily_digest": False, "size_back": True, "price_drop": True, "sale_start": False}
MAX_BODY_BYTES = 64 * 1024
USER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
EMAIL_ADDRESS = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
PAGE_VISIT_MIN_CONFIDENCE = 0.8


def site_for(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    return SUPPORTED_SITES.get(host)


def search_sites(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Suggestions for the "Sites I watch" search box. Matches names, aliases and domains."""
    text = (query or "").strip().lower()
    if not text:
        return []
    ranked = []
    for site in SITE_CATALOG:
        words = [site["name"].lower(), *[a.lower() for a in site.get("aliases", [])], *site["domains"]]
        if any(word.startswith(text) for word in words):
            ranked.append((0, site["name"], site))
        elif any(text in word for word in words):
            ranked.append((1, site["name"], site))
    ranked.sort(key=lambda entry: entry[:2])
    return [{"name": s["name"], "domain": s["domains"][0], "category": s.get("category", "")} for _, _, s in ranked[:limit]]


def parse_pilot_keys(raw: str | None) -> dict[str, str]:
    """PILOT_KEYS="iris:key1,friend:key2" becomes {"key1": "iris", "key2": "friend"}."""
    keys: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if not pair.strip():
            continue
        user, _, key = pair.strip().partition(":")
        if not USER_NAME.match(user) or len(key) < 12:
            raise ValueError(f"Bad PILOT_KEYS entry for {user!r}: use name:key with a key of 12+ characters")
        keys[key] = user
    return keys


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class UserContext:
    user: str
    orchestrator: ShoppingOrchestrator
    repository: JsonRepository
    lock: threading.Lock


class ShoppingApi:
    """Request logic, kept apart from HTTP plumbing so tests can call it directly."""

    def __init__(
        self,
        data_dir: str | Path = "data/users",
        *,
        pilot_keys: dict[str, str] | None = None,
        sheets: SheetsMirror | None = None,
        analytics: AnalyticsLog | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.analytics = analytics or AnalyticsLog(self.data_dir / "analytics.jsonl")
        self.pilot_keys = pilot_keys if pilot_keys is not None else parse_pilot_keys(os.environ.get("PILOT_KEYS"))
        self.sheets = sheets if sheets is not None else SheetsMirror()
        self._users: dict[str, UserContext] = {}
        self._users_lock = threading.Lock()

    # ----- auth and per-user state -------------------------------------------------

    def authenticate(self, key: str | None) -> str:
        if not self.pilot_keys:
            return "me"  # Local development: one user, no key.
        user = self.pilot_keys.get((key or "").strip())
        if not user:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Missing or unknown pilot key")
        return user

    def context(self, user: str) -> UserContext:
        with self._users_lock:
            if user not in self._users:
                repository = JsonRepository(self.data_dir / user / "state.json")
                self._users[user] = UserContext(
                    user=user,
                    orchestrator=ShoppingOrchestrator(repository=repository),
                    repository=repository,
                    lock=threading.Lock(),
                )
            return self._users[user]

    # ----- endpoints ----------------------------------------------------------------

    def record_event(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        product = _product_from(body.get("product") or {})
        if not site_for(product.url):
            raise ApiError(HTTPStatus.BAD_REQUEST, "This site is not supported yet")
        try:
            event_type = BehaviorEventType(body.get("event_type", ""))
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Unknown event_type") from None
        session_id = str(body.get("session_id") or "").strip()
        if not session_id:
            raise ApiError(HTTPStatus.BAD_REQUEST, "session_id is required")

        ctx = self.context(user)
        with ctx.lock:
            occurred_at = body.get("occurred_at") or utc_now()
            ctx.repository.prune_behavior_events(
                (parse_time(occurred_at) - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
            )
            existing = ctx.repository.find_item_by_url(product.url) if product.url else None
            result = ctx.orchestrator.record_behavior(
                event_type, product, session_id=session_id, occurred_at=occurred_at
            )
        site = site_for(product.url)
        track = {"site": site, "product_url": product.url, "session_id": session_id[:64]}
        if event_type is BehaviorEventType.VIEW:
            self.analytics.log("product_view_counted", user, watched=existing is not None, **track)
        else:
            self.analytics.log("cart_or_wishlist_click", user, kind=event_type.value, **track)
        if result.route is Route.AUTO_SAVE and result.item:
            self._mirror(user, result.item)
            if existing is None:
                self.analytics.log("item_auto_saved", user, item_id=result.item.id, kind=event_type.value, **track)
        if result.prompt_now and result.pending:
            summary_ = result.behavior_summary
            self.analytics.log("prompt_shown", user, pending_id=result.pending.id,
                               view_count=summary_.view_count if summary_ else None, **track)
        elif result.deferred_to_daily_digest:
            self.analytics.log("prompt_deferred", user, **track)

        # A visit to a watched product is a free, real-browser price and stock check.
        if existing and existing.status == "watching" and product.confidence >= PAGE_VISIT_MIN_CONFIDENCE:
            page = PageProduct(
                name=product.name or existing.name,
                price=product.current_price,
                currency=product.currency,
                availability=product.availability,
                confidence=product.confidence,
                source="page_visit",
                promotion_text=product.promotion_text,
                variants=variants_from_client((body.get("product") or {}).get("variants")),
            )
            self.apply_observation(
                user,
                existing.id,
                observation_from(page, size=existing.size, color=existing.color, checked_at=occurred_at),
                via="page_visit",
            )

        summary = result.behavior_summary
        response: dict[str, Any] = {
            "route": result.route.value,
            "prompt_now": result.prompt_now,
            "saved": result.route is Route.AUTO_SAVE,
            "item": _item_json(result.item, user) if result.item else None,
        }
        if result.prompt_now and result.pending:
            response["prompt"] = {
                "pending_id": result.pending.id,
                "product_name": product.name,
                "site": site_for(product.url),
                "view_count": summary.view_count if summary else None,
                "window_days": summary.window_days if summary else None,
            }
        return response

    def confirm(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        pending_id = str(body.get("pending_id") or "")
        approved = bool(body.get("approved"))
        ctx = self.context(user)
        with ctx.lock:
            try:
                result = ctx.orchestrator.confirm(
                    pending_id,
                    approved,
                    size=_text(body.get("size")),
                    color=_text(body.get("color")),
                )
            except KeyError:
                raise ApiError(HTTPStatus.NOT_FOUND, "This question has expired") from None
        if result.item:
            self._mirror(user, result.item)
        size, color = _text(body.get("size")), _text(body.get("color"))
        self.analytics.log(
            "prompt_accepted" if approved else "prompt_declined",
            user,
            pending_id=pending_id,
            item_id=result.item.id if result.item else None,
            size_filled=bool(size and size.strip()),
            color_filled=bool(color and color.strip()),
        )
        return {"saved": result.item is not None, "item": _item_json(result.item, user) if result.item else None}

    def save_current(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        """"Save this item" in the popup: save the product on the open tab, with size and color."""
        product = _product_from(body.get("product") or {})
        site = site_for(product.url)
        if not site:
            raise ApiError(HTTPStatus.BAD_REQUEST, "This site is not supported yet")
        if not product.name:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Could not read this product")
        size, color = _text(body.get("size")), _text(body.get("color"))
        ctx = self.context(user)
        with ctx.lock:
            item, created = ctx.orchestrator.save_from_popup(product, size=size, color=color)
        self._mirror(user, item)
        self.analytics.log(
            "item_saved_from_popup", user, item_id=item.id, site=site, product_url=product.url, created=created,
            size_filled=bool(size and size.strip()), color_filled=bool(color and color.strip()),
        )
        return {"saved": True, "created": created, "item": _item_json(item, user)}

    def list_wishlist(self, user: str) -> dict[str, Any]:
        ctx = self.context(user)
        with ctx.lock:
            items = ctx.repository.list_items()
        items.sort(key=lambda item: item.created_at, reverse=True)
        return {"items": [_item_json(item, user) for item in items]}

    def update_item(self, user: str, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ctx = self.context(user)
        with ctx.lock:
            try:
                item = ctx.orchestrator.update_preferences(
                    item_id, size=_text(body.get("size")), color=_text(body.get("color"))
                )
            except KeyError:
                raise ApiError(HTTPStatus.NOT_FOUND, "No such wishlist item") from None
        self._mirror(user, item)
        self.analytics.log("item_edited", user, item_id=item_id, size_set=bool(item.size), color_set=bool(item.color))
        return {"item": _item_json(item, user)}

    def set_bought(self, user: str, item_id: str, bought: bool) -> dict[str, Any]:
        ctx = self.context(user)
        with ctx.lock:
            try:
                item = ctx.orchestrator.set_bought(item_id, bought)
            except KeyError:
                raise ApiError(HTTPStatus.NOT_FOUND, "No such wishlist item") from None
            alerts = [a for a in ctx.repository.snapshot().get("alerts", []) if a.get("item_id") == item_id]
            emailed = ctx.repository.emailed_alert_ids()
        self._mirror(user, item)
        self.analytics.log(
            "item_marked_bought" if bought else "item_unmarked_bought",
            user,
            item_id=item_id,
            site=site_for(item.url),
            days_watched=_days_since(item.created_at),
            had_alert=bool(alerts),
            had_email=any(a.get("id") in emailed for a in alerts),
            price_below_baseline=bool(
                item.last_price is not None and item.baseline_price is not None and item.last_price < item.baseline_price
            ),
        )
        return {"item": _item_json(item, user)}

    def delete_item(self, user: str, item_id: str) -> dict[str, Any]:
        ctx = self.context(user)
        with ctx.lock:
            item = ctx.repository.get_item(item_id)
            removed = ctx.orchestrator.remove_item(item_id)
        if not removed:
            raise ApiError(HTTPStatus.NOT_FOUND, "No such wishlist item")
        self.analytics.log(
            "item_removed", user, item_id=item_id, status=item.status if item else None,
            site=site_for(item.url) if item else None, days_watched=_days_since(item.created_at) if item else None,
        )
        if self.sheets.enabled:
            self.sheets.remove(item_id)
        return {"removed": True}

    def clear_history(self, user: str) -> dict[str, Any]:
        ctx = self.context(user)
        with ctx.lock:
            removed = ctx.repository.clear_behavior_events()
        self.analytics.log("history_cleared", user, removed_events=removed)
        return {"removed_events": removed}

    def set_preferences(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        email = body.get("email", {})
        address = body.get("email_address")
        if address is not None:
            address = str(address).strip()
            if address and not EMAIL_ADDRESS.match(address):
                raise ApiError(HTTPStatus.BAD_REQUEST, "That email address does not look right")
        if not isinstance(email, dict) or set(email) - set(EMAIL_OPTIONS):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"email must be an object with keys from {', '.join(EMAIL_OPTIONS)}")
        if not all(isinstance(value, bool) for value in email.values()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "email values must be true or false")
        ctx = self.context(user)
        with ctx.lock:
            current = {**DEFAULT_EMAIL, **ctx.repository.get_preferences().get("email", {}), **email}
            values: dict[str, Any] = {"email": current}
            if address is not None:
                values["email_address"] = address
            saved = ctx.repository.set_preferences(values)
        self.analytics.log("settings_saved", user, has_email_address=bool(saved.get("email_address")),
                           **{f"email_{key}": value for key, value in current.items()})
        return {"preferences": saved}

    def get_preferences(self, user: str) -> dict[str, Any]:
        ctx = self.context(user)
        with ctx.lock:
            prefs = ctx.repository.get_preferences()
        return {"preferences": {"email": {**DEFAULT_EMAIL, **prefs.get("email", {})},
                                "email_address": prefs.get("email_address", "")}}

    # ----- price and stock observations -------------------------------------------

    def apply_observation(self, user: str, item_id: str, observation: Observation, *, via: str) -> dict[str, Any]:
        """Run one observation through the monitor agent and log the check and any change."""
        ctx = self.context(user)
        with ctx.lock:
            before = {a.get("id") for a in ctx.repository.snapshot().get("alerts", [])}
            item = ctx.repository.get_item(item_id)
            if not item:
                raise ApiError(HTTPStatus.NOT_FOUND, "No such wishlist item")
            decision = ctx.orchestrator.check_item(item_id, observation)
            new_alerts = [a for a in ctx.repository.snapshot().get("alerts", []) if a.get("id") not in before]
            item = ctx.repository.get_item(item_id)
        self.analytics.log(
            "check_run", user, item_id=item_id, via=via, ok=decision.accepted, site=site_for(item.url),
            size_level=observation.size is not None, error=observation.error,
        )
        for alert in new_alerts:
            self.analytics.log("change_detected", user, item_id=item_id, type=alert.get("type"), via=via,
                               size_level=bool(alert.get("size")), alert_id=alert.get("id"))
        if new_alerts:
            self._mirror(user, item)
        return {"ok": decision.accepted, "alerts": new_alerts, "state": decision.state}

    # ----- analytics ----------------------------------------------------------------

    def track(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        event = str(body.get("event") or "")
        if event not in CLIENT_EVENTS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Unknown event")
        props = body.get("props") if isinstance(body.get("props"), dict) else {}
        props = {k: v for k, v in props.items() if isinstance(v, (str, int, float, bool)) or v is None}
        props.pop("user", None)
        props.pop("event", None)
        props.pop("ts", None)
        self.analytics.log(event, user, client=True, **props)
        return {"ok": True}

    def email_click(self, token: str) -> str:
        data = read_token(token)
        if not data or not USER_NAME.match(data["user"]):
            raise ApiError(HTTPStatus.NOT_FOUND, "This link is not valid")
        item = self.context(data["user"]).repository.get_item(data["item_id"])
        self.analytics.log("email_link_clicked", data["user"], email_id=data["email_id"], kind=data["kind"],
                           item_id=data["item_id"], item_found=item is not None)
        if not item:
            raise ApiError(HTTPStatus.NOT_FOUND, "This item is no longer on the wishlist")
        return item.url

    def metrics(self, admin_key: str | None, *, since: str | None = None, fmt: str = "json") -> Any:
        expected = os.environ.get("ADMIN_KEY", "")
        if not expected or (admin_key or "") != expected:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Admin key required")
        events = self.analytics.events(since=since)
        if fmt == "jsonl":  # Raw export for your own analysis.
            return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events)
        return metrics_report(events) if fmt == "md" else compute_metrics(events)

    def send_test_email(self, user: str, mailer=None) -> dict[str, Any]:
        """Lets a friend check that emails arrive, and add the sender to contacts right away."""
        from .digest import test_email
        from .jobs import default_mailer

        address = self.context(user).repository.get_preferences().get("email_address")
        if not address:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Save your email address in Settings first")
        email = test_email()
        try:
            (mailer or default_mailer)(address, email.subject, email.body, email.html)
        except Exception as exc:
            log.warning("Test email for %s failed: %s", user, exc)
            self.analytics.log("email_failed", user, kind="test", error=type(exc).__name__)
            raise ApiError(HTTPStatus.BAD_GATEWAY, "The email could not be sent. Tell Iris.") from None
        self.analytics.log("email_sent", user, kind="test", items=0)
        return {"sent_to": address}

    def _mirror(self, user: str, item: WishlistItem) -> None:
        if self.sheets.enabled:
            self.sheets.upsert(item, user=user)


def _days_since(value: str | None) -> float | None:
    if not value:
        return None
    return round((parse_time(utc_now()) - parse_time(value)).total_seconds() / 86400, 1)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:40]


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _product_from(raw: dict[str, Any]) -> ProductContext:
    try:
        availability = Availability(raw.get("availability") or "unknown")
    except ValueError:
        availability = Availability.UNKNOWN
    name = (str(raw.get("name") or "").strip() or None)
    return ProductContext(
        name=name[:200] if name else None,
        url=_canonical_url(raw.get("url")),
        current_price=_number(raw.get("price")),
        currency=str(raw.get("currency") or "USD")[:3].upper(),
        availability=availability,
        confidence=min(max(_number(raw.get("confidence")) or 0.0, 0.0), 1.0),
        promotion_text=(str(raw["promotion_text"])[:200] if raw.get("promotion_text") else None),
    )


def _canonical_url(value: Any) -> str | None:
    """Drop query strings and fragments so tracking params do not split one product into many."""
    if not value:
        return None
    parsed = urlparse(str(value))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return f"https://{parsed.hostname.lower()}{parsed.path.rstrip('/') or '/'}"


def _item_json(item: WishlistItem, user: str) -> dict[str, Any]:
    row = item_to_row(item, user=user)
    row["site"] = site_for(item.url) or row["site"]
    row["notify_restock"] = item.policy.notify_restock
    row["target_price"] = item.policy.target_price
    row["status"] = item.status
    row["bought_at"] = item.bought_at
    return to_primitive(row)


ROUTES = [
    ("GET", re.compile(r"^/health$"), "health"),
    ("POST", re.compile(r"^/events$"), "events"),
    ("POST", re.compile(r"^/confirm$"), "confirm"),
    ("GET", re.compile(r"^/wishlist$"), "wishlist"),
    ("POST", re.compile(r"^/wishlist$"), "save_current"),
    ("PATCH", re.compile(r"^/wishlist/([A-Za-z0-9-]+)$"), "update"),
    ("DELETE", re.compile(r"^/wishlist/([A-Za-z0-9-]+)$"), "delete"),
    ("POST", re.compile(r"^/wishlist/([A-Za-z0-9-]+)/bought$"), "bought"),
    ("DELETE", re.compile(r"^/wishlist/([A-Za-z0-9-]+)/bought$"), "unbought"),
    ("POST", re.compile(r"^/track$"), "track"),
    ("POST", re.compile(r"^/test-email$"), "test_email"),
    ("GET", re.compile(r"^/preferences$"), "get_preferences"),
    ("GET", re.compile(r"^/metrics$"), "metrics"),
    ("GET", re.compile(r"^/r/([A-Za-z0-9_.-]+)$"), "redirect"),
    ("DELETE", re.compile(r"^/history$"), "history"),
    ("PUT", re.compile(r"^/preferences$"), "preferences"),
    ("GET", re.compile(r"^/sites$"), "sites"),
]


def make_handler(api: ShoppingApi):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ShoppingAssistant/0.3"

        def log_message(self, fmt, *args):  # Keep request logs short, free of bodies, keys and tokens.
            path = self.path.split("?")[0]
            log.info("%s %s", self.command, "/r/…" if path.startswith("/r/") else path)

        def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, url: str) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_text(self, text: str, kind: str = "text/markdown") -> None:
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cors(self) -> None:
            # The pilot key, not the origin, is what protects each user's data.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pilot-Key, X-Admin-Key")

        def do_OPTIONS(self):
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.end_headers()

        def _dispatch(self):
            path = urlparse(self.path).path
            for method, pattern, name in ROUTES:
                match = pattern.match(path)
                if method == self.command and match:
                    break
            else:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            try:
                if name == "health":
                    return self._send(HTTPStatus.OK, {"ok": True})
                query = parse_qs(urlparse(self.path).query)
                if name == "sites":  # Public list, no key needed.
                    return self._send(HTTPStatus.OK, {"sites": search_sites(query.get("q", [""])[0])})
                if name == "redirect":  # Email links. The signed token is the key.
                    return self._redirect(api.email_click(match.group(1)))
                if name == "metrics":  # For Iris only, with ADMIN_KEY.
                    fmt = query.get("format", ["json"])[0]
                    result = api.metrics(self.headers.get("X-Admin-Key") or query.get("key", [None])[0],
                                         since=query.get("since", [None])[0], fmt=fmt)
                    if fmt in ("md", "jsonl"):
                        return self._send_text(result, "text/markdown" if fmt == "md" else "application/x-ndjson")
                    return self._send(HTTPStatus.OK, result)
                user = api.authenticate(self.headers.get("X-Pilot-Key"))
                body = self._read_json() if self.command in ("POST", "PATCH", "PUT") else {}
                if name == "events":
                    payload = api.record_event(user, body)
                elif name == "confirm":
                    payload = api.confirm(user, body)
                elif name == "wishlist":
                    payload = api.list_wishlist(user)
                elif name == "save_current":
                    payload = api.save_current(user, body)
                elif name == "update":
                    payload = api.update_item(user, match.group(1), body)
                elif name == "delete":
                    payload = api.delete_item(user, match.group(1))
                elif name == "preferences":
                    payload = api.set_preferences(user, body)
                elif name == "get_preferences":
                    payload = api.get_preferences(user)
                elif name == "bought":
                    payload = api.set_bought(user, match.group(1), True)
                elif name == "unbought":
                    payload = api.set_bought(user, match.group(1), False)
                elif name == "track":
                    payload = api.track(user, body)
                elif name == "test_email":
                    payload = api.send_test_email(user)
                else:
                    payload = api.clear_history(user)
                return self._send(HTTPStatus.OK, payload)
            except ApiError as error:
                return self._send(error.status, {"error": error.message})
            except Exception:  # Never leak internals to the browser.
                log.exception("Request failed")
                return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Server error"})

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be JSON") from None
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
            return data

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _dispatch

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8787, data_dir: str | Path = "data/users", *, jobs: bool = False) -> None:
    from .multiagent.provider import load_local_env

    load_local_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api = ShoppingApi(data_dir)
    public = host not in ("127.0.0.1", "localhost")
    if not api.pilot_keys and public:
        raise SystemExit("Set PILOT_KEYS before serving on a public address.")
    if public and not os.environ.get("TRACKING_SECRET"):
        raise SystemExit("Set TRACKING_SECRET before serving on a public address, so email links cannot be forged.")
    server = ThreadingHTTPServer((host, port), make_handler(api))
    mode = f"{len(api.pilot_keys)} pilot users" if api.pilot_keys else "local mode, no key"
    print(f"Shopping Assistant API on http://{host}:{port} ({mode})", flush=True)
    runner = None
    if jobs:
        from .jobs import PilotJobs

        runner = PilotJobs(api)
        runner.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if runner:
            runner.stop()
        server.server_close()
