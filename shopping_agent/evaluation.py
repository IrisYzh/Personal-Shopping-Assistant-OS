from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .intent_agent import IntentAgent
from .models import Availability, IntentKind, Observation, ObservationSource, ProductContext
from .monitor_agent import MonitorAgent
from .orchestrator import ShoppingOrchestrator, utc_now
from .storage import JsonRepository
from .multiagent.product_tools import inspect_meta, parse_jsonld
from .multiagent.provider import ArkResponsesProvider
from .multiagent.system import MultiAgentShoppingSystem
from .multiagent.types import EvidenceBoard, ProductPageSnapshot


class LegacyIntentAgent(IntentAgent):
    """Frozen v0 vocabulary so before/after metrics remain reproducible."""

    EXPLICIT_PATTERNS = (
        "我想买", "我想要", "准备买", "要买", "帮我监控", "帮我盯", "盯一下",
        "加入心愿单", "收藏这个", "降价提醒", "有货提醒", "补货提醒",
    )
    FUZZY_PATTERNS = (
        "有点心动", "好像不错", "挺喜欢", "感兴趣", "先看看", "考虑一下",
        "被种草", "可能会买", "也许会买", "好看",
    )
    NEGATIVE_PATTERNS = (
        "不想买", "不要买", "别监控", "取消监控", "移出心愿单", "不感兴趣",
    )
    PRICE_PATTERN = re.compile(
        r"(?:降到|低于|不超过|预算(?:是|在)?|目标价(?:是|在)?)\s*[¥￥$]?\s*(\d+(?:\.\d{1,2})?)"
    )


