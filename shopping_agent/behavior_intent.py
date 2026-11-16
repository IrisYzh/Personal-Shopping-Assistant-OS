from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import (
    BehaviorEvent,
    BehaviorEventType,
    BehaviorSummary,
    IntentDecision,
    IntentKind,
    ProductContext,
    Route,
)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class BehaviorIntentAgent:
    """Infer product intent from auditable behavior events, not a single utterance."""

    def __init__(self, window_days: int = 7, fuzzy_view_threshold: int = 3, min_sessions: int = 2):
        self.window_days = window_days
        self.fuzzy_view_threshold = fuzzy_view_threshold
        self.min_sessions = min_sessions

    def classify(
        self,
        events: list[BehaviorEvent],
        product: ProductContext,
        *,
        now: datetime | None = None,
    ) -> tuple[IntentDecision, BehaviorSummary]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_start = now - timedelta(days=self.window_days)
        relevant = sorted(
            (
                event
                for event in events
                if event.product.url == product.url and window_start <= parse_time(event.occurred_at) <= now
            ),
            key=lambda event: parse_time(event.occurred_at),
        )
        views = [event for event in relevant if event.event_type is BehaviorEventType.VIEW]
        sessions = {event.session_id for event in views if event.session_id}
        cart_active = self._active_state(
            relevant, BehaviorEventType.ADD_TO_CART, BehaviorEventType.REMOVE_FROM_CART
        )
        wishlist_active = self._active_state(
            relevant, BehaviorEventType.ADD_TO_WISHLIST, BehaviorEventType.REMOVE_FROM_WISHLIST
        )
        strong_actions = [
            event
            for event in relevant
            if event.event_type in (BehaviorEventType.ADD_TO_CART, BehaviorEventType.ADD_TO_WISHLIST)
        ]
        summary = BehaviorSummary(
            product_url=product.url or "",
            window_days=self.window_days,
            view_count=len(views),
            distinct_session_count=len(sessions),
            cart_active=cart_active,
            wishlist_active=wishlist_active,
            event_count=len(relevant),
            first_seen_at=relevant[0].occurred_at if relevant else None,
            last_seen_at=relevant[-1].occurred_at if relevant else None,
            last_strong_action=strong_actions[-1].event_type.value if strong_actions else None,
        )

        if cart_active or wishlist_active:
            source = "add_to_cart" if cart_active else "add_to_wishlist"
            confidence = 0.99 if cart_active else 0.96
            resolved = product.is_resolved and product.confidence >= 0.75
            return (
                IntentDecision(
                    kind=IntentKind.EXPLICIT_PURCHASE,
                    confidence=confidence,
                    route=Route.AUTO_SAVE if resolved else Route.ASK_CONFIRMATION,
                    signals=[f"behavior:{source}", f"views_7d:{len(views)}"],
                    missing_fields=[] if resolved else self._missing_product_fields(product),
                    rationale="用户已加购或收藏，属于可审计的明确购买信号。",
                    notify_restock=True,
                ),
                summary,
            )

        repeated_interest = (
            len(views) >= self.fuzzy_view_threshold and len(sessions) >= self.min_sessions
        )
        if repeated_interest:
            confidence = min(0.90, 0.64 + (len(views) - self.fuzzy_view_threshold) * 0.03)
            return (
                IntentDecision(
                    kind=IntentKind.FUZZY_INTEREST,
                    confidence=round(confidence, 2),
                    route=Route.ASK_CONFIRMATION,
                    signals=[f"behavior:repeat_view", f"views_7d:{len(views)}", f"sessions_7d:{len(sessions)}"],
                    rationale="同一商品被跨会话多次浏览，但没有加购或收藏，进入每日兴趣确认。",
                ),
                summary,
            )

        return (
            IntentDecision(
                kind=IntentKind.UNKNOWN,
                confidence=0.30,
                route=Route.IGNORE,
                signals=[f"behavior:insufficient", f"views_7d:{len(views)}"],
                rationale="行为频次尚不足，不创建任务也不打扰用户。",
            ),
            summary,
        )

    @staticmethod
    def _active_state(
        events: list[BehaviorEvent], add: BehaviorEventType, remove: BehaviorEventType
    ) -> bool:
        relevant = [event for event in events if event.event_type in (add, remove)]
        return bool(relevant and relevant[-1].event_type is add)

    @staticmethod
    def _missing_product_fields(product: ProductContext) -> list[str]:
        missing = []
        if not product.name:
            missing.append("product_name")
        if not product.url:
            missing.append("product_url")
        return missing
