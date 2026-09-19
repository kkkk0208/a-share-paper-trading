from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .broker import StaticQuoteBroker
from .config import load_config
from .ledger import PaperLedger
from .market_data import TencentPaperQuoteBroker
from .models import MarketQuote, OrderSide, SignalIntent
from .order_service import OrderService
from .performance import calculate_performance
from .reconciliation import reconcile
from .report_adapter import paper_account_summary
from .signal_adapter import load_intent


def now_local() -> datetime:
    return datetime.now().astimezone()


def json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=json_default))


def load_quote(path: str | None, symbol: str, failure_threshold: int = 3) -> tuple[Any, MarketQuote]:
    if not path:
        broker = TencentPaperQuoteBroker(failure_threshold=failure_threshold)
        return broker, broker.latest_quote(symbol)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    quote = MarketQuote(**data)
    return StaticQuoteBroker(quote), quote


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="A-share paper trading CLI (paper only)")
    result.add_argument("--config", required=True)
    result.add_argument("--db", required=True)
    result.add_argument("--runtime-dir", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init")

    draft = commands.add_parser("draft")
    draft.add_argument("--symbol", required=True)
    draft.add_argument("--side", required=True, choices=["BUY", "SELL"])
    draft.add_argument("--quantity", required=True, type=int)
    draft.add_argument("--limit-price", required=True)
    draft.add_argument("--reason", action="append", required=True)
    draft.add_argument("--strategy-id", default="manual-paper-v1")
    draft.add_argument("--valid-minutes", type=int)
    draft.add_argument("--output", required=True)

    propose = commands.add_parser("propose")
    propose.add_argument("--intent", required=True)
    propose.add_argument("--quote")

    approve = commands.add_parser("approve")
    approve.add_argument("--intent-id", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--note", default="")

    reject = commands.add_parser("reject")
    reject.add_argument("--intent-id", required=True)
    reject.add_argument("--actor", required=True)
    reject.add_argument("--note", required=True)

    execute = commands.add_parser("execute")
    execute.add_argument("--intent-id", required=True)
    execute.add_argument("--quote")

    commands.add_parser("status")
    settle = commands.add_parser("settle")
    settle.add_argument("--trade-date", required=True)
    commands.add_parser("reconcile")
    commands.add_parser("kill")
    commands.add_parser("resume")
    intents = commands.add_parser("intents")
    intents.add_argument("--status", choices=["pending", "approved", "executed", "rejected", "expired"])
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--intent-id", required=True)
    cancel.add_argument("--reason", required=True)
    commands.add_parser("snapshot")
    perf = commands.add_parser("perf")
    perf.add_argument("--from", dest="from_date", required=True)
    perf.add_argument("--to", dest="to_date", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_config(args.config)
    ledger = PaperLedger(args.db)
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    when = now_local()

    if args.command == "init":
        ledger.initialize_account(config, when)
        print_json({"status": "INITIALIZED", "account": paper_account_summary(ledger, config.account_id)})
        return 0
    if args.command == "draft":
        from datetime import timedelta
        token = uuid.uuid4().hex[:10]
        intent = SignalIntent(
            intent_id=f"{when.strftime('%Y%m%d%H%M%S')}-{args.symbol}-{args.side.lower()}-{token}",
            created_at=when,
            strategy_id=args.strategy_id,
            symbol=args.symbol,
            side=OrderSide(args.side),
            quantity=args.quantity,
            limit_price=args.limit_price,
            valid_until=when + timedelta(minutes=args.valid_minutes or config.intent_ttl_minutes),
            reason_codes=tuple(args.reason),
            evidence_snapshot_id=f"manual-{token}",
            requires_user_approval=True,
        )
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(intent.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print_json({"status": "DRAFTED", "intent_id": intent.intent_id, "output": str(destination)})
        return 0
    if args.command == "kill":
        (runtime / "kill_switch").write_text(when.isoformat(), encoding="utf-8")
        print_json({"status": "KILL_SWITCH_ENABLED"})
        return 0
    if args.command == "resume":
        (runtime / "kill_switch").unlink(missing_ok=True)
        print_json({"status": "KILL_SWITCH_DISABLED"})
        return 0
    if args.command == "status":
        print_json(paper_account_summary(ledger, config.account_id))
        return 0
    if args.command == "settle":
        ledger.settle_t1(config.account_id, date.fromisoformat(args.trade_date), when)
        print_json({
            "status": "NO_OP",
            "message": "settle 已弃用：可用数量按持仓批次和最新行情交易日实时计算；下个版本将删除此命令。",
            "positions_changed": 0,
        })
        return 0
    if args.command == "reconcile":
        errors = reconcile(ledger, config.account_id)
        print_json({"status": "OK" if not errors else "FAILED", "errors": errors})
        return 0 if not errors else 2
    if args.command == "intents":
        for row in ledger.list_intents():
            ledger.expire_intent_if_due(row["intent_id"], config, when)
        rows = ledger.list_intents(args.status)
        print_json({"status": args.status or "all", "count": len(rows), "intents": rows})
        return 0
    if args.command == "cancel":
        print_json(ledger.cancel_intent(args.intent_id, args.reason, when))
        return 0
    if args.command == "snapshot":
        broker = TencentPaperQuoteBroker(failure_threshold=config.quote_failover_failures)
        for position in ledger.positions(config.account_id):
            ledger.save_quote(broker.latest_quote(position["symbol"]), when)
        benchmark = broker.latest_quote(config.benchmark_symbol)
        if benchmark.timestamp.astimezone().hour < 15:
            raise ValueError("净值快照只能使用收盘后的基准行情")
        result = ledger.record_equity_snapshot(
            config.account_id, benchmark.timestamp.date(), benchmark.last, when,
        )
        print_json({"status": "SNAPSHOT_RECORDED", **result, "benchmark": config.benchmark_symbol})
        return 0
    if args.command == "perf":
        print_json(calculate_performance(
            ledger, config.account_id, date.fromisoformat(args.from_date), date.fromisoformat(args.to_date),
        ))
        return 0

    if args.command == "propose":
        intent = load_intent(args.intent)
        broker, quote = load_quote(args.quote, intent.symbol, config.quote_failover_failures)
        service = OrderService(config, ledger, broker, runtime)
        print_json(service.propose(intent, quote, when))
        return 0
    if args.command in {"approve", "reject"}:
        service = OrderService(config, ledger, StaticQuoteBroker(MarketQuote(
            symbol=ledger.get_intent(args.intent_id).symbol,
            timestamp=when,
            last="1", bid="1", ask="1", bid_size=0, ask_size=0,
            source="approval_only", tradable=False, limit_up="1", limit_down="1",
        )), runtime)
        if args.command == "approve":
            print_json(service.approve(args.intent_id, args.actor, args.note, when))
        else:
            print_json(service.reject(args.intent_id, args.actor, args.note, when))
        return 0
    if args.command == "execute":
        intent = ledger.get_intent(args.intent_id)
        broker, _ = load_quote(getattr(args, "quote", None), intent.symbol, config.quote_failover_failures)
        service = OrderService(config, ledger, broker, runtime)
        print_json(service.execute(args.intent_id, when))
        return 0
    return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
