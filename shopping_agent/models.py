from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class IntentKind(str, Enum):
    EXPLICIT_PURCHASE = "explicit_purchase"
    FUZZY_INTEREST = "fuzzy_interest"
    UNKNOWN = "unknown"


class Route(str, Enum):
    AUTO_SAVE = "auto_save"
    ASK_CONFIRMATION = "ask_confirmation"
    IGNORE = "ignore"


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


class ObservationSource(str, Enum):
    STRUCTURED = "structured"
    MODEL = "model"
    MANUAL = "manual"


class BehaviorEventType(str, Enum):
    VIEW = "view"
    ADD_TO_CART = "add_to_cart"
    ADD_TO_WISHLIST = "add_to_wishlist"
    REMOVE_FROM_CART = "remove_from_cart"
    REMOVE_FROM_WISHLIST = "remove_from_wishlist"


@dataclass(slots=True)
class ProductContext:
    name: str | None = None
    url: str | None = None
    current_price: float | None = None
    currency: str = "CNY"
    availability: Availability = Availability.UNKNOWN
    confidence: float = 0.0
    promotion_text: str | None = None

    @property
    def is_resolved(self) -> bool:
        return bool(self.name and self.url)


@dataclass(slots=True)
class IntentDecision:
    kind: IntentKind
    confidence: float
    route: Route
    signals: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    rationale: str = ""
    target_price: float | None = None
    notify_restock: bool = False


@dataclass(slots=True)
class MonitoringPolicy:
    frequency_hours: int
    target_price: float | None
    drop_percent: float | None
    notify_restock: bool
    source: str
    notify_promotions: bool = True
    model_confidence_floor: float = 0.75
    max_consecutive_failures: int = 3


@dataclass(slots=True)
class WishlistItem:
    id: str
    name: str
    url: str
    currency: str
    baseline_price: float | None
    last_price: float | None
    last_availability: Availability
    intent_kind: IntentKind
    intent_confidence: float
    product_confidence: float
    policy: MonitoringPolicy
    created_at: str
    consecutive_failures: int = 0
    last_promotion: str | None = None
    size: str | None = None
    color: str | None = None
    added_via: str = "behavior"
    status: str = "watching"  # "watching" or "bought". Bought items are kept but not checked.
    bought_at: str | None = None


@dataclass(slots=True)
class BehaviorEvent:
    id: str
    event_type: BehaviorEventType
    product: ProductContext
    session_id: str
    occurred_at: str


@dataclass(slots=True)
class BehaviorSummary:
    product_url: str
    window_days: int
    view_count: int
    distinct_session_count: int
    cart_active: bool
    wishlist_active: bool
    event_count: int
    first_seen_at: str | None
    last_seen_at: str | None
    last_strong_action: str | None


@dataclass(slots=True)
class PendingConfirmation:
    id: str
    expression: str
    product: ProductContext
    decision: IntentDecision
    question: str
    created_at: str


@dataclass(slots=True)
class Observation:
    price: float | None
    availability: Availability
    confidence: float
    source: ObservationSource
    checked_at: str
    promotion_text: str | None = None
    evidence: list[str] = field(default_factory=list)
    error: str | None = None
    size: str | None = None  # Set when availability is for the watched size, not the whole product.


@dataclass(slots=True)
class MonitorDecision:
    accepted: bool
    state: str
    alert_types: list[str] = field(default_factory=list)
    reason: str = ""
    used_fallback: bool = False
    next_check_hours: int | None = None


@dataclass(slots=True)
class OrchestrationResult:
    route: Route
    message: str
    intent: IntentDecision | None = None
    item: WishlistItem | None = None
    pending: PendingConfirmation | None = None
    behavior_event: BehaviorEvent | None = None
    behavior_summary: BehaviorSummary | None = None
    deferred_to_daily_digest: bool = False
    prompt_now: bool = False


def to_primitive(value: Any) -> Any:
    """Convert nested dataclasses and enums into JSON-compatible values."""
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_primitive(item) for item in value]
    return value
