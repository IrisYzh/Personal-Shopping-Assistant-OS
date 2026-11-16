"""Pilot analytics: an append-only event log and the metrics report built from it.

Every tracking point writes one JSON line to data/users/analytics.jsonl. Events carry only what
the metrics need: no page text, no browsing history beyond the product URL, no email body.

Funnel this measures (see docs/pilot-metrics.md):
  product page counted -> prompt shown -> prompt answered (add / no thanks / closed)
  -> item watched -> change found -> email sent -> email link clicked -> marked as bought
"""

from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Events the browser extension may send through POST /track. Everything else is logged by
# the backend itself, so a buggy or modified extension cannot fake the core funnel.
CLIENT_EVENTS = {
    "extension_installed",
    "popup_opened",
    "prompt_rendered",
    "prompt_closed",
    "settings_saved",
    "site_search",
    "site_added",
    "site_removed",
    "wishlist_link_clicked",
}
SERVER_EVENTS = {
    "product_view_counted",
    "cart_or_wishlist_click",
    "prompt_shown",
    "prompt_deferred",
    "prompt_accepted",
    "prompt_declined",
    "item_auto_saved",
    "item_edited",
    "item_removed",
    "item_marked_bought",
    "item_unmarked_bought",
    "check_run",
    "change_detected",
    "email_sent",
    "email_link_clicked",
    "email_failed",
    "history_cleared",
    "settings_saved",
}
MAX_PROPS = 20


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnalyticsLog:
    def __init__(self, path: str | Path = "data/users/analytics.jsonl"):
        self.path = Path(path)
        self._lock = threading.Lock()

    def log(self, event: str, user: str, **props: Any) -> dict[str, Any]:
        record = {"ts": props.pop("ts", None) or utc_now(), "event": event, "user": user}
        record.update(_clean_props(props))
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    def events(self, *, since: str | None = None, user: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        cutoff = _parse(since) if since else None
        rows = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A half-written line after a crash should not break the report.
                if user and row.get("user") != user:
                    continue
                if cutoff and _parse(row["ts"]) < cutoff:
                    continue
                rows.append(row)
        return rows


def _clean_props(props: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in list(props.items())[:MAX_PROPS]:
        if not isinstance(key, str) or len(key) > 40:
            continue
        if isinstance(value, str):
            value = value[:300]
        elif isinstance(value, (list, dict)) and len(json.dumps(value, default=str)) > 1000:
            continue
        clean[key] = value
    return clean


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def compute_metrics(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Turn raw events into the pilot numbers. Rates are None when there is nothing to divide by."""
    events = list(events)
    count = Counter(row["event"] for row in events)
    users = sorted({row["user"] for row in events})
    days_by_user: dict[str, set[str]] = defaultdict(set)
    for row in events:
        days_by_user[row["user"]].add(row["ts"][:10])

    shown = count["prompt_shown"]
    accepted = count["prompt_accepted"]
    declined = count["prompt_declined"]
    closed = count["prompt_closed"]
    accepted_rows = [row for row in events if row["event"] == "prompt_accepted"]
    with_size = sum(1 for row in accepted_rows if row.get("size_filled"))
    with_color = sum(1 for row in accepted_rows if row.get("color_filled"))

    emails = [row for row in events if row["event"] == "email_sent" and row.get("kind") != "test"]
    clicks = [row for row in events if row["event"] == "email_link_clicked"]
    emails_clicked = {row.get("email_id") for row in clicks if row.get("email_id")}
    email_kinds = Counter(row.get("kind", "unknown") for row in emails)

    changes = [row for row in events if row["event"] == "change_detected"]
    checks = [row for row in events if row["event"] == "check_run"]
    checks_ok = sum(1 for row in checks if row.get("ok"))

    bought = max(count["item_marked_bought"] - count["item_unmarked_bought"], 0)
    rendered = count["prompt_rendered"]
    products_viewed = {row.get("product_url") for row in events if row["event"] == "product_view_counted"}
    watched = accepted + count["item_auto_saved"]

    return {
        "people": len(users),
        "active_days": sum(len(days) for days in days_by_user.values()),
        "funnel": {
            "product_views_counted": count["product_view_counted"],
            "distinct_products_viewed": len(products_viewed - {None}),
            "prompts_shown": shown,
            "prompts_deferred_by_daily_cap": count["prompt_deferred"],
            "prompts_accepted": accepted,
            "prompts_declined": declined,
            "prompts_closed": closed,
            "items_auto_saved_from_cart_or_wishlist": count["item_auto_saved"],
            "items_watched": watched,
            "changes_detected": len(changes),
            "emails_sent": len(emails),
            "emails_clicked": len(emails_clicked),
            "items_marked_bought": bought,
        },
        "rates": {
            "prompt_rate_per_100_views": round(100 * shown / count["product_view_counted"], 1)
            if count["product_view_counted"]
            else None,
            "prompt_accept_rate": _rate(accepted, shown),
            "prompt_decline_rate": _rate(declined, shown),
            "prompt_close_rate": _rate(closed, shown),
            "prompt_no_answer_rate": _rate(max(shown - accepted - declined - closed, 0), shown),
            "size_filled_rate_on_accept": _rate(with_size, accepted),
            "color_filled_rate_on_accept": _rate(with_color, accepted),
            "email_click_rate": _rate(len(emails_clicked), len(emails)),
            "bought_per_watched_item": _rate(bought, watched),
            "prompt_render_rate": _rate(rendered, shown),
            "check_success_rate": _rate(checks_ok, len(checks)),
        },
        "prompt_cards_rendered": rendered,
        "noise": {
            "prompts_per_person_per_active_day": round(shown / max(sum(len(d) for d in days_by_user.values()), 1), 2),
            "emails_per_person": round(len(emails) / max(len(users), 1), 2),
            "emails_by_kind": dict(email_kinds),
            "items_removed": count["item_removed"],
            "history_cleared": count["history_cleared"],
        },
        "engagement": {
            "popup_opens": count["popup_opened"],
            "items_edited": count["item_edited"],
            "settings_saved": count["settings_saved"],
            "site_searches": count["site_search"],
            "site_searches_with_no_match": sum(
                1 for row in events if row["event"] == "site_search" and row.get("results") == 0
            ),
            "wishlist_link_clicks": count["wishlist_link_clicked"],
            "email_failures": count["email_failed"],
            "changes_by_type": dict(Counter(row.get("type", "unknown") for row in changes)),
        },
    }


def metrics_report(events: list[dict[str, Any]], *, per_person: bool = True) -> str:
    """Markdown report for the pilot write-up. Shows counts next to every rate."""
    total = compute_metrics(events)
    lines = ["# Pilot metrics", "", f"People: {total['people']}  ·  Active person-days: {total['active_days']}", ""]

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    f, r, n = total["funnel"], total["rates"], total["noise"]
    lines += [
        "## Funnel",
        "",
        "| Step | Count |",
        "| --- | --- |",
        *[f"| {key.replace('_', ' ')} | {value} |" for key, value in f.items()],
        "",
        "## Rates",
        "",
        "| Metric | Value | Based on |",
        "| --- | --- | --- |",
        f"| Prompts per 100 counted views | {r['prompt_rate_per_100_views'] if r['prompt_rate_per_100_views'] is not None else 'n/a'} | {f['prompts_shown']} prompts / {f['product_views_counted']} views |",
        f"| Prompt accept rate | {pct(r['prompt_accept_rate'])} | {f['prompts_accepted']} / {f['prompts_shown']} |",
        f"| Prompt decline rate | {pct(r['prompt_decline_rate'])} | {f['prompts_declined']} / {f['prompts_shown']} |",
        f"| Prompt closed without answer | {pct(r['prompt_close_rate'])} | {f['prompts_closed']} / {f['prompts_shown']} |",
        f"| Size filled when accepting | {pct(r['size_filled_rate_on_accept'])} | of {f['prompts_accepted']} accepts |",
        f"| Email click rate | {pct(r['email_click_rate'])} | {f['emails_clicked']} / {f['emails_sent']} emails |",
        f"| Marked bought per watched item | {pct(r['bought_per_watched_item'])} | {f['items_marked_bought']} / {f['items_watched']} |",
        f"| Price and stock checks that worked | {pct(r['check_success_rate'])} | |",
        "",
        "## Noise guardrails",
        "",
        f"* Prompts per person per active day: {n['prompts_per_person_per_active_day']} (rule: at most 1)",
        f"* Emails per person: {n['emails_per_person']}  ·  by kind: {n['emails_by_kind'] or 'none'}",
        f"* Items removed: {n['items_removed']}  ·  history cleared: {n['history_cleared']}",
        "",
        "With 3 to 5 people, report counts next to every rate. A rate on a handful of events is a story, not a statistic.",
    ]
    if per_person:
        lines += ["", "## Per person", "", "| Person | Views | Prompts | Accepted | Declined | Emails | Clicks | Bought |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for user in sorted({row["user"] for row in events}):
            m = compute_metrics(row for row in events if row["user"] == user)["funnel"]
            lines.append(
                f"| {user} | {m['product_views_counted']} | {m['prompts_shown']} | {m['prompts_accepted']} | "
                f"{m['prompts_declined']} | {m['emails_sent']} | {m['emails_clicked']} | {m['items_marked_bought']} |"
            )
    return "\n".join(lines) + "\n"
