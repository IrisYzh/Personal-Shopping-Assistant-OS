from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import (
    Availability,
    BehaviorEvent,
    BehaviorEventType,
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
        "behavior_events": [],
        "daily_digest_log": [],
        "prompt_log": [],
        "audit_log": [],
        "email_log": [],
    }

    def __init__(self, path: str | Path = "data/agent_state.json"):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {key: value.copy() for key, value in self.EMPTY_STATE.items()}
        state = json.loads(self.path.read_text(encoding="utf-8"))
        for key, empty_value in self.EMPTY_STATE.items():
            state.setdefault(key, empty_value.copy())
        return state

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

    def delete_item(self, item_id: str) -> bool:
        state = self._load()
        removed = state["items"].pop(item_id, None) is not None
        if removed:
            self._save(state)
        return removed

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

    def append_behavior_event(self, event: BehaviorEvent) -> None:
        state = self._load()
        state["behavior_events"].append(to_primitive(event))
        self._save(state)

    def list_behavior_events(self, product_url: str | None = None) -> list[BehaviorEvent]:
        events = [self._decode_behavior_event(raw) for raw in self._load()["behavior_events"]]
        if product_url is None:
            return events
        return [event for event in events if event.product.url == product_url]

    def get_preferences(self) -> dict[str, Any]:
        return dict(self._load().get("preferences", {}))

    def set_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        state = self._load()
        state.setdefault("preferences", {}).update(values)
        self._save(state)
        return dict(state["preferences"])

    def clear_behavior_events(self) -> int:
        """Delete all browsing history. Used by the privacy setting in the extension."""
        state = self._load()
        removed = len(state["behavior_events"])
        state["behavior_events"] = []
        self._save(state)
        return removed

    def prune_behavior_events(self, before: str) -> int:
        """Drop browsing events older than an ISO timestamp (30 day retention)."""
        from .behavior_intent import parse_time

        cutoff = parse_time(before)
        state = self._load()
        kept = [raw for raw in state["behavior_events"] if parse_time(str(raw["occurred_at"])) >= cutoff]
        removed = len(state["behavior_events"]) - len(kept)
        if removed:
            state["behavior_events"] = kept
            self._save(state)
        return removed

    def append_daily_digest_record(self, record: dict[str, Any]) -> None:
        state = self._load()
        state["daily_digest_log"].append(to_primitive(record))
        self._save(state)

    def was_daily_digest_prompted(self, product_url: str, digest_date: str) -> bool:
        return any(
            record.get("product_url") == product_url and record.get("digest_date") == digest_date
            for record in self._load()["daily_digest_log"]
        )

    def append_prompt_record(self, record: dict[str, Any]) -> None:
        state = self._load()
        state["prompt_log"].append(to_primitive(record))
        self._save(state)

    def was_prompted(self, product_url: str) -> bool:
        return any(record.get("product_url") == product_url for record in self._load()["prompt_log"])

    def prompt_count_on(self, prompt_date: str) -> int:
        return sum(1 for record in self._load()["prompt_log"] if record.get("prompt_date") == prompt_date)

    def append_email_record(self, record: dict[str, Any]) -> None:
        """One row per email sent: {email_id, kind, alert_ids, sent_at}. Stops double sends."""
        state = self._load()
        state["email_log"].append(to_primitive(record))
        self._save(state)

    def emailed_alert_ids(self) -> set[str]:
        return {alert_id for record in self._load()["email_log"] for alert_id in record.get("alert_ids", [])}

    def last_email_at(self, kind: str) -> str | None:
        times = [record["sent_at"] for record in self._load()["email_log"] if record.get("kind") == kind]
        return max(times) if times else None

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
            last_promotion=raw.get("last_promotion"),
            size=raw.get("size"),
            color=raw.get("color"),
            added_via=raw.get("added_via", "behavior"),
            status=raw.get("status", "watching"),
            bought_at=raw.get("bought_at"),
        )

    @staticmethod
    def _decode_product(raw: dict[str, Any]) -> ProductContext:
        return ProductContext(
            name=raw.get("name"),
            url=raw.get("url"),
            current_price=raw.get("current_price"),
            currency=raw.get("currency", "CNY"),
            availability=Availability(raw.get("availability", "unknown")),
            confidence=raw.get("confidence", 0.0),
            promotion_text=raw.get("promotion_text"),
        )

    @classmethod
    def _decode_behavior_event(cls, raw: dict[str, Any]) -> BehaviorEvent:
        return BehaviorEvent(
            id=raw["id"],
            event_type=BehaviorEventType(raw["event_type"]),
            product=cls._decode_product(raw["product"]),
            session_id=raw["session_id"],
            occurred_at=raw["occurred_at"],
        )

    @staticmethod
    def _decode_pending(raw: dict[str, Any]) -> PendingConfirmation:
        product_raw = raw["product"]
        decision_raw = raw["decision"]
        return PendingConfirmation(
            id=raw["id"],
            expression=raw["expression"],
            product=JsonRepository._decode_product(product_raw),
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
