from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from .behavior_intent import BehaviorIntentAgent, parse_time
from .intent_agent import IntentAgent
from .models import (
    Availability,
    BehaviorEvent,
    BehaviorEventType,
    IntentKind,
    Observation,
    OrchestrationResult,
    PendingConfirmation,
    ProductContext,
    Route,
    WishlistItem,
    to_primitive,
)
from .monitor_agent import MonitorAgent
from .policy_agent import PolicyAgent
from .ports import ShoppingRepository
from .storage import JsonRepository


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ShoppingOrchestrator:
    """Coordinate specialist agents and own every persistence side effect."""

    def __init__(
        self,
        repository: ShoppingRepository | None = None,
        intent_agent: IntentAgent | None = None,
        policy_agent: PolicyAgent | None = None,
        monitor_agent: MonitorAgent | None = None,
        behavior_intent_agent: BehaviorIntentAgent | None = None,
        max_prompts_per_day: int = 1,
    ):
        self.repository = repository or JsonRepository()
        self.intent_agent = intent_agent or IntentAgent()
        self.policy_agent = policy_agent or PolicyAgent()
        self.monitor_agent = monitor_agent or MonitorAgent()
        self.behavior_intent_agent = behavior_intent_agent or BehaviorIntentAgent()
        self.max_prompts_per_day = max_prompts_per_day

    def handle_expression(
        self,
        expression: str,
        product: ProductContext | None = None,
    ) -> OrchestrationResult:
        product = product or ProductContext()
        decision = self.intent_agent.classify(expression, product)
        self._audit("intent_classified", {"expression": expression, "decision": decision, "product": product})

        if decision.route is Route.IGNORE:
            return OrchestrationResult(route=Route.IGNORE, message=decision.rationale, intent=decision)

        if decision.route is Route.AUTO_SAVE:
            item, created = self._persist_item(product, decision)
            message = "已加入监控清单。" if created else "该商品已经在监控清单中，没有重复写入。"
            return OrchestrationResult(route=Route.AUTO_SAVE, message=message, intent=decision, item=item)

        pending = PendingConfirmation(
            id=str(uuid4()),
            expression=expression,
            product=product,
            decision=decision,
            question=self._build_question(decision, product),
            created_at=utc_now(),
        )
        self.repository.save_pending(pending)
        self._audit("confirmation_requested", {"pending": pending})
        return OrchestrationResult(
            route=Route.ASK_CONFIRMATION,
            message=pending.question,
            intent=decision,
            pending=pending,
        )

    def record_behavior(
        self,
        event_type: BehaviorEventType | str,
        product: ProductContext,
        *,
        session_id: str,
        occurred_at: str | None = None,
    ) -> OrchestrationResult:
        """Record one browser event and route only meaningful product-level intent."""
        if not product.url:
            raise ValueError("A product URL is required to aggregate behavior by product.")
        event = BehaviorEvent(
            id=str(uuid4()),
            event_type=BehaviorEventType(event_type),
            product=product,
            session_id=session_id,
            occurred_at=occurred_at or utc_now(),
        )
        self.repository.append_behavior_event(event)
        decision, summary = self.behavior_intent_agent.classify(
            self.repository.list_behavior_events(product.url),
            product,
            now=parse_time(event.occurred_at),
        )
        self._audit(
            "behavior_intent_classified",
            {"event": event, "decision": decision, "summary": summary},
        )

        if decision.route is Route.AUTO_SAVE:
            item, created = self._persist_item(product, decision)
            message = (
                "检测到加购或收藏，已默认开启降价、库存和促销监控。"
                if created
                else "该商品已在监控清单中，已保留默认监控。"
            )
            return OrchestrationResult(
                route=Route.AUTO_SAVE,
                message=message,
                intent=decision,
                item=item,
                behavior_event=event,
                behavior_summary=summary,
            )

        if decision.kind is IntentKind.FUZZY_INTEREST:
            return self._route_fuzzy_interest(event, product, decision, summary)

        return OrchestrationResult(
            route=Route.IGNORE,
            message="已记录浏览行为，当前频次不足，不创建监控或打扰用户。",
            intent=decision,
            behavior_event=event,
            behavior_summary=summary,
        )

    def _route_fuzzy_interest(self, event, product, decision, summary) -> OrchestrationResult:
        """Ask on the page right away, but at most once per product and once per day."""
        if self.repository.find_item_by_url(product.url) or self.repository.was_prompted(product.url):
            return OrchestrationResult(
                route=Route.IGNORE,
                message="这个商品已经问过或已在心愿单中，不再打扰。",
                intent=decision,
                behavior_event=event,
                behavior_summary=summary,
            )
        prompt_date = parse_time(event.occurred_at).date().isoformat()
        if self.repository.prompt_count_on(prompt_date) >= self.max_prompts_per_day:
            return OrchestrationResult(
                route=Route.ASK_CONFIRMATION,
                message="今天已经弹过一次询问，这个商品放进汇总邮件里再问。",
                intent=decision,
                behavior_event=event,
                behavior_summary=summary,
                deferred_to_daily_digest=True,
            )
        pending = PendingConfirmation(
            id=str(uuid4()),
            expression=f"behavior_prompt:{product.url}",
            product=product,
            decision=decision,
            question=(
                f"你最近 {summary.window_days} 天看了“{product.name or '这个商品'}” {summary.view_count} 次。"
                "要加入心愿单吗？可以顺便填上你的尺码和颜色，有货或降价时提醒你。"
            ),
            created_at=event.occurred_at,
        )
        self.repository.save_pending(pending)
        self.repository.append_prompt_record(
            {
                "id": str(uuid4()),
                "product_url": product.url,
                "prompt_date": prompt_date,
                "pending_id": pending.id,
                "created_at": event.occurred_at,
            }
        )
        self._audit("wishlist_prompt_shown", {"pending": pending, "summary": summary})
        return OrchestrationResult(
            route=Route.ASK_CONFIRMATION,
            message=pending.question,
            intent=decision,
            pending=pending,
            behavior_event=event,
            behavior_summary=summary,
            prompt_now=True,
        )

    def build_daily_interest_digest(self, *, now: str | None = None) -> list[PendingConfirmation]:
        """Create at most one fuzzy-interest question per product and calendar day."""
        now_value = now or utc_now()
        now_parsed = parse_time(now_value)
        digest_date = now_parsed.date().isoformat()
        events = self.repository.list_behavior_events()
        latest_product_by_url: dict[str, ProductContext] = {}
        for event in sorted(events, key=lambda item: parse_time(item.occurred_at)):
            if event.product.url:
                latest_product_by_url[event.product.url] = event.product

        prompts: list[PendingConfirmation] = []
        for product_url, product in latest_product_by_url.items():
            if self.repository.find_item_by_url(product_url):
                continue
            if self.repository.was_daily_digest_prompted(product_url, digest_date):
                continue
            if self.repository.was_prompted(product_url):
                continue
            decision, summary = self.behavior_intent_agent.classify(
                events, product, now=now_parsed
            )
            if decision.kind is not IntentKind.FUZZY_INTEREST:
                continue
            question = (
                f"你最近 {summary.window_days} 天浏览了“{product.name or '这个商品'}”"
                f" {summary.view_count} 次。你现在有购买意愿吗？"
                "如果有，希望降到多少元时提醒你？也可以选择暂不关注。"
            )
            pending = PendingConfirmation(
                id=str(uuid4()),
                expression=f"behavior_daily_digest:{product_url}",
                product=product,
                decision=decision,
                question=question,
                created_at=now_value,
            )
            self.repository.save_pending(pending)
            self.repository.append_daily_digest_record(
                {
                    "id": str(uuid4()),
                    "product_url": product_url,
                    "digest_date": digest_date,
                    "pending_id": pending.id,
                    "summary": summary,
                    "created_at": now_value,
                }
            )
            self._audit("daily_interest_confirmation_requested", {"pending": pending, "summary": summary})
            prompts.append(pending)
        return prompts

    def confirm(
        self,
        pending_id: str,
        approved: bool,
        *,
        name: str | None = None,
        url: str | None = None,
        current_price: float | None = None,
        target_price: float | None = None,
        notify_restock: bool | None = None,
        size: str | None = None,
        color: str | None = None,
    ) -> OrchestrationResult:
        pending = self.repository.get_pending(pending_id)
        if not pending:
            raise KeyError(f"Unknown confirmation: {pending_id}")

        self.repository.delete_pending(pending_id)
        if not approved:
            self._audit("confirmation_declined", {"pending_id": pending_id})
            return OrchestrationResult(route=Route.IGNORE, message="已取消，没有写入监控清单。")

        product = replace(
            pending.product,
            name=name or pending.product.name,
            url=url or pending.product.url,
            current_price=current_price if current_price is not None else pending.product.current_price,
            confidence=1.0,
        )
        if not product.is_resolved:
            # Explicit approval is not permission to store an unusable entity.
            retry = replace(
                pending,
                question="还缺少商品名称或链接，请补充后再确认。",
            )
            self.repository.save_pending(retry)
            return OrchestrationResult(
                route=Route.ASK_CONFIRMATION,
                message=retry.question,
                intent=retry.decision,
                pending=retry,
            )

        decision = replace(
            pending.decision,
            kind=IntentKind.EXPLICIT_PURCHASE,
            route=Route.AUTO_SAVE,
            confidence=1.0,
            target_price=target_price if target_price is not None else pending.decision.target_price,
            notify_restock=(
                notify_restock
                if notify_restock is not None
                else pending.decision.notify_restock
                or pending.expression.startswith(("behavior_daily_digest:", "behavior_prompt:"))
            ),
            rationale="用户已显式确认，允许入库。",
        )
        added_via = "prompt" if pending.expression.startswith("behavior_prompt:") else "confirmation"
        item, created = self._persist_item(
            product, decision, size=_clean(size), color=_clean(color), added_via=added_via
        )
        self._audit("confirmation_approved", {"pending_id": pending_id, "item_id": item.id})
        message = "确认完成，已加入监控清单。" if created else "确认完成，该商品已在清单中。"
        return OrchestrationResult(route=Route.AUTO_SAVE, message=message, intent=decision, item=item)

    def check_item(
        self,
        item_id: str,
        observation: Observation,
        deterministic_fallback: Observation | None = None,
    ):
        item = self.repository.get_item(item_id)
        if not item:
            raise KeyError(f"Unknown item: {item_id}")

        decision, selected = self.monitor_agent.evaluate(item, observation, deterministic_fallback)
        self.repository.append_observation(
            {
                "item_id": item_id,
                "received": observation,
                "fallback": deterministic_fallback,
                "selected": selected,
                "decision": decision,
                "recorded_at": utc_now(),
            }
        )

        if selected and decision.accepted:
            previous_price = item.last_price
            item.last_price = selected.price if selected.price is not None else item.last_price
            if selected.availability is not Availability.UNKNOWN:
                item.last_availability = selected.availability
            if selected.promotion_text:
                item.last_promotion = selected.promotion_text
            item.consecutive_failures = 0
            self.repository.save_item(item)
            for alert_type in decision.alert_types:
                self.repository.append_alert(
                    {
                        "id": str(uuid4()),
                        "item_id": item.id,
                        "type": alert_type,
                        "old_price": previous_price,
                        "new_price": selected.price,
                        "availability": selected.availability,
                        "size": selected.size,
                        "promotion_text": selected.promotion_text,
                        "created_at": utc_now(),
                        "dedupe_key": f"{item.id}:{alert_type}:{selected.checked_at}",
                    }
                )
        else:
            item.consecutive_failures += 1
            self.repository.save_item(item)

        self._audit("monitor_evaluated", {"item_id": item_id, "decision": decision})
        return decision

    def update_preferences(
        self, item_id: str, *, size: str | None = None, color: str | None = None
    ) -> WishlistItem:
        """Let the user set or change the size and color the agent should watch."""
        item = self.repository.get_item(item_id)
        if not item:
            raise KeyError(f"Unknown item: {item_id}")
        if size is not None:
            item.size = _clean(size)
        if color is not None:
            item.color = _clean(color)
        self.repository.save_item(item)
        self._audit("preferences_updated", {"item_id": item_id, "size": item.size, "color": item.color})
        return item

    def set_bought(self, item_id: str, bought: bool) -> WishlistItem:
        """Mark an item as bought (stop watching, keep the record) or undo that."""
        item = self.repository.get_item(item_id)
        if not item:
            raise KeyError(f"Unknown item: {item_id}")
        item.status = "bought" if bought else "watching"
        item.bought_at = utc_now() if bought else None
        self.repository.save_item(item)
        self._audit("item_bought" if bought else "item_unbought", {"item_id": item_id})
        return item

    def remove_item(self, item_id: str) -> bool:
        removed = self.repository.delete_item(item_id)
        if removed:
            self._audit("item_removed", {"item_id": item_id})
        return removed

    def _persist_item(
        self,
        product: ProductContext,
        decision,
        *,
        size: str | None = None,
        color: str | None = None,
        added_via: str | None = None,
    ) -> tuple[WishlistItem, bool]:
        if not product.name or not product.url:
            raise ValueError("A resolved product name and URL are required before persistence.")
        existing = self.repository.find_item_by_url(product.url)
        if existing:
            if decision.kind is IntentKind.EXPLICIT_PURCHASE and any(
                signal.startswith("behavior:") for signal in decision.signals
            ):
                existing.intent_kind = IntentKind.EXPLICIT_PURCHASE
                existing.intent_confidence = max(existing.intent_confidence, decision.confidence)
                existing.policy.notify_restock = True
                existing.policy.notify_promotions = True
            if size:
                existing.size = size
            if color:
                existing.color = color
            self.repository.save_item(existing)
            return existing, False

        item = WishlistItem(
            id=str(uuid4()),
            name=product.name,
            url=product.url,
            currency=product.currency,
            baseline_price=product.current_price,
            last_price=product.current_price,
            last_availability=product.availability,
            intent_kind=decision.kind,
            intent_confidence=decision.confidence,
            product_confidence=product.confidence,
            policy=self.policy_agent.build(decision, product),
            created_at=utc_now(),
            last_promotion=product.promotion_text,
            size=size,
            color=color,
            added_via=added_via or _added_via_from_signals(decision.signals),
        )
        self.repository.save_item(item)
        self._audit("item_created", {"item": item})
        return item, True

    @staticmethod
    def _build_question(decision, product: ProductContext) -> str:
        product_name = product.name or "当前商品"
        if decision.kind is IntentKind.FUZZY_INTEREST:
            return f"你似乎对“{product_name}”感兴趣。要把它加入监控清单吗？我只会在你确认后保存。"
        if decision.missing_fields:
            return "我理解你可能想监控它，但还缺少商品名称或链接。要补充信息并加入清单吗？"
        return f"要把“{product_name}”加入监控清单吗？当前判断置信度不足，不会自动保存。"

    def _audit(self, event: str, payload: dict) -> None:
        self.repository.append_audit(
            {"id": str(uuid4()), "event": event, "payload": to_primitive(payload), "created_at": utc_now()}
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _added_via_from_signals(signals: list[str]) -> str:
    if "behavior:add_to_cart" in signals:
        return "add_to_cart"
    if "behavior:add_to_wishlist" in signals:
        return "add_to_wishlist"
    return "text"
