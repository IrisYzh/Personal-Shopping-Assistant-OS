from __future__ import annotations

import re

from .models import IntentDecision, IntentKind, ProductContext, Route


class IntentAgent:
    """Classify expressions without deciding persistence itself."""

    EXPLICIT_PATTERNS = (
        "我想买",
        "我想要",
        "准备买",
        "要买",
        "帮我监控",
        "帮我盯",
        "盯一下",
        "加入心愿单",
        "收藏这个",
        "降价提醒",
        "有货提醒",
        "补货提醒",
        "便宜到",
        "有活动提醒",
        "替我留意",
        "跌破",
        "打折再下单",
        "想买清单",
        "蹲一下",
        "蹲一波",
        "到了告诉我",
        "通知我",
        "帮我看着",
    )
    FUZZY_PATTERNS = (
        "有点心动",
        "好像不错",
        "挺喜欢",
        "感兴趣",
        "先看看",
        "考虑一下",
        "被种草",
        "可能会买",
        "也许会买",
        "好看",
        "纠结",
        "可能会考虑",
        "收藏灵感",
    )
    NEGATIVE_PATTERNS = (
        "不想买",
        "我不要买",
        "不要买了",
        "别监控",
        "取消监控",
        "移出心愿单",
        "不感兴趣",
        "不用记录",
        "不需要提醒",
        "别提醒",
        "已经买过",
    )
    PRICE_PATTERN = re.compile(
        r"(?:降到|便宜到|低于|跌破|不超过|预算(?:是|在)?|目标价(?:是|在)?)\s*[¥￥$]?\s*(\d+(?:\.\d{1,2})?)"
    )
    URL_PATTERN = re.compile(r"https?://\S+")

    def __init__(self, auto_save_threshold: float = 0.80, product_threshold: float = 0.75):
        self.auto_save_threshold = auto_save_threshold
        self.product_threshold = product_threshold

    def classify(self, expression: str, product: ProductContext | None = None) -> IntentDecision:
        text = expression.strip()
        product = product or ProductContext()
        lowered = text.lower()

        if not text:
            return IntentDecision(
                kind=IntentKind.UNKNOWN,
                confidence=0.0,
                route=Route.IGNORE,
                missing_fields=["expression"],
                rationale="没有可判断的用户表达。",
            )

        if any(pattern in lowered for pattern in self.NEGATIVE_PATTERNS):
            return IntentDecision(
                kind=IntentKind.UNKNOWN,
                confidence=0.98,
                route=Route.IGNORE,
                signals=["negative_intent"],
                rationale="检测到否定或取消表达，不创建监控。",
            )

        explicit_hits = [
            pattern
            for pattern in self.EXPLICIT_PATTERNS
            if pattern in lowered and not (pattern == "要买" and "要不要买" in lowered)
        ]
        fuzzy_hits = [pattern for pattern in self.FUZZY_PATTERNS if pattern in lowered]
        price_match = self.PRICE_PATTERN.search(text)
        has_url = bool(product.url or self.URL_PATTERN.search(text))
        has_product = bool(product.name and has_url)
        restock = any(token in lowered for token in ("补货", "有货", "到货"))

        signals: list[str] = []
        if explicit_hits:
            signals.append(f"explicit:{explicit_hits[0]}")
        if fuzzy_hits:
            signals.append(f"fuzzy:{fuzzy_hits[0]}")
        if price_match:
            signals.append("price_threshold")
        if restock:
            signals.append("restock_request")
        if has_product:
            signals.append("resolved_product")

        target_price = float(price_match.group(1)) if price_match else None
        missing_fields = []
        if not product.name:
            missing_fields.append("product_name")
        if not product.url:
            missing_fields.append("product_url")

        has_action_directive = bool(price_match or restock)

        if explicit_hits or has_action_directive:
            confidence = 0.74
            confidence += 0.10 if has_product else 0.0
            confidence += 0.08 if has_action_directive else 0.0
            confidence -= 0.14 if fuzzy_hits else 0.0
            kind = IntentKind.EXPLICIT_PURCHASE
        elif fuzzy_hits:
            confidence = 0.58 + (0.08 if has_product else 0.0)
            kind = IntentKind.FUZZY_INTEREST
        else:
            confidence = 0.32 + (0.08 if has_product else 0.0)
            kind = IntentKind.UNKNOWN

        confidence = round(max(0.0, min(confidence, 0.99)), 2)
        can_auto_save = (
            kind is IntentKind.EXPLICIT_PURCHASE
            and confidence >= self.auto_save_threshold
            and product.confidence >= self.product_threshold
            and product.is_resolved
        )

        if kind is IntentKind.UNKNOWN:
            route = Route.IGNORE
            rationale = "未检测到购物监控意图，不创建任务也不打扰用户。"
        elif can_auto_save:
            route = Route.AUTO_SAVE
            rationale = "购买意图明确，商品实体完整且两项置信度均达到自动入库阈值。"
        else:
            route = Route.ASK_CONFIRMATION
            rationale = "意图或商品识别未达到自动入库阈值，需要用户确认。"

        return IntentDecision(
            kind=kind,
            confidence=confidence,
            route=route,
            signals=signals,
            missing_fields=missing_fields,
            rationale=rationale,
            target_price=target_price,
            notify_restock=restock,
        )
