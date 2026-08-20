from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..models import (
    Availability,
    IntentDecision,
    IntentKind,
    MonitorDecision,
    Observation,
    ObservationSource,
    OrchestrationResult,
    PendingConfirmation,
    ProductContext,
    Route,
    WishlistItem,
    to_primitive,
)
from ..monitor_agent import MonitorAgent
from ..orchestrator import ShoppingOrchestrator
from ..policy_agent import PolicyAgent
from ..ports import ShoppingRepository
from ..storage import JsonRepository
from .agents import ProductIntelligenceAgent, ShoppingSupervisorAgent, VerifierAgent
from .product_tools import parse_jsonld
from .provider import ArkResponsesProvider, ModelProvider
from .runtime import AgentRunner
from .types import EvidenceBoard, ProductPageSnapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConfidenceGate:
    """Final deterministic authority for any proposed wishlist write."""

    def decide(
        self,
        intent: IntentDecision,
        product: ProductContext,
        verification: dict[str, Any] | None,
        recommended_route: str,
        product_evidence_ok: bool,
    ) -> Route:
        if recommended_route == "ignore" and intent.kind is IntentKind.UNKNOWN:
            return Route.IGNORE
        approved = bool(verification and verification.get("verdict") == "approve")
        if (
            recommended_route == "auto_save"
            and intent.kind is IntentKind.EXPLICIT_PURCHASE
            and intent.confidence >= 0.80
            and product.is_resolved
            and product.confidence >= 0.75
            and product_evidence_ok
            and approved
        ):
            return Route.AUTO_SAVE
        return Route.ASK_CONFIRMATION


