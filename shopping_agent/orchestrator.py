from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from .intent_agent import IntentAgent
from .models import (
    Availability,
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
    ):
        self.repository = repository or JsonRepository()
        self.intent_agent = intent_agent or IntentAgent()
        self.policy_agent = policy_agent or PolicyAgent()
        self.monitor_agent = monitor_agent or MonitorAgent()

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
            route=Route.AUTO_SAVE,
            confidence=1.0,
            target_price=target_price if target_price is not None else pending.decision.target_price,
            notify_restock=(
                notify_restock if notify_restock is not None else pending.decision.notify_restock
            ),
            rationale="用户已显式确认，允许入库。",
        )
        item, created = self._persist_item(product, decision)
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
                        "created_at": utc_now(),
                        "dedupe_key": f"{item.id}:{alert_type}:{selected.checked_at}",
                    }
                )
        else:
            item.consecutive_failures += 1
            self.repository.save_item(item)

        self._audit("monitor_evaluated", {"item_id": item_id, "decision": decision})
        return decision

    def _persist_item(self, product: ProductContext, decision) -> tuple[WishlistItem, bool]:
        if not product.name or not product.url:
            raise ValueError("A resolved product name and URL are required before persistence.")
        existing = self.repository.find_item_by_url(product.url)
        if existing:
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
