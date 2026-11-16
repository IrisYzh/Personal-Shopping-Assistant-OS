"""Mirror the wishlist to a Google Sheet through an Apps Script web app.

The local JSON repository stays the source of truth. When SHEETS_WEBHOOK_URL is set,
every wishlist change is also sent to the sheet so Iris and pilot users can see it.
See docs/browser-extension.md for the Apps Script code.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from .models import WishlistItem

log = logging.getLogger(__name__)

SHEET_COLUMNS = [
    "id",
    "user",
    "name",
    "url",
    "site",
    "added_via",
    "size",
    "color",
    "baseline_price",
    "last_price",
    "currency",
    "availability",
    "status",
    "created_at",
]


def item_to_row(item: WishlistItem, *, user: str, status: str | None = None) -> dict:
    from urllib.parse import urlparse

    return {
        "id": item.id,
        "user": user,
        "name": item.name,
        "url": item.url,
        "site": urlparse(item.url).netloc,
        "added_via": item.added_via,
        "size": item.size or "",
        "color": item.color or "",
        "baseline_price": item.baseline_price if item.baseline_price is not None else "",
        "last_price": item.last_price if item.last_price is not None else "",
        "currency": item.currency,
        "availability": item.last_availability.value,
        "status": status or item.status,
        "created_at": item.created_at,
    }


class SheetsMirror:
    def __init__(self, webhook_url: str | None = None, user: str | None = None, timeout: float = 5.0):
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("SHEETS_WEBHOOK_URL", "")
        self.user = user or os.environ.get("PILOT_USER", "me")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def upsert(self, item: WishlistItem, *, user: str | None = None) -> bool:
        return self._post({"action": "upsert", "row": item_to_row(item, user=user or self.user)})

    def remove(self, item_id: str) -> bool:
        return self._post({"action": "remove", "id": item_id})

    def _post(self, payload: dict) -> bool:
        if not self.enabled:
            return False
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except Exception as error:  # A sheet outage must never block the local wishlist.
            log.warning("Sheets mirror failed: %s", error)
            return False
