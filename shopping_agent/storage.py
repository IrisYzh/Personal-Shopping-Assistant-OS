from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import (
    Availability,
    IntentDecision,
    IntentKind,
    MonitoringPolicy,
    PendingConfirmation,
    ProductContext,
    Route,
    WishlistItem,
    to_primitive,
)


class JsonRepository:
    """Small local adapter standing in for the future Supabase repository."""

    EMPTY_STATE = {
        "items": {},
        "pending_confirmations": {},
        "observations": [],
        "alerts": [],
        "audit_log": [],
    }

    def __init__(self, path: str | Path = "data/agent_state.json"):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {key: value.copy() for key, value in self.EMPTY_STATE.items()}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="shopping-agent-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save_item(self, item: WishlistItem) -> None:
        state = self._load()
        state["items"][item.id] = to_primitive(item)
        self._save(state)

    def get_item(self, item_id: str) -> WishlistItem | None:
        raw = self._load()["items"].get(item_id)
        return self._decode_item(raw) if raw else None

    def find_item_by_url(self, url: str) -> WishlistItem | None:
        for raw in self._load()["items"].values():
            if raw["url"] == url:
                return self._decode_item(raw)
        return None

    def list_items(self) -> list[WishlistItem]:
        return [self._decode_item(raw) for raw in self._load()["items"].values()]

    def save_pending(self, pending: PendingConfirmation) -> None:
        state = self._load()
        state["pending_confirmations"][pending.id] = to_primitive(pending)
        self._save(state)

    def get_pending(self, pending_id: str) -> PendingConfirmation | None:
        raw = self._load()["pending_confirmations"].get(pending_id)
        return self._decode_pending(raw) if raw else None

    def delete_pending(self, pending_id: str) -> None:
        state = self._load()
        state["pending_confirmations"].pop(pending_id, None)
        self._save(state)

    def append_observation(self, record: dict[str, Any]) -> None:
        state = self._load()
        state["observations"].append(to_primitive(record))
        self._save(state)

    def append_alert(self, record: dict[str, Any]) -> None:
        state = self._load()
        dedupe_key = record.get("dedupe_key")
        if dedupe_key and any(alert.get("dedupe_key") == dedupe_key for alert in state["alerts"]):
            return
        state["alerts"].append(to_primitive(record))
        self._save(state)

    def append_audit(self, record: dict[str, Any]) -> None:
        state = self._load()
        state["audit_log"].append(to_primitive(record))
        self._save(state)

    def snapshot(self) -> dict[str, Any]:
        return self._load()

    @staticmethod
    def _decode_item(raw: dict[str, Any]) -> WishlistItem:
        return WishlistItem(
            id=raw["id"],
            name=raw["name"],
            url=raw["url"],
            currency=raw["currency"],
            baseline_price=raw.get("baseline_price"),
            last_price=raw.get("last_price"),
            last_availability=Availability(raw["last_availability"]),
            intent_kind=IntentKind(raw["intent_kind"]),
            intent_confidence=raw["intent_confidence"],
            product_confidence=raw["product_confidence"],
            policy=MonitoringPolicy(**raw["policy"]),
            created_at=raw["created_at"],
            consecutive_failures=raw.get("consecutive_failures", 0),
        )

    @staticmethod
    def _decode_pending(raw: dict[str, Any]) -> PendingConfirmation:
        product_raw = raw["product"]
        decision_raw = raw["decision"]
        return PendingConfirmation(
            id=raw["id"],
            expression=raw["expression"],
            product=ProductContext(
                name=product_raw.get("name"),
                url=product_raw.get("url"),
                current_price=product_raw.get("current_price"),
                currency=product_raw.get("currency", "CNY"),
                availability=Availability(product_raw.get("availability", "unknown")),
                confidence=product_raw.get("confidence", 0.0),
            ),
            decision=IntentDecision(
                kind=IntentKind(decision_raw["kind"]),
                confidence=decision_raw["confidence"],
                route=Route(decision_raw["route"]),
                signals=decision_raw.get("signals", []),
                missing_fields=decision_raw.get("missing_fields", []),
                rationale=decision_raw.get("rationale", ""),
                target_price=decision_raw.get("target_price"),
                notify_restock=decision_raw.get("notify_restock", False),
            ),
            question=raw["question"],
            created_at=raw["created_at"],
        )