class MultiAgentShoppingSystem:
    """Three LLM agents around deterministic persistence and trigger engines."""

    def __init__(
        self,
        provider: ModelProvider | None = None,
        repository: ShoppingRepository | None = None,
        *,
        supervisor_model: str | None = None,
        product_model: str | None = None,
        verifier_model: str | None = None,
    ):
        self.repository = repository or JsonRepository()
        self.provider = provider or ArkResponsesProvider()
        self.runner = AgentRunner(self.provider)
        self.product_agent = ProductIntelligenceAgent(
            self.runner,
            product_model or os.getenv("DOUBAO_PRODUCT_MODEL", "doubao-seed-2-0-lite-260215"),
        )
        self.verifier = VerifierAgent(
            self.runner,
            verifier_model or os.getenv("DOUBAO_VERIFIER_MODEL", "doubao-seed-2-0-pro-260215"),
        )
        self.supervisor = ShoppingSupervisorAgent(
            self.runner,
            supervisor_model or os.getenv("DOUBAO_SUPERVISOR_MODEL", "doubao-seed-2-0-lite-260215"),
            self.product_agent,
            self.verifier,
            self.repository,
        )
        self.confidence_gate = ConfidenceGate()
        self.policy_engine = PolicyAgent()
        self.trigger_engine = MonitorAgent()
        self.confirmation_handler = ShoppingOrchestrator(repository=self.repository)

    async def handle_expression(
        self,
        expression: str,
        *,
        product: ProductContext | None = None,
        snapshot: ProductPageSnapshot | None = None,
    ) -> dict[str, Any]:
        product = product or ProductContext()
        snapshot = snapshot or ProductPageSnapshot(url=product.url or "")
        board = EvidenceBoard()
        if product.is_resolved and product.confidence >= 0.75:
            board.add(
                source="content_script",
                kind="provided_product_context",
                payload={
                    **to_primitive(product),
                    "evidence": ["trusted current-page product context"],
                },
            )
        try:
            supervisor_run = await self.supervisor.run(
                expression=expression,
                product=product,
                snapshot=snapshot,
                board=board,
            )
        except (RuntimeError, ValueError) as exc:
            return self._degraded_expression_fallback(expression, product, snapshot, exc)
        output = supervisor_run.output
        intent = self._decode_intent(output.get("intent", {}))
        rule_intent = self.supervisor.rule_intent.classify(expression, product)
        strong_rule_intent = bool(
            rule_intent.kind is IntentKind.EXPLICIT_PURCHASE
            and rule_intent.confidence >= 0.80
            and any(
                signal in ("price_threshold", "restock_request")
                or signal.startswith("explicit:")
                for signal in rule_intent.signals
            )
        )
        negative_rule_override = bool(
            rule_intent.kind is IntentKind.UNKNOWN
            and rule_intent.route is Route.IGNORE
            and "negative_intent" in rule_intent.signals
        )
        if strong_rule_intent and (
            intent.kind is not IntentKind.EXPLICIT_PURCHASE
            or intent.confidence < rule_intent.confidence
        ):
            intent = rule_intent
        elif negative_rule_override:
            intent = rule_intent
        product_entry = self._latest_board_entry(board, "product_hypothesis")
        product_hypothesis = product_entry.get("payload") if product_entry else None
        # A specialist result is canonical when present; the Supervisor cannot silently
        # replace evidence-backed product facts in its final synthesis.
        resolved_product = self._decode_product(
            product_hypothesis or output.get("product", {}), product
        )
        recommended_route = output.get("recommended_route", "ask_confirmation")
        if strong_rule_intent:
            recommended_route = "auto_save"
        elif negative_rule_override:
            recommended_route = "ignore"
        verification_board_entry = self._latest_board_entry(board, "verification")
        verification_entry = (
            verification_board_entry.get("payload") if verification_board_entry else None
        )
        verification = (
            verification_entry.get("result") if verification_entry else None
        )
        product_evidence_ok = bool(
            (product_hypothesis and product_hypothesis.get("evidence"))
            or (product.is_resolved and product.confidence >= 0.75)
        )
        verifier_saw_product_evidence = bool(
            not product_entry
            or (
                verification_entry
                and product_entry["id"]
                in verification_entry.get("evidence_ids_at_start", [])
            )
        )

        final_proposal = {
            "intent": to_primitive(intent),
            "product": to_primitive(resolved_product),
        }
        verified_proposal = verification_entry.get("proposal") if verification_entry else None
        if recommended_route == "auto_save" and (
            not verification
            or not verifier_saw_product_evidence
            or not self._same_save_proposal(final_proposal, verified_proposal)
        ):
            try:
                verification_run = await self.verifier.verify(
                    action_type="save_item",
                    proposal=final_proposal,
                    expression=expression,
                    board=board,
                )
                verification = verification_run.output
            except (RuntimeError, ValueError) as exc:
                verification = self._verification_failure(exc)

        route = self.confidence_gate.decide(
            intent,
            resolved_product,
            verification,
            recommended_route,
            product_evidence_ok,
        )
        intent.route = route
        if route is Route.AUTO_SAVE:
            item, created = self._commit_item(intent, resolved_product)
            message = "已加入监控清单。" if created else "该商品已经在监控清单中。"
            pending = None
        elif route is Route.IGNORE:
            item = None
            pending = None
            message = output.get("question") or "没有创建监控任务。"
        else:
            item = None
            question = (
                (verification or {}).get("recommended_question")
                or output.get("question")
                or self._default_question(intent, resolved_product)
            )
            pending = PendingConfirmation(
                id=str(uuid4()),
                expression=expression,
                product=resolved_product,
                decision=IntentDecision(
                    kind=intent.kind,
                    confidence=intent.confidence,
                    route=Route.ASK_CONFIRMATION,
                    signals=intent.signals,
                    missing_fields=intent.missing_fields,
                    rationale="LLM proposal did not pass the deterministic write gate.",
                    target_price=intent.target_price,
                    notify_restock=intent.notify_restock,
                ),
                question=question,
                created_at=utc_now(),
            )
            self.repository.save_pending(pending)
            message = question

        self.repository.append_audit(
            {
                "id": str(uuid4()),
                "event": "multiagent_expression_completed",
                "payload": {
                    "route": route.value,
                    "supervisor": output,
                    "verification": verification,
                    "evidence": board.snapshot(),
                    "trace": supervisor_run.trace,
                },
                "created_at": utc_now(),
            }
        )
        return {
            "route": route.value,
            "message": message,
            "intent": to_primitive(intent),
            "product": to_primitive(resolved_product),
            "verification": verification,
            "item": to_primitive(item),
            "pending": to_primitive(pending),
            "agent_trace": supervisor_run.trace,
            "evidence_board": board.snapshot(),
        }

    async def monitor_item(
        self,
        item_id: str,
        *,
        snapshot: ProductPageSnapshot,
    ) -> dict[str, Any]:
        item = self.repository.get_item(item_id)
        if not item:
            raise KeyError(f"Unknown item: {item_id}")
        board = EvidenceBoard()
        investigation = await self.product_agent.run(
            mode="observe",
            expression="scheduled product observation",
            product=ProductContext(
                name=item.name,
                url=item.url,
                current_price=item.last_price,
                currency=item.currency,
                availability=item.last_availability,
                confidence=item.product_confidence,
            ),
            snapshot=snapshot,
            previous_state={
                "price": item.last_price,
                "availability": item.last_availability.value,
            },
            board=board,
        )
        candidate = investigation.output
        method = candidate.get("extraction_method", "model")
        source = (
            ObservationSource.STRUCTURED
            if method in ("jsonld", "meta")
            else ObservationSource.MODEL
        )
        observation = Observation(
            price=self._as_float(candidate.get("price")),
            availability=self._availability(candidate.get("availability")),
            confidence=self._as_float(candidate.get("confidence")) or 0.0,
            source=source,
            checked_at=utc_now(),
            evidence=[str(value) for value in candidate.get("evidence", [])],
        )
        preliminary, _ = self.trigger_engine.evaluate(item, observation)
        verification = None
        if preliminary.alert_types:
            try:
                verification = (
                    await self.verifier.verify(
                        action_type="create_alert",
                        proposal={
                            "item": to_primitive(item),
                            "observation": to_primitive(observation),
                            "alert_types": preliminary.alert_types,
                        },
                        expression="scheduled product observation",
                        board=board,
                    )
                ).output
            except (RuntimeError, ValueError) as exc:
                verification = self._verification_failure(exc)
            if verification.get("verdict") != "approve":
                self.repository.append_audit(
                    {
                        "id": str(uuid4()),
                        "event": "alert_blocked_by_verifier",
                        "payload": {"item_id": item_id, "verification": verification},
                        "created_at": utc_now(),
                    }
                )
                return {
                    "accepted": False,
                    "state": "manual_review",
                    "alert_types": [],
                    "reason": "Potential alert did not pass independent verification.",
                    "verification": verification,
                    "evidence_board": board.snapshot(),
                }

        committed = self.confirmation_handler.check_item(item_id, observation)
        return {
            **to_primitive(committed),
            "verification": verification,
            "evidence_board": board.snapshot(),
        }

    def confirm(self, pending_id: str, approved: bool, **updates) -> OrchestrationResult:
        return self.confirmation_handler.confirm(pending_id, approved, **updates)

    def _commit_item(self, intent: IntentDecision, product: ProductContext) -> tuple[WishlistItem, bool]:
        existing = self.repository.find_item_by_url(product.url or "")
        if existing:
            return existing, False
        item = WishlistItem(
            id=str(uuid4()),
            name=product.name or "",
            url=product.url or "",
            currency=product.currency,
            baseline_price=product.current_price,
            last_price=product.current_price,
            last_availability=product.availability,
            intent_kind=intent.kind,
            intent_confidence=intent.confidence,
            product_confidence=product.confidence,
            policy=self.policy_engine.build(intent, product),
            created_at=utc_now(),
        )
        self.repository.save_item(item)
        return item, True

    def _degraded_expression_fallback(
        self,
        expression: str,
        product: ProductContext,
        snapshot: ProductPageSnapshot,
        error: Exception,
    ) -> dict[str, Any]:
        resolved = product
        parsed = parse_jsonld(snapshot)
        if parsed.get("found"):
            resolved = ProductContext(
                name=parsed.get("name"),
                url=parsed.get("url"),
                current_price=self._as_float(parsed.get("price")),
                currency=parsed.get("currency", "CNY"),
                availability=self._availability(parsed.get("availability")),
                confidence=0.98,
            )
        fallback = self.confirmation_handler.handle_expression(expression, resolved)
        self.repository.append_audit(
            {
                "id": str(uuid4()),
                "event": "multiagent_degraded_to_deterministic",
                "payload": {"error": f"{type(error).__name__}:{error}", "route": fallback.route.value},
                "created_at": utc_now(),
            }
        )
        return {
            "route": fallback.route.value,
            "message": fallback.message,
            "intent": to_primitive(fallback.intent),
            "product": to_primitive(resolved),
            "verification": None,
            "item": to_primitive(fallback.item),
            "pending": to_primitive(fallback.pending),
            "agent_trace": [{"degraded": "deterministic_fallback"}],
            "evidence_board": [],
            "degraded": True,
            "error": f"{type(error).__name__}:{error}",
        }

    @staticmethod
    def _decode_intent(raw: dict[str, Any]) -> IntentDecision:
        try:
            kind = IntentKind(raw.get("kind", "unknown"))
        except ValueError:
            kind = IntentKind.UNKNOWN
        confidence = MultiAgentShoppingSystem._as_float(raw.get("confidence")) or 0.0
        return IntentDecision(
            kind=kind,
            confidence=max(0.0, min(confidence, 1.0)),
            route=Route.ASK_CONFIRMATION,
            signals=[str(value) for value in raw.get("evidence", [])],
            target_price=MultiAgentShoppingSystem._as_float(raw.get("target_price")),
            notify_restock=bool(raw.get("notify_restock", False)),
        )

    @staticmethod
    def _decode_product(raw: dict[str, Any], fallback: ProductContext) -> ProductContext:
        return ProductContext(
            name=raw.get("name") or fallback.name,
            url=raw.get("url") or fallback.url,
            current_price=(
                MultiAgentShoppingSystem._as_float(raw.get("price"))
                if raw.get("price") is not None
                else fallback.current_price
            ),
            currency=raw.get("currency") or fallback.currency,
            availability=MultiAgentShoppingSystem._availability(
                raw.get("availability", fallback.availability.value)
            ),
            confidence=MultiAgentShoppingSystem._as_float(raw.get("confidence")) or fallback.confidence,
        )

    @staticmethod
    def _latest_board_payload(board: EvidenceBoard, kind: str):
        entry = MultiAgentShoppingSystem._latest_board_entry(board, kind)
        return entry.get("payload") if entry else None

    @staticmethod
    def _latest_board_entry(board: EvidenceBoard, kind: str):
        for entry in reversed(board.entries):
            if entry["kind"] == kind:
                return entry
        return None

    @staticmethod
    def _same_save_proposal(current: dict[str, Any], verified: dict[str, Any] | None) -> bool:
        if not verified:
            return False
        current_intent = current.get("intent", {})
        verified_intent = verified.get("intent", {})
        current_product = current.get("product", {})
        verified_product = verified.get("product", {})
        return (
            current_intent.get("kind") == verified_intent.get("kind")
            and current_intent.get("target_price") == verified_intent.get("target_price")
            and current_intent.get("notify_restock") == verified_intent.get("notify_restock")
            and current_product.get("name") == verified_product.get("name")
            and current_product.get("url") == verified_product.get("url")
            and current_product.get("current_price")
            == (
                verified_product.get("current_price")
                if verified_product.get("current_price") is not None
                else verified_product.get("price")
            )
            and current_product.get("availability") == verified_product.get("availability")
        )

    @staticmethod
    def _verification_failure(error: Exception) -> dict[str, Any]:
        return {
            "verdict": "ask_user",
            "confidence": 0.0,
            "reasons": [f"verifier_unavailable:{type(error).__name__}"],
            "missing_fields": [],
            "recommended_question": "独立校验暂时不可用，要确认加入监控清单吗？",
        }

    @staticmethod
    def _default_question(intent: IntentDecision, product: ProductContext) -> str:
        name = product.name or "当前商品"
        if intent.kind is IntentKind.FUZZY_INTEREST:
            return f"你似乎对“{name}”感兴趣，要把它加入监控清单吗？"
        return f"要把“{name}”加入监控清单吗？我会在你确认后保存。"

    @staticmethod
    def _availability(value: Any) -> Availability:
        try:
            return Availability(str(value))
        except ValueError:
            return Availability.UNKNOWN

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
