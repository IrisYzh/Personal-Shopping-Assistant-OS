from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from .models import Availability, Observation, ObservationSource, ProductContext, to_primitive
from .orchestrator import ShoppingOrchestrator, utc_now
from .storage import JsonRepository


def emit(value) -> None:
    print(json.dumps(to_primitive(value), ensure_ascii=False, indent=2))


def product_from_args(args) -> ProductContext:
    return ProductContext(
        name=args.product,
        url=args.url,
        current_price=args.price,
        currency=args.currency,
        availability=Availability(args.stock),
        confidence=args.product_confidence if args.product and args.url else 0.0,
    )


def observation_from_args(args, prefix: str = "") -> Observation | None:
    price = getattr(args, f"{prefix}price")
    stock = getattr(args, f"{prefix}stock")
    if prefix and price is None and stock is None:
        return None
    return Observation(
        price=price,
        availability=Availability(stock or "unknown"),
        confidence=getattr(args, f"{prefix}confidence"),
        source=ObservationSource(getattr(args, f"{prefix}source")),
        checked_at=utc_now(),
        evidence=getattr(args, f"{prefix}evidence") or [],
        error=getattr(args, f"{prefix}error"),
    )


def run_demo() -> None:
    with tempfile.TemporaryDirectory(prefix="shopping-agent-demo-") as directory:
        repository = JsonRepository(Path(directory) / "state.json")
        orchestrator = ShoppingOrchestrator(repository=repository)

        print("\n1) 明确购买意图：满足阈值，自动入库")
        explicit = orchestrator.handle_expression(
            "帮我盯一下这件外套，降到 650 提醒我",
            ProductContext(
                name="羊毛混纺短外套",
                url="https://shop.example.com/wool-jacket",
                current_price=799,
                availability=Availability.IN_STOCK,
                confidence=0.94,
            ),
        )
        emit(explicit)

        print("\n2) 模糊兴趣：不静默入库，先反问")
        fuzzy = orchestrator.handle_expression(
            "这双鞋有点心动，先看看",
            ProductContext(
                name="复古跑鞋",
                url="https://shop.example.com/retro-runner",
                current_price=629,
                availability=Availability.IN_STOCK,
                confidence=0.91,
            ),
        )
        emit(fuzzy)

        print("\n3) 用户显式确认后才入库")
        confirmed = orchestrator.confirm(fuzzy.pending.id, True, target_price=550)
        emit(confirmed)

        print("\n4) 模型低置信度：退回 JSON-LD 确定性结果，并触发越阈值提醒")
        monitor = orchestrator.check_item(
            explicit.item.id,
            Observation(
                price=639,
                availability=Availability.IN_STOCK,
                confidence=0.48,
                source=ObservationSource.MODEL,
                checked_at=utc_now(),
                evidence=[],
            ),
            deterministic_fallback=Observation(
                price=639,
                availability=Availability.IN_STOCK,
                confidence=0.99,
                source=ObservationSource.STRUCTURED,
                checked_at=utc_now(),
                evidence=["schema.org/Product.offers.price"],
            ),
        )
        emit(monitor)

        state = repository.snapshot()
        print("\n5) 最终状态摘要")
        emit(
            {
                "items": len(state["items"]),
                "pending_confirmations": len(state["pending_confirmations"]),
                "alerts": state["alerts"],
                "audit_events": len(state["audit_log"]),
            }
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal Shopping Assistant local multi-agent MVP")
    parser.add_argument("--state", default="data/agent_state.json", help="Local JSON repository path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    say = subparsers.add_parser("say", help="Classify and route a shopping expression")
    say.add_argument("expression")
    say.add_argument("--product")
    say.add_argument("--url")
    say.add_argument("--price", type=float)
    say.add_argument("--currency", default="CNY")
    say.add_argument("--stock", choices=[item.value for item in Availability], default="unknown")
    say.add_argument("--product-confidence", type=float, default=0.9)

    confirm = subparsers.add_parser("confirm", help="Resolve a pending confirmation")
    confirm.add_argument("pending_id")
    confirm.add_argument("--approve", action="store_true")
    confirm.add_argument("--name")
    confirm.add_argument("--url")
    confirm.add_argument("--price", type=float)
    confirm.add_argument("--target-price", type=float)
    confirm.add_argument("--restock", action="store_true")

    check = subparsers.add_parser("check", help="Evaluate a new price/stock observation")
    check.add_argument("item_id")
    check.add_argument("--price", type=float)
    check.add_argument("--stock", choices=[item.value for item in Availability], default="unknown")
    check.add_argument("--confidence", type=float, default=0.9)
    check.add_argument("--source", choices=[item.value for item in ObservationSource], default="structured")
    check.add_argument("--evidence", action="append")
    check.add_argument("--error")
    check.add_argument("--fallback-price", type=float)
    check.add_argument("--fallback-stock", choices=[item.value for item in Availability])
    check.add_argument("--fallback-confidence", type=float, default=0.99)
    check.add_argument(
        "--fallback-source", choices=[item.value for item in ObservationSource], default="structured"
    )
    check.add_argument("--fallback-evidence", action="append")
    check.add_argument("--fallback-error")

    subparsers.add_parser("list", help="List locally persisted wishlist items")
    subparsers.add_parser("state", help="Print the complete local state and audit log")
    subparsers.add_parser("demo", help="Run four deterministic portfolio scenarios")
    subparsers.add_parser("multi-demo", help="Run the three-agent tool/delegation demo without an API key")

    evaluate = subparsers.add_parser("eval", help="Run effect evaluation and badcase analysis")
    evaluate.add_argument("--output", default="eval_results/latest.json")
    evaluate.add_argument("--baseline-only", action="store_true")
    evaluate.add_argument(
        "--rerun-suite",
        action="append",
        choices=["intent", "product", "reminder", "verifier"],
    )

    agent_say = subparsers.add_parser("agent-say", help="Run the real three-agent system with Doubao")
    agent_say.add_argument("expression")
    agent_say.add_argument("--product")
    agent_say.add_argument("--url")
    agent_say.add_argument("--price", type=float)
    agent_say.add_argument("--currency", default="CNY")
    agent_say.add_argument("--stock", choices=[item.value for item in Availability], default="unknown")
    agent_say.add_argument("--product-confidence", type=float, default=0.0)
    agent_say.add_argument("--html-file", help="Saved product-page HTML for Product Intelligence tools")

    agent_check = subparsers.add_parser(
        "agent-check", help="Run Product Intelligence and Verifier for a monitored item"
    )
    agent_check.add_argument("item_id")
    agent_check.add_argument("--url", required=True)
    agent_check.add_argument("--html-file", required=True, help="Saved current product-page HTML")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "demo":
        run_demo()
        return
    if args.command == "multi-demo":
        from .multiagent.demo import run_scripted_demo

        emit(asyncio.run(run_scripted_demo()))
        return
    if args.command == "eval":
        from .evaluation import run_evaluation

        result = asyncio.run(
            run_evaluation(
                args.output,
                baseline_only=args.baseline_only,
                rerun_suites=set(args.rerun_suite or []),
            )
        )
        emit(
            {
                "json": result["json"],
                "report": result["report"],
                "agent_status": result["summary"]["agent_status"],
                "baseline_summary": result["summary"]["baseline_summary"],
                "guardrail_v2_summary": result["summary"]["guardrail_v2_summary"],
                "agent_summary": result["summary"]["agent_summary"],
            }
        )
        return
    if args.command in ("agent-say", "agent-check"):
        from .multiagent.system import MultiAgentShoppingSystem
        from .multiagent.types import ProductPageSnapshot

        repository = JsonRepository(args.state)
        system = MultiAgentShoppingSystem(repository=repository)
        html = Path(args.html_file).read_text(encoding="utf-8") if args.html_file else ""
        snapshot = ProductPageSnapshot(
            url=args.url or "",
            title=getattr(args, "product", None) or "",
            html=html,
        )
        if args.command == "agent-check":
            emit(asyncio.run(system.monitor_item(args.item_id, snapshot=snapshot)))
            return
        product = product_from_args(args)
        result = asyncio.run(
            system.handle_expression(
                args.expression,
                product=product,
                snapshot=snapshot,
            )
        )
        emit(result)
        return

    repository = JsonRepository(args.state)
    orchestrator = ShoppingOrchestrator(repository=repository)

    if args.command == "say":
        emit(orchestrator.handle_expression(args.expression, product_from_args(args)))
    elif args.command == "confirm":
        emit(
            orchestrator.confirm(
                args.pending_id,
                args.approve,
                name=args.name,
                url=args.url,
                current_price=args.price,
                target_price=args.target_price,
                notify_restock=True if args.restock else None,
            )
        )
    elif args.command == "check":
        emit(
            orchestrator.check_item(
                args.item_id,
                observation_from_args(args),
                observation_from_args(args, "fallback_"),
            )
        )
    elif args.command == "list":
        emit(repository.list_items())
    elif args.command == "state":
        emit(repository.snapshot())


if __name__ == "__main__":
    main()
