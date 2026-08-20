from __future__ import annotations

from .models import IntentDecision, MonitoringPolicy, ProductContext


class PolicyAgent:
    """Translate approved intent into auditable, deterministic monitoring rules."""

    def build(self, decision: IntentDecision, product: ProductContext) -> MonitoringPolicy:
        if decision.notify_restock or product.availability.value == "out_of_stock":
            frequency_hours = 6
        elif decision.target_price is not None:
            frequency_hours = 24
        else:
            frequency_hours = 72

        if decision.target_price is not None:
            target_price = decision.target_price
            drop_percent = None
            source = "user_threshold"
        else:
            target_price = None
            drop_percent = 0.10
            source = "deterministic_default_10_percent"

        return MonitoringPolicy(
            frequency_hours=frequency_hours,
            target_price=target_price,
            drop_percent=drop_percent,
            notify_restock=decision.notify_restock,
            source=source,
        )
