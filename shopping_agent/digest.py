"""Weekly Saturday digest and instant alerts.

Counting and selection happen in code. Every line says why it is in the email, so the
user can trust it. Each email has a plain text part and a simple HTML part. The HTML only
highlights what changed (new price, discount, back in stock) with color and bold. It has
no images or banners, so it still reads like a personal note and not a promo blast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from html import escape
from urllib.parse import urlparse

from .behavior_intent import parse_time
from .models import Availability, WishlistItem
from .orchestrator import utc_now
from .ports import ShoppingRepository

WINDOW_DAYS = 7
INSTANT_ALERT_TYPES = {"restock", "price_drop", "promotion"}


# Colors used to mark what changed. Edit here to restyle every email.
COLOR_TEXT = "#1f1f1f"
COLOR_MUTED = "#6b6b6b"
COLOR_PRICE = "#c62828"  # new price and discount
COLOR_STOCK = "#1e7a34"  # back in stock
COLOR_SALE = "#b45309"   # sale started


@dataclass(slots=True)
class Digest:
    subject: str
    body: str
    change_count: int
    html: str = ""


@dataclass(slots=True)
class Email:
    subject: str
    body: str
    html: str


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "price unknown"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.2f}"


def _reason(alert: dict, item: WishlistItem) -> str:
    kind = alert.get("type")
    if kind == "restock":
        # Name the size only when the check really looked at that size.
        size = f" in size {alert['size']}" if alert.get("size") else ""
        return f"Back in stock{size}."
    if kind == "price_drop":
        old, new = alert.get("old_price"), alert.get("new_price")
        drop = ""
        if old and new and old > new:
            drop = f", {round((old - new) / old * 100)}% off"
        return f"Price dropped from {_money(old, item.currency)} to {_money(new, item.currency)}{drop}."
    if kind == "promotion":
        text = alert.get("promotion_text") or item.last_promotion
        promo = f": {text}" if text else ""
        return f"A new sale started{promo}."
    return "Something changed."


def _reason_html(alert: dict, item: WishlistItem) -> str:
    """The same sentence as _reason, with the change itself highlighted."""
    kind = alert.get("type")
    if kind == "restock":
        size = f" in size {escape(str(alert['size']))}" if alert.get("size") else ""
        return f'<strong style="color:{COLOR_STOCK}">Back in stock{size}</strong>'
    if kind == "price_drop":
        old, new = alert.get("old_price"), alert.get("new_price")
        drop = ""
        if old and new and old > new:
            drop = f', <strong style="color:{COLOR_PRICE}">{round((old - new) / old * 100)}% off</strong>'
        return (
            f'Price dropped from <span style="color:{COLOR_MUTED};text-decoration:line-through">'
            f"{_money(old, item.currency)}</span> to "
            f'<strong style="color:{COLOR_PRICE}">{_money(new, item.currency)}</strong>{drop}'
        )
    if kind == "promotion":
        text = alert.get("promotion_text") or item.last_promotion
        promo = f": {escape(text)}" if text else ""
        return f'<strong style="color:{COLOR_SALE}">New sale{promo}</strong>'
    return "Something changed"


def _site_name(url: str) -> str:
    from .server import site_for  # Local import keeps digest usable without the server.

    return site_for(url) or urlparse(url).hostname or "the site"


def _status_html(item: WishlistItem) -> str:
    stock = "in stock" if item.last_availability is Availability.IN_STOCK else item.last_availability.value.replace("_", " ")
    return f'Now <strong>{_money(item.last_price, item.currency)}</strong>, {stock}.'


def _wrap_html(inner: str, footer: str) -> str:
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#ffffff">'
        f'<div style="max-width:560px;margin:0 auto;padding:20px 16px;color:{COLOR_TEXT};'
        'font:15px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        f"{inner}"
        f'<p style="margin:24px 0 0;color:{COLOR_MUTED};font-size:13px">{footer}</p>'
        "</div></body></html>"
    )


def _item_html(item: WishlistItem, reasons_html: list[str], link: str | None = None) -> str:
    prefs = ", ".join(part for part in (item.size and f"size {item.size}", item.color) if part)
    title = escape(item.name) + (f' <span style="color:{COLOR_MUTED};font-weight:400">({escape(prefs)})</span>' if prefs else "")
    lines = "".join(f'<div style="margin:2px 0">{line}</div>' for line in reasons_html)
    return (
        f'<div style="margin:0 0 18px;padding:12px 14px;border:1px solid #e6e6e6;border-radius:10px">'
        f'<div style="font-weight:600;margin-bottom:4px">{title}</div>'
        f"{lines}"
        f'<div style="margin:2px 0">{_status_html(item)}</div>'
        f'<div style="margin-top:6px"><a href="{escape(link or item.url, quote=True)}" style="color:{COLOR_TEXT}">'
        f"View on {escape(_site_name(item.url))}</a></div></div>"
    )


def build_weekly_digest(
    repository: ShoppingRepository,
    *,
    now: str | None = None,
    window_days: int = WINDOW_DAYS,
    link_for=None,
) -> Digest | None:
    """Return the digest, or None when nothing changed. No change means no email.

    window_days=7 is the Saturday summary, window_days=1 the daily one. link_for(item)
    gives the tracked link for each item (see tracking.py).
    """
    link_for = link_for or (lambda item: item.url)
    now_at = parse_time(now or utc_now())
    since = now_at - timedelta(days=window_days)
    items = {item.id: item for item in repository.list_items() if item.status == "watching"}
    alerts = [
        alert
        for alert in repository.snapshot().get("alerts", [])
        if alert.get("item_id") in items and parse_time(alert["created_at"]) >= since
    ]

    lines_by_item: dict[str, list[str]] = {}
    html_by_item: dict[str, list[str]] = {}
    for alert in sorted(alerts, key=lambda a: a["created_at"]):
        item = items[alert["item_id"]]
        reason = _reason(alert, item)
        lines_by_item.setdefault(item.id, [])
        html_by_item.setdefault(item.id, [])
        if reason not in lines_by_item[item.id]:
            lines_by_item[item.id].append(reason)
            html_by_item[item.id].append(_reason_html(alert, item))

    if not lines_by_item:
        return None

    period = "this week" if window_days >= 7 else "today" if window_days == 1 else f"in the last {window_days} days"
    out = ["Hi,", "", f"Here is what changed {period} on the items you are watching.", ""]
    for item_id, reasons in lines_by_item.items():
        item = items[item_id]
        prefs = ", ".join(part for part in (item.size and f"size {item.size}", item.color) if part)
        title = f"{item.name} ({prefs})" if prefs else item.name
        out.append(f"* {title}")
        for reason in reasons:
            out.append(f"  {reason}")
        stock = "in stock" if item.last_availability is Availability.IN_STOCK else item.last_availability.value.replace("_", " ")
        out.append(f"  Now {_money(item.last_price, item.currency)}, {stock}.")
        out.append(f"  {link_for(item)}")
        out.append("")

    quiet = len(items) - len(lines_by_item)
    quiet_line = ""
    if quiet > 0:
        others = "the other item" if quiet == 1 else f"the other {quiet} items"
        quiet_line = f"No news on {others}. I will keep watching."
        out.append(quiet_line)
        out.append("")
    footer = (
        "You get this email because you saved these items with the Shopping Assistant extension. "
        "To change how often I write, open the extension and go to Settings."
    )
    out.append("You get this email because you saved these items with the Shopping Assistant extension.")
    out.append("To change how often I write, open the extension and go to Settings.")

    inner = f"<p style=\"margin:0 0 6px\">Hi,</p><p style=\"margin:0 0 16px\">Here is what changed {period} on the items you are watching.</p>"
    inner += "".join(
        _item_html(items[item_id], html_by_item[item_id], link_for(items[item_id])) for item_id in lines_by_item
    )
    if quiet_line:
        inner += f'<p style="margin:0">{quiet_line}</p>'

    count = len(lines_by_item)
    subject = f"{count} item{'s' if count != 1 else ''} on your wishlist changed {period}"
    return Digest(subject=subject, body="\n".join(out), change_count=count, html=_wrap_html(inner, footer))


def instant_alert(item: WishlistItem, alert: dict, link_for=None) -> Email | None:
    """A short email for one change the user asked to hear about right away."""
    if alert.get("type") not in INSTANT_ALERT_TYPES:
        return None
    link = (link_for or (lambda i: i.url))(item)
    reason = _reason(alert, item)
    subject = f"{item.name}: {reason.rstrip('.')}"
    footer = "You asked me to tell you right away about changes like this one. You can change this in Settings."
    body = "\n".join(
        [
            "Hi,",
            "",
            f"{item.name}. {reason}",
            f"Now {_money(item.last_price, item.currency)}.",
            link,
            "",
            footer,
        ]
    )
    inner = '<p style="margin:0 0 16px">Hi, good news on an item you are watching.</p>' + _item_html(
        item, [_reason_html(alert, item)], link
    )
    return Email(subject=subject, body=body, html=_wrap_html(inner, footer))


def test_email() -> Email:
    """Sent from Settings, so a pilot friend can see the email arrives and add the sender to contacts."""
    footer = "You asked for this test from the Shopping Assistant settings."
    lines = [
        "Hi,",
        "",
        "This is a test from your Shopping Assistant. If you can read this, emails work.",
        "",
        "One small favor: add this address to your contacts. That keeps my real alerts out of Promotions and spam.",
        "",
        footer,
    ]
    inner = (
        '<p style="margin:0 0 12px">Hi,</p>'
        '<p style="margin:0 0 12px">This is a test from your Shopping Assistant. '
        f'<strong style="color:{COLOR_STOCK}">If you can read this, emails work.</strong></p>'
        '<p style="margin:0">One small favor: add this address to your contacts. '
        "That keeps my real alerts out of Promotions and spam.</p>"
    )
    return Email(subject="Shopping Assistant test email", body="\n".join(lines), html=_wrap_html(inner, footer))
