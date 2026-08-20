from __future__ import annotations

from .models import (
    Availability,
    MonitorDecision,
    Observation,
    ObservationSource,
    WishlistItem,
)


class MonitorAgent:
    """Evaluate observations with deterministic trigger and fallback rules."""

    def evaluate(
        self,
        item: WishlistItem,
        observation: Observation,
        deterministic_fallback: Observation | None = None,
    ) -> tuple[MonitorDecision, Observation | None]:
        selected = observation
        used_fallback = False

        if observation.source is ObservationSource.MODEL and (
            observation.confidence < item.policy.model_confidence_floor
            or not observation.evidence
        ):
            if deterministic_fallback and self._is_deterministic(deterministic_fallback):
                selected = deterministic_fallback
                used_fallback = True
            else:
                failures = item.consecutive_failures + 1
                return (
                    MonitorDecision(
                        accepted=False,
                        state=self._failure_state(item, failures),
                        reason="模型置信度或证据不足，确定性解析也不可用；本次不更新、不提醒。",
                        next_check_hours=self._failure_backoff(item.policy.frequency_hours, failures),
                    ),
                    None,
                )

        if not self._is_usable(selected):
            failures = item.consecutive_failures + 1
            return (
                MonitorDecision(
                    accepted=False,
                    state=self._failure_state(item, failures),
                    reason=selected.error or "观测缺少可验证的价格和库存信息。",
                    used_fallback=used_fallback,
                    next_check_hours=self._failure_backoff(item.policy.frequency_hours, failures),
                ),
                None,
            )

        alerts: list[str] = []
        if (
            item.policy.notify_restock
            and item.last_availability is Availability.OUT_OF_STOCK
            and selected.availability is Availability.IN_STOCK
        ):
            alerts.append("restock")

        threshold = item.policy.target_price
        if threshold is None and item.baseline_price is not None and item.policy.drop_percent is not None:
            threshold = item.baseline_price * (1 - item.policy.drop_percent)

        crossed_price_threshold = (
            threshold is not None
            and selected.price is not None
            and item.last_price is not None
            and item.last_price > threshold
            and selected.price <= threshold
        )
        if crossed_price_threshold:
            alerts.append("price_drop")

        return (
            MonitorDecision(
                accepted=True,
                state="alert" if alerts else "no_change",
                alert_types=alerts,
                reason="仅在补货状态跃迁或价格首次越过阈值时提醒。",
                used_fallback=used_fallback,
                next_check_hours=item.policy.frequency_hours,
            ),
            selected,
        )

    @staticmethod
    def _is_usable(observation: Observation) -> bool:
        if observation.error:
            return False
        return observation.price is not None or observation.availability is not Availability.UNKNOWN

    @classmethod
    def _is_deterministic(cls, observation: Observation) -> bool:
        return (
            observation.source in (ObservationSource.STRUCTURED, ObservationSource.MANUAL)
            and cls._is_usable(observation)
        )

    @staticmethod
    def _failure_backoff(base_hours: int, failures: int) -> int:
        # Retry sooner on the first failure, then avoid hammering a blocked site.
        if failures == 1:
            return min(base_hours, 6)
        if failures == 2:
            return max(base_hours, 12)
        return max(base_hours, 24)

    @staticmethod
    def _failure_state(item: WishlistItem, failures: int) -> str:
        return "manual_review" if failures >= item.policy.max_consecutive_failures else "unknown"