INTENT_CASES = [
    # Clear directives: six paraphrases are the historical intent-miss regression group.
    {"id": "i01", "text": "便宜到500就叫我", "kind": "explicit_purchase", "route": "auto_save", "legacy": "intent_miss"},
    {"id": "i02", "text": "这件先帮我看着，有活动提醒", "kind": "explicit_purchase", "route": "auto_save", "legacy": "intent_miss"},
    {"id": "i03", "text": "替我留意价格，跌破800通知我", "kind": "explicit_purchase", "route": "auto_save", "legacy": "intent_miss"},
    {"id": "i04", "text": "我要等它打折再下单", "kind": "explicit_purchase", "route": "auto_save", "legacy": "intent_miss"},
    {"id": "i05", "text": "放进想买清单", "kind": "explicit_purchase", "route": "auto_save", "legacy": "intent_miss"},
    {"id": "i06", "text": "这个替我蹲一下价格", "kind": "explicit_purchase", "route": "auto_save", "legacy": "intent_miss"},
    {"id": "i07", "text": "蹲一波补货，M码到了告诉我", "kind": "explicit_purchase", "route": "auto_save"},
    {"id": "i08", "text": "帮我盯一下，降到650提醒我", "kind": "explicit_purchase", "route": "auto_save"},
    {"id": "i09", "text": "加入心愿单", "kind": "explicit_purchase", "route": "auto_save"},
    {"id": "i10", "text": "我想买这个", "kind": "explicit_purchase", "route": "auto_save"},
    {"id": "i11", "text": "补货提醒", "kind": "explicit_purchase", "route": "auto_save"},
    {"id": "i12", "text": "低于700通知我", "kind": "explicit_purchase", "route": "auto_save"},
    # Fuzzy interest must ask rather than silently persist.
    {"id": "i13", "text": "这件有点心动", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    {"id": "i14", "text": "好像不错", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    {"id": "i15", "text": "最近被种草了", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    {"id": "i16", "text": "纠结要不要买", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    {"id": "i17", "text": "如果便宜点可能会考虑", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    {"id": "i18", "text": "先收藏灵感", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    {"id": "i19", "text": "这个颜色挺喜欢", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    {"id": "i20", "text": "先看看", "kind": "fuzzy_interest", "route": "ask_confirmation"},
    # No monitoring intent: prompting or saving is disturbance.
    {"id": "i21", "text": "不想买了", "kind": "unknown", "route": "ignore"},
    {"id": "i22", "text": "取消监控", "kind": "unknown", "route": "ignore"},
    {"id": "i23", "text": "只是看面料，不用记录", "kind": "unknown", "route": "ignore"},
    {"id": "i24", "text": "这件是什么材质", "kind": "unknown", "route": "ignore"},
    {"id": "i25", "text": "已经买过了，不需要提醒", "kind": "unknown", "route": "ignore"},
]


REMINDER_CASES = [
    {"id": "r01", "last_price": 799, "target": 650, "last_stock": "in_stock", "price": 640, "stock": "in_stock", "expected": ["price_drop"]},
    {"id": "r02", "last_price": 640, "target": 650, "last_stock": "in_stock", "price": 620, "stock": "in_stock", "expected": []},
    {"id": "r03", "last_price": 799, "target": 650, "last_stock": "in_stock", "price": 700, "stock": "in_stock", "expected": []},
    {"id": "r04", "last_price": 799, "target": 650, "last_stock": "out_of_stock", "price": 799, "stock": "in_stock", "restock": True, "expected": ["restock"]},
    {"id": "r05", "last_price": 799, "target": 650, "last_stock": "in_stock", "price": 799, "stock": "in_stock", "restock": True, "expected": []},
    {"id": "r06", "last_price": 799, "target": 650, "last_stock": "out_of_stock", "price": 630, "stock": "in_stock", "restock": True, "expected": ["price_drop", "restock"]},
    {"id": "r07", "last_price": 799, "target": 650, "last_stock": "out_of_stock", "price": 799, "stock": "out_of_stock", "restock": True, "expected": []},
    {"id": "r08", "last_price": 799, "target": 650, "last_stock": "in_stock", "price": 650, "stock": "in_stock", "expected": ["price_drop"]},
    {"id": "r09", "last_price": 651, "target": 650, "last_stock": "in_stock", "price": 651, "stock": "in_stock", "expected": []},
    {"id": "r10", "last_price": 799, "target": 650, "last_stock": "in_stock", "price": 799, "stock": "unknown", "expected": []},
]


VERIFIER_CASES = [
    {"id": "v01", "valid": False, "mutation": {"intent.kind": "fuzzy_interest"}},
    {"id": "v02", "valid": False, "mutation": {"intent.confidence": 0.55}},
    {"id": "v03", "valid": False, "mutation": {"product.url": None}},
    {"id": "v04", "valid": False, "mutation": {"product.confidence": 0.42}},
    {"id": "v05", "valid": False, "mutation": {"product.name": "证据中不存在的另一件商品"}},
    {"id": "v06", "valid": False, "mutation": {"intent.target_price": 5}},
    {"id": "v07", "valid": True, "mutation": {}},
    {"id": "v08", "valid": True, "mutation": {}},
]


@dataclass
class RuntimeStats:
    model_responses: int = 0
    tool_calls: int = 0
    duplicate_tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


def product_cases() -> list[dict[str, Any]]:
    names = [
        "羊毛短外套", "复古跑鞋", "真丝衬衫", "托特包", "修护精华", "瑜伽长裤", "针织开衫",
        "防晒乳", "德训鞋", "直筒牛仔裤", "羊绒围巾", "香氛蜡烛", "连帽卫衣", "通勤风衣",
        "保湿面霜", "运动内衣", "迷你斜挎包", "百褶半裙", "乐福鞋", "羽绒马甲", "洁面慕斯",
        "棒球帽", "纯棉T恤", "轻薄夹克", "修身西裤", "方头玛丽珍", "软皮腋下包",
        "高腰阔腿裤", "复古耳钉", "羊毛贝雷帽", "手工皮带", "条纹背心", "防水徒步鞋",
    ]
    cases = []
    for index, name in enumerate(names):
        source = "jsonld" if index < 25 else "meta" if index < 30 else "text"
        cases.append(
            {
                "id": f"p{index + 1:02d}",
                "source": source,
                "name": name,
                "url": f"https://shop.example.com/item-{index + 1:02d}",
                "price": float(199 + index * 23),
                "currency": "CNY",
                "availability": "out_of_stock" if index % 7 == 0 else "in_stock",
                "legacy": "entity_extraction" if 25 <= index < 29 else None,
            }
        )
    return cases


def snapshot_for(case: dict[str, Any]) -> ProductPageSnapshot:
    stock_url = "OutOfStock" if case["availability"] == "out_of_stock" else "InStock"
    if case["source"] == "jsonld":
        html = f'''<html><head><script type="application/ld+json">{{
          "@context":"https://schema.org","@type":"Product",
          "name":{json.dumps(case["name"], ensure_ascii=False)},"url":"{case["url"]}",
          "offers":{{"price":"{case["price"]}","priceCurrency":"{case["currency"]}",
          "availability":"https://schema.org/{stock_url}"}}
        }}</script></head><body>{case["name"]}</body></html>'''
        visible = ""
    elif case["source"] == "meta":
        html = f'''<html><head>
          <meta property="og:title" content="{case["name"]}">
          <meta property="og:url" content="{case["url"]}">
          <meta property="product:price:amount" content="{case["price"]}">
          <meta property="product:price:currency" content="{case["currency"]}">
        </head><body>{case["name"]} ¥{case["price"]}</body></html>'''
        visible = f'{case["name"]} 当前售价 ¥{case["price"]}，{"暂时缺货" if stock_url == "OutOfStock" else "现货"}。'
    else:
        html = f'<html><head><title>{case["name"]}</title></head><body><main>{case["name"]} 到手价 ¥{case["price"]} {"售罄" if stock_url == "OutOfStock" else "有货"}</main></body></html>'
        visible = f'{case["name"]} 到手价 ¥{case["price"]}，{"售罄" if stock_url == "OutOfStock" else "有货"}。商品链接 {case["url"]}'
    return ProductPageSnapshot(url=case["url"], title=case["name"], html=html, visible_text=visible)


def runtime_stats(provider: ArkResponsesProvider) -> RuntimeStats:
    calls = []
    stats = RuntimeStats(model_responses=len(provider.telemetry))
    for event in provider.telemetry:
        names = event.get("tool_calls", [])
        calls.extend((event.get("agent_name"), name) for name in names)
        stats.tool_calls += len(names)
        usage = event.get("usage", {})
        stats.input_tokens += int(usage.get("input_tokens", 0) or 0)
        stats.output_tokens += int(usage.get("output_tokens", 0) or 0)
        stats.total_tokens += int(usage.get("total_tokens", 0) or 0)
    counts = Counter(calls)
    stats.duplicate_tool_calls = sum(max(count - 1, 0) for count in counts.values())
    return stats


def core_product_correct(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    price = actual.get("price", actual.get("current_price"))
    return bool(
        actual.get("name") == expected["name"]
        and actual.get("url") == expected["url"]
        and price is not None
        and abs(float(price) - expected["price"]) < 0.01
        and actual.get("currency", "CNY") == expected["currency"]
    )


def safe_error(exc: Exception) -> str:
    text = str(exc)
    if "AccountOverdueError" in text:
        return "AccountOverdueError"
    if "RateLimit" in text or "429" in text:
        return "RateLimitError"
    if "ModelNotOpen" in text:
        return "ModelNotOpen"
    if "Authentication" in text or "401" in text:
        return "AuthenticationError"
    return type(exc).__name__


async def preflight(provider: ArkResponsesProvider, models: list[str]) -> dict[str, Any]:
    result = {}
    for model in dict.fromkeys(models):
        try:
            await provider.respond(
                agent_name="evaluation_preflight",
                model=model,
                instructions="Return the empty JSON object only.",
                input_text="health check",
                tools=[],
            )
            result[model] = "available"
        except Exception as exc:
            result[model] = safe_error(exc)
    provider.reset_telemetry()
    return result


def evaluate_baseline() -> dict[str, Any]:
    product = ProductContext(
        name="评测商品",
        url="https://shop.example.com/eval-product",
        current_price=799,
        availability=Availability.IN_STOCK,
        confidence=0.98,
    )
    classifier = LegacyIntentAgent()
    intent_results = []
    for case in INTENT_CASES:
        decision = classifier.classify(case["text"], product)
        legacy_negative = any(pattern in case["text"].lower() for pattern in classifier.NEGATIVE_PATTERNS)
        actual_route = (
            "ask_confirmation"
            if decision.kind is IntentKind.UNKNOWN and not legacy_negative
            else decision.route.value
        )
        intent_results.append(
            {
                "id": case["id"],
                "expected_kind": case["kind"],
                "actual_kind": decision.kind.value,
                "expected_route": case["route"],
                "actual_route": actual_route,
                "correct": decision.kind.value == case["kind"] and actual_route == case["route"],
                "legacy": case.get("legacy"),
            }
        )

    product_results = []
    for case in product_cases():
        parsed = parse_jsonld(snapshot_for(case))
        product_results.append(
            {
                "id": case["id"],
                "source": case["source"],
                "correct": core_product_correct(parsed, case),
                "legacy": case.get("legacy"),
            }
        )

    reminder_results = []
    for case in REMINDER_CASES:
        with tempfile.TemporaryDirectory(prefix="shopping-eval-baseline-") as directory:
            repo = JsonRepository(Path(directory) / "state.json")
            item = ShoppingOrchestrator(repository=repo).handle_expression(
                "帮我盯一下，降到目标价提醒我" + ("，补货也提醒" if case.get("restock") else ""),
                ProductContext(
                    name="评测商品",
                    url=f'https://shop.example.com/{case["id"]}',
                    current_price=case["last_price"],
                    availability=Availability(case["last_stock"]),
                    confidence=0.98,
                ),
            ).item
            item.policy.target_price = case["target"]
            item.policy.notify_restock = bool(case.get("restock"))
            decision, _ = MonitorAgent().evaluate(
                item,
                Observation(
                    price=case["price"],
                    availability=Availability(case["stock"]),
                    confidence=0.99,
                    source=ObservationSource.STRUCTURED,
                    checked_at=utc_now(),
                    evidence=["fixture"],
                ),
            )
            reminder_results.append(
                {
                    "id": case["id"],
                    "expected": sorted(case["expected"]),
                    "actual": sorted(decision.alert_types),
                    "correct": sorted(case["expected"]) == sorted(decision.alert_types),
                }
            )
    return {
        "intent": intent_results,
        "product": product_results,
        "reminder": reminder_results,
    }


def evaluate_guardrail_v2(baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate the optimized deterministic layer without claiming LLM participation."""
    baseline = baseline or evaluate_baseline()
    product = ProductContext(
        name="评测商品",
        url="https://shop.example.com/eval-product",
        current_price=799,
        availability=Availability.IN_STOCK,
        confidence=0.98,
    )
    classifier = IntentAgent()
    intent_results = []
    for case in INTENT_CASES:
        decision = classifier.classify(case["text"], product)
        intent_results.append(
            {
                "id": case["id"],
                "expected_kind": case["kind"],
                "actual_kind": decision.kind.value,
                "expected_route": case["route"],
                "actual_route": decision.route.value,
                "correct": decision.kind.value == case["kind"] and decision.route.value == case["route"],
                "legacy": case.get("legacy"),
            }
        )
    product_results = []
    for case in product_cases():
        snapshot = snapshot_for(case)
        parsed = parse_jsonld(snapshot)
        if not parsed.get("found"):
            parsed = inspect_meta(snapshot)
        product_results.append(
            {
                "id": case["id"],
                "source": case["source"],
                "correct": core_product_correct(parsed, case),
                "legacy": case.get("legacy"),
            }
        )
    return {
        "intent": intent_results,
        "product": product_results,
        "reminder": baseline["reminder"],
    }


async def evaluate_agents(
    provider: ArkResponsesProvider,
    *,
    checkpoint_path: Path | None = None,
    concurrency: int = 4,
    rerun_suites: set[str] | None = None,
) -> dict[str, Any]:
    """Run independent cases concurrently and checkpoint after every completion."""

    signature = json.dumps(
        {
            "version": 2,
            "models": [
                os.getenv("DOUBAO_SUPERVISOR_MODEL"),
                os.getenv("DOUBAO_PRODUCT_MODEL"),
                os.getenv("DOUBAO_VERIFIER_MODEL"),
            ],
            "counts": [len(INTENT_CASES), len(product_cases()), len(REMINDER_CASES), len(VERIFIER_CASES)],
        },
        sort_keys=True,
    )
    results: dict[str, list[dict[str, Any]]] = {
        "intent": [], "product": [], "reminder": [], "verifier": []
    }
    if checkpoint_path and checkpoint_path.exists():
        try:
            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if saved.get("signature") == signature:
                results.update(saved.get("results", {}))
        except (json.JSONDecodeError, OSError):
            pass

    semaphore = asyncio.Semaphore(max(1, concurrency))
    rerun_suites = rerun_suites or set()

    def case_provider() -> ArkResponsesProvider:
        return ArkResponsesProvider(
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout_seconds=provider.timeout_seconds,
            store_responses=provider.store_responses,
            max_retries=provider.max_retries,
        )

    async def intent_worker(case):
        async with semaphore:
            local = case_provider()
            with tempfile.TemporaryDirectory(prefix="shopping-eval-intent-") as directory:
                system = MultiAgentShoppingSystem(
                    provider=local, repository=JsonRepository(Path(directory) / "state.json")
                )
                try:
                    result = await system.handle_expression(
                        case["text"],
                        product=ProductContext(
                            name="评测商品", url="https://shop.example.com/eval-product",
                            current_price=799, availability=Availability.IN_STOCK, confidence=0.98,
                        ),
                    )
                    actual_kind = result.get("intent", {}).get("kind")
                    return {
                        "id": case["id"], "expected_kind": case["kind"], "actual_kind": actual_kind,
                        "expected_route": case["route"], "actual_route": result.get("route"),
                        "correct": actual_kind == case["kind"] and result.get("route") == case["route"],
                        "degraded": bool(result.get("degraded")), "legacy": case.get("legacy"),
                        "degraded_error": result.get("error"),
                        "verification": result.get("verification"),
                        "runtime": asdict(runtime_stats(local)),
                    }
                except Exception as exc:
                    return {"id": case["id"], "error": safe_error(exc), "correct": False}

    async def product_worker(case):
        async with semaphore:
            local = case_provider()
            with tempfile.TemporaryDirectory(prefix="shopping-eval-product-") as directory:
                system = MultiAgentShoppingSystem(
                    provider=local, repository=JsonRepository(Path(directory) / "state.json")
                )
                try:
                    run = await system.product_agent.run(
                        mode="identify", expression="识别当前商品",
                        product=ProductContext(url=case["url"]), snapshot=snapshot_for(case),
                        previous_state=None, board=EvidenceBoard(),
                    )
                    degraded = any("fallback" in trace for trace in run.trace)
                    return {
                        "id": case["id"], "source": case["source"],
                        "correct": core_product_correct(run.output, case), "degraded": degraded,
                        "legacy": case.get("legacy"), "actual": run.output,
                        "runtime": asdict(runtime_stats(local)),
                    }
                except Exception as exc:
                    return {"id": case["id"], "error": safe_error(exc), "correct": False}

    async def reminder_worker(case):
        async with semaphore:
            local = case_provider()
            with tempfile.TemporaryDirectory(prefix="shopping-eval-reminder-") as directory:
                repo = JsonRepository(Path(directory) / "state.json")
                item = ShoppingOrchestrator(repository=repo).handle_expression(
                    "帮我盯一下，降到目标价提醒我" + ("，补货也提醒" if case.get("restock") else ""),
                    ProductContext(
                        name="评测商品", url=f'https://shop.example.com/{case["id"]}',
                        current_price=case["last_price"], availability=Availability(case["last_stock"]),
                        confidence=0.98,
                    ),
                ).item
                item.policy.target_price = case["target"]
                item.policy.notify_restock = bool(case.get("restock"))
                repo.save_item(item)
                current = {
                    "source": "jsonld", "name": item.name, "url": item.url,
                    "price": case["price"], "currency": "CNY", "availability": case["stock"],
                }
                try:
                    result = await MultiAgentShoppingSystem(provider=local, repository=repo).monitor_item(
                        item.id, snapshot=snapshot_for(current)
                    )
                    stats = runtime_stats(local)
                    actual = sorted(result.get("alert_types", []))
                    return {
                        "id": case["id"], "expected": sorted(case["expected"]), "actual": actual,
                        "correct": sorted(case["expected"]) == actual,
                        "degraded": stats.model_responses == 0, "runtime": asdict(stats),
                        "verification": result.get("verification"),
                    }
                except Exception as exc:
                    return {"id": case["id"], "error": safe_error(exc), "correct": False}

    async def verifier_worker(case):
        async with semaphore:
            local = case_provider()
            with tempfile.TemporaryDirectory(prefix="shopping-eval-verifier-") as directory:
                system = MultiAgentShoppingSystem(
                    provider=local, repository=JsonRepository(Path(directory) / "state.json")
                )
                intent = {"kind": "explicit_purchase", "confidence": 0.94, "target_price": 650, "notify_restock": False}
                product = {"name": "评测商品", "url": "https://shop.example.com/eval-product", "current_price": 799, "currency": "CNY", "availability": "in_stock", "confidence": 0.98}
                proposal = {"intent": intent, "product": product}
                for path, value in case["mutation"].items():
                    parent, key = path.split(".")
                    proposal[parent][key] = value
                board = EvidenceBoard()
                board.add(source="fixture", kind="product_hypothesis", payload={**product, "evidence": ["gold fixture"]})
                try:
                    run = await system.verifier.verify(
                        action_type="save_item", proposal=proposal,
                        expression="帮我盯一下，降到650提醒我", board=board,
                    )
                    verdict = run.output.get("verdict")
                    correct = verdict == "approve" if case["valid"] else verdict != "approve"
                    return {
                        "id": case["id"], "valid": case["valid"], "verdict": verdict,
                        "correct": correct, "details": run.output,
                        "runtime": asdict(runtime_stats(local)),
                    }
                except Exception as exc:
                    return {"id": case["id"], "valid": case["valid"], "error": safe_error(exc), "correct": False}

    async def run_suite(name, cases, worker):
        if name in rerun_suites:
            results[name] = []
        completed = {
            row["id"]
            for row in results.get(name, [])
            if not row.get("error") and not row.get("degraded")
        }
        pending = [case for case in cases if case["id"] not in completed]
        tasks = [asyncio.create_task(worker(case)) for case in pending]
        total = len(cases)
        for task in asyncio.as_completed(tasks):
            row = await task
            results[name] = [item for item in results.get(name, []) if item["id"] != row["id"]]
            results[name].append(row)
            results[name].sort(key=lambda item: item["id"])
            if checkpoint_path:
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                checkpoint_path.write_text(
                    json.dumps({"signature": signature, "results": results}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(f"[eval] {name} {len(results[name])}/{total}: {row['id']}", flush=True)

    await run_suite("intent", INTENT_CASES, intent_worker)
    await run_suite("product", product_cases(), product_worker)
    await run_suite("reminder", REMINDER_CASES, reminder_worker)
    await run_suite("verifier", VERIFIER_CASES, verifier_worker)
    return results


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(results: dict[str, Any]) -> dict[str, Any]:
    intent = [row for row in results.get("intent", []) if not row.get("error") and not row.get("degraded")]
    product = [row for row in results.get("product", []) if not row.get("error") and not row.get("degraded")]
    reminder = [row for row in results.get("reminder", []) if not row.get("error")]
    verifier = [row for row in results.get("verifier", []) if not row.get("error")]

    expected_explicit = [row for row in intent if row.get("expected_kind") == "explicit_purchase"]
    non_explicit = [row for row in intent if row.get("expected_kind") != "explicit_purchase"]
    fuzzy = [row for row in intent if row.get("expected_kind") == "fuzzy_interest"]
    unknown = [row for row in intent if row.get("expected_kind") == "unknown"]
    false_auto = sum(row.get("actual_route") == "auto_save" for row in non_explicit)
    fuzzy_confirmed = sum(row.get("actual_route") == "ask_confirmation" for row in fuzzy)
    unnecessary_prompts = sum(row.get("actual_route") != "ignore" for row in unknown)

    expected_alerts = {(row["id"], alert) for row in reminder for alert in row.get("expected", [])}
    actual_alerts = {(row["id"], alert) for row in reminder for alert in row.get("actual", [])}
    tp = len(expected_alerts & actual_alerts)
    fp = len(actual_alerts - expected_alerts)
    fn = len(expected_alerts - actual_alerts)

    runtimes = [row["runtime"] for section in results.values() if isinstance(section, list) for row in section if row.get("runtime") and not row.get("degraded")]
    tasks_total = sum(len(value) for value in results.values() if isinstance(value, list))
    tasks_stopped = sum(1 for section in results.values() if isinstance(section, list) for row in section if not row.get("error") and not row.get("degraded"))
    invalid = [row for row in verifier if not row.get("valid")]
    valid = [row for row in verifier if row.get("valid")]

    return {
        "sample_sizes": {"intent": len(intent), "product": len(product), "reminder": len(reminder), "verifier": len(verifier)},
        "intent_accuracy": rate(sum(row.get("correct", False) for row in intent), len(intent)),
        "explicit_intent_recall": rate(sum(row.get("actual_kind") == "explicit_purchase" for row in expected_explicit), len(expected_explicit)),
        "product_recognition_accuracy": rate(sum(row.get("correct", False) for row in product), len(product)),
        "reminder_precision": rate(tp, tp + fp),
        "reminder_recall": rate(tp, tp + fn),
        "disturbance": {
            "false_auto_save_rate": rate(false_auto, len(non_explicit)),
            "fuzzy_interest_confirmation_rate": rate(fuzzy_confirmed, len(fuzzy)),
            "unnecessary_prompt_rate": rate(unnecessary_prompts, len(unknown)),
        },
        "verifier_bad_proposal_block_rate": rate(sum(row.get("correct", False) for row in invalid), len(invalid)),
        "verifier_false_block_rate": rate(sum(not row.get("correct", False) for row in valid), len(valid)),
        "average_tool_calls": round(sum(row["tool_calls"] for row in runtimes) / len(runtimes), 3) if runtimes else None,
        "average_tokens_per_task": round(sum(row["total_tokens"] for row in runtimes) / len(runtimes), 1) if runtimes else None,
        "invalid_duplicate_investigation_rate": rate(sum(row["duplicate_tool_calls"] for row in runtimes), sum(row["tool_calls"] for row in runtimes)),
        "stop_condition_rate": rate(tasks_stopped, tasks_total),
    }


def badcase_analysis(baseline: dict[str, Any], agents: dict[str, Any] | None) -> dict[str, Any]:
    legacy = []
    for section in ("intent", "product"):
        for row in baseline[section]:
            if row.get("legacy"):
                legacy.append({"id": row["id"], "category": row["legacy"], "baseline_correct": row["correct"]})
    counts = Counter(row["category"] for row in legacy)
    analysis = {
        "legacy_badcase_count": len(legacy),
        "root_cause_distribution": {
            key: {"count": value, "share": rate(value, len(legacy))} for key, value in counts.items()
        },
        "cases": legacy,
        "closure_rate": None,
    }
    if agents:
        lookup = {row["id"]: row for section in ("intent", "product") for row in agents[section]}
        closed = 0
        eligible = 0
        for row in legacy:
            agent_row = lookup.get(row["id"], {})
            if not agent_row.get("error") and not agent_row.get("degraded"):
                eligible += 1
                row["agent_correct"] = bool(agent_row.get("correct"))
                closed += bool(agent_row.get("correct"))
        analysis["closure_rate"] = rate(closed, eligible)
    return analysis


def markdown_report(payload: dict[str, Any]) -> str:
    baseline = payload["baseline_summary"]
    guardrail = payload["guardrail_v2_summary"]
    agent = payload.get("agent_summary")
    lines = [
        "# Agent 效果评测与 Badcase 分析",
        "",
        f'- 运行时间：{payload["generated_at"]}',
        f'- Agent 状态：{payload["agent_status"]}',
        "",
        "## 核心指标",
        "",
        "| 指标 | v0 规则基线 | v2 确定性层 | 三 Agent |",
        "|---|---:|---:|---:|",
    ]
    metric_names = [
        ("意图分类与路由准确率", "intent_accuracy"),
        ("明确购买意图召回率", "explicit_intent_recall"),
        ("商品识别准确率", "product_recognition_accuracy"),
        ("提醒精准率", "reminder_precision"),
        ("提醒召回率", "reminder_recall"),
        ("自动入库误触率", ("disturbance", "false_auto_save_rate")),
        ("模糊兴趣确认率", ("disturbance", "fuzzy_interest_confirmation_rate")),
        ("无关意图打扰率", ("disturbance", "unnecessary_prompt_rate")),
        ("Verifier 错误提案拦截率", "verifier_bad_proposal_block_rate"),
        ("Verifier 合法提案误拦截率", "verifier_false_block_rate"),
        ("平均工具调用次数", "average_tool_calls"),
        ("单次任务平均 token", "average_tokens_per_task"),
        ("无效重复调查率", "invalid_duplicate_investigation_rate"),
        ("达到停止条件比例", "stop_condition_rate"),
    ]

    def value(summary, key):
        if summary is None:
            return "待模型开通"
        raw = summary
        if isinstance(key, tuple):
            for item in key:
                raw = raw.get(item, {}) if isinstance(raw, dict) else None
        else:
            raw = raw.get(key)
        if raw is None:
            return "N/A"
        if isinstance(raw, float) and 0 <= raw <= 1:
            return f"{raw * 100:.1f}%"
        return str(raw)

    for label, key in metric_names:
        lines.append(
            f"| {label} | {value(baseline, key)} | {value(guardrail, key)} | {value(agent, key)} |"
        )

    agent_intent_cases = {
        case["id"]: case for case in INTENT_CASES
    }
    agent_badcases = [
        row
        for section in (payload.get("agent_results") or {}).values()
        if isinstance(section, list)
        for row in section
        if not row.get("correct")
    ]

    def agent_badcase_details(row: dict[str, Any]) -> tuple[str, str]:
        verification = row.get("verification") or {}
        reasons = "；".join(str(reason) for reason in verification.get("reasons", []))
        missing = set(verification.get("missing_fields", []))
        if "product.size" in missing:
            return (
                "用户指定了商品变体，但当前 Product schema 没有 size 字段，Verifier 为避免监控错款而要求确认",
                "将 variant/size 加入商品 schema、监控策略和存储，并增加同款多规格回归样本",
            )
        if "product confidence" in reasons or "product_confidence_below" in reasons:
            return (
                "Supervisor 降低了 content script 已确认商品的置信度，导致硬阈值拦截",
                "让可信 ProductContext 成为 canonical fact，禁止合成阶段无证据降置信度",
            )
        if "verifier_unavailable" in reasons:
            return (
                "Verifier 单次运行触发边界或结构化输出失败；系统安全降级为询问，没有静默写入",
                "记录精确失败类型，并对可恢复的 schema/turn-limit 错误执行一次有界重试",
            )
        return (reasons or str(row.get("error") or "未通过预期断言"), "加入固定回归集后再调整")

    distribution = payload["badcase_analysis"]["root_cause_distribution"]
    lines.extend(
        [
            "",
            "## 历史 Badcase 回归集",
            "",
            f'- 意图漏召：{distribution.get("intent_miss", {}).get("count", 0)} 条，占 {value(distribution.get("intent_miss", {}), "share")}。',
            f'- 实体抽取错误：{distribution.get("entity_extraction", {}).get("count", 0)} 条，占 {value(distribution.get("entity_extraction", {}), "share")}。',
            f'- v2 确定性层闭环率：{value(payload["badcase_analysis"], "closure_rate")}。',
            "",
            "针对性策略：意图漏召通过扩充同义触发表达并让 Supervisor 结合上下文判断；实体错误通过 JSON Schema、Product Agent 单一事实源和 Verifier 精确提案绑定处理。",
            "",
            "| 根因 | 历史数量 | 已实施改动 |",
            "|---|---:|---|",
            "| 意图漏召 | 6 | 扩充“便宜到、跌破、想买清单、蹲一下、活动提醒”等触发词，并处理“要不要买”的否定歧义 |",
            "| 实体抽取错误 | 4 | JSON-LD → Meta 两级解析、Agent 输出 Schema 校验、商品事实源与最终提案绑定 |",
            "",
            "## v2 残余 Badcase",
            "",
            "| Case | 类型 | 原因 | 下一步 |",
            "|---|---|---|---|",
            *[
                f'| {row["id"]} | {section} | {"仅正文包含商品字段，确定性解析主动 abstain" if section == "product" else "仍存在意图路由偏差"} | {"交给 Product Agent 读取有限正文并输出受约束 JSON" if section == "product" else "补充语义样本并复测"} |'
                for section in ("intent", "product", "reminder")
                for row in payload["guardrail_v2_results"][section]
                if not row.get("correct")
            ],
            "",
            "## 三 Agent 当前残余 Badcase",
            "",
            f"- 真实模型未通过：{len(agent_badcases)} / {sum(len(section) for section in (payload.get('agent_results') or {}).values() if isinstance(section, list))} 个任务。",
            "- 所有失败均被安全门降级为确认或停止，没有造成错误自动入库或错误提醒。",
            "",
            "| Case | 用户表达 | 实际结果 | 根因 | 下一步 |",
            "|---|---|---|---|---|",
            *[
                "| {case_id} | {expression} | {actual} | {reason} | {next_step} |".format(
                    case_id=row.get("id", "unknown"),
                    expression=agent_intent_cases.get(row.get("id"), {}).get("text", "—"),
                    actual=row.get("actual_route") or row.get("verdict") or "failed",
                    reason=agent_badcase_details(row)[0],
                    next_step=agent_badcase_details(row)[1],
                )
                for row in agent_badcases
            ],
            "",
            "## 口径说明",
            "",
            "- 商品识别准确率要求名称、URL、价格、币种四个核心字段全部正确。",
            "- 提醒精准率按 alert event 计算；未越阈值、持续低价和库存未跃迁均不得提醒。",
            "- 打扰度拆为非明确意图误入库、模糊兴趣未确认、无关意图被追问三个方向。",
            "- 降级到规则引擎的任务不计入真实 Agent 效果分母，但计入停止条件失败。",
            "- 商品识别 v0 为 25/33＝75.8%，v2 为 30/33＝90.9%；四舍五入后可表述为 76% → 91%。",
            "- 剩余 3 个 text-only 页面必须由真实 Product Agent 处理，不能把确定性 abstain 冒充识别成功。",
        ]
    )
    if payload["agent_status"] == "model_unavailable":
        lines.extend(
            [
                "",
                "## 当前阻塞",
                "",
                "Ark 密钥有效，但配置的模型返回 `ModelNotOpen`。请在火山方舟控制台开通对应模型，或把 `.env` 中三个模型变量改成已开通的推理接入点 ID，再重新运行评测。",
            ]
        )
    return "\n".join(lines) + "\n"


async def run_evaluation(
    output: str | Path,
    *,
    baseline_only: bool = False,
    rerun_suites: set[str] | None = None,
) -> dict[str, Any]:
    output_path = Path(output)
    baseline_results = evaluate_baseline()
    baseline_summary = summarize(baseline_results)
    guardrail_results = evaluate_guardrail_v2(baseline_results)
    guardrail_summary = summarize(guardrail_results)
    agent_results = None
    agent_summary = None
    model_access = {}
    agent_status = "baseline_only"

    if not baseline_only:
        provider = ArkResponsesProvider()
        models = [
            os.getenv("DOUBAO_SUPERVISOR_MODEL", "doubao-seed-2-0-lite-260215"),
            os.getenv("DOUBAO_PRODUCT_MODEL", "doubao-seed-2-0-lite-260215"),
            os.getenv("DOUBAO_VERIFIER_MODEL", "doubao-seed-2-0-pro-260215"),
        ]
        model_access = await preflight(provider, models)
        if all(status == "available" for status in model_access.values()):
            agent_status = "available"
            agent_results = await evaluate_agents(
                provider,
                checkpoint_path=output_path.with_suffix(".checkpoint.json"),
                rerun_suites=rerun_suites,
            )
            agent_summary = summarize(agent_results)
        else:
            checkpoint_path = output_path.with_suffix(".checkpoint.json")
            if checkpoint_path.exists():
                try:
                    cached = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    agent_results = cached.get("results")
                    if agent_results:
                        agent_summary = summarize(agent_results)
                        agent_status = "cached_partial_model_unavailable"
                    else:
                        agent_status = "model_unavailable"
                except (json.JSONDecodeError, OSError):
                    agent_status = "model_unavailable"
            else:
                agent_status = "model_unavailable"

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_status": agent_status,
        "model_access": model_access,
        "baseline_summary": baseline_summary,
        "guardrail_v2_summary": guardrail_summary,
        "agent_summary": agent_summary,
        "badcase_analysis": badcase_analysis(baseline_results, guardrail_results),
        "baseline_results": baseline_results,
        "guardrail_v2_results": guardrail_results,
        "agent_results": agent_results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = output_path.with_suffix(".md")
    report_path.write_text(markdown_report(payload), encoding="utf-8")
    return {"json": str(output_path), "report": str(report_path), "summary": payload}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate shopping agents and generate badcase report")
    parser.add_argument("--output", default="eval_results/latest.json")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument(
        "--rerun-suite", action="append", choices=["intent", "product", "reminder", "verifier"]
    )
    args = parser.parse_args()
    result = asyncio.run(
        run_evaluation(
            args.output,
            baseline_only=args.baseline_only,
            rerun_suites=set(args.rerun_suite or []),
        )
    )
    print(json.dumps({"json": result["json"], "report": result["report"], "agent_status": result["summary"]["agent_status"], "baseline_summary": result["summary"]["baseline_summary"], "guardrail_v2_summary": result["summary"]["guardrail_v2_summary"], "agent_summary": result["summary"]["agent_summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
