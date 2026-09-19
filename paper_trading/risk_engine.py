from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import PaperConfig
from .ledger import PaperLedger
from .models import MarketQuote, OrderSide, RiskCheck, RiskDecision, SignalIntent, money


SHANGHAI = ZoneInfo("Asia/Shanghai")


def in_continuous_session(timestamp: datetime) -> bool:
    local = timestamp.astimezone(SHANGHAI)
    if local.weekday() >= 5:
        return False
    current = local.time().replace(tzinfo=None)
    return time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)


class RiskEngine:
    def __init__(self, config: PaperConfig, ledger: PaperLedger, runtime_dir: str | Path) -> None:
        self.config = config
        self.ledger = ledger
        self.runtime_dir = Path(runtime_dir)

    def evaluate(self, intent: SignalIntent, quote: MarketQuote, now: datetime) -> RiskDecision:
        account = self.ledger.account(self.config.account_id)
        position = self.ledger.position(self.config.account_id, intent.symbol, quote.timestamp.date())
        prices = self.ledger.latest_prices() | {intent.symbol: quote.last}
        current_exposure = self.ledger.total_exposure(self.config.account_id, prices)
        current_symbol_value = money(position["quantity"] * quote.last)
        order_value = money(intent.quantity * intent.limit_price)

        checks: list[RiskCheck] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append(RiskCheck(name=name, passed=bool(passed), detail=detail))

        age = (now.astimezone(SHANGHAI) - quote.timestamp.astimezone(SHANGHAI)).total_seconds()
        check("paper_mode", self.config.paper_mode, "配置不是模拟模式")
        check("adapter", self.config.adapter in {"deterministic", "vnpy_paper"}, "适配器不在模拟白名单")
        check("kill_switch", not (self.runtime_dir / "kill_switch").exists(), "一键停止已启用")
        check("whitelist", intent.symbol in self.config.whitelist, f"{intent.symbol} 不在白名单")
        check("not_expired", now <= intent.valid_until, "交易意图已过期")
        check("quote_symbol", quote.symbol == intent.symbol, "行情证券与交易意图不一致")
        check("quote_fresh", -2 <= age <= self.config.matching.max_quote_age_seconds,
              f"行情时延 {age:.1f} 秒超过限制")
        check("trading_session", in_continuous_session(quote.timestamp), "不在连续竞价时段或不是工作日")
        check("tradable", quote.tradable, "证券当前不可交易")
        check("lot_size", intent.side == OrderSide.SELL or intent.quantity % self.config.lot_size == 0,
              f"买入数量必须是 {self.config.lot_size} 股整数倍")
        check("limit_prices_present", quote.limit_up is not None and quote.limit_down is not None,
              "行情快照缺少涨停价或跌停价，按 fail closed 拒绝")
        if quote.limit_up is not None and quote.limit_down is not None:
            check("upper_limit", intent.limit_price <= quote.limit_up, "委托价高于涨停价")
            check("lower_limit", intent.limit_price >= quote.limit_down, "委托价低于跌停价")
        check("max_order_value", order_value <= self.config.max_order_value,
              f"订单金额 {order_value} 超过单笔上限 {self.config.max_order_value}")

        if intent.side == OrderSide.BUY:
            estimated_fee = max(
                self.config.fees.commission_min,
                money(order_value * self.config.fees.commission_rate),
            ) + money(order_value * self.config.fees.transfer_fee_rate)
            check("cash", account["cash"] - order_value - estimated_fee >= self.config.min_cash_buffer,
                  "买入后现金低于安全垫")
            check("max_symbol_value", current_symbol_value + order_value <= self.config.max_symbol_value,
                  "买入后单股市值超过上限")
            check("max_total_exposure", current_exposure + order_value <= self.config.max_total_exposure,
                  "买入后总持仓超过上限")
        else:
            check("available_position", position["available_quantity"] >= intent.quantity,
                  "可用持仓不足，可能触发 T+1 或超卖")

        equity = money(account["cash"] + current_exposure)
        check("loss_guard", equity >= account["initial_cash"] - self.config.max_daily_loss,
              "账户亏损保护阈值已触发")
        decision = "APPROVAL_REQUIRED" if all(item.passed for item in checks) else "REJECTED"
        return RiskDecision(
            intent_id=intent.intent_id,
            decision=decision,
            checked_at=now,
            ruleset_version=f"{self.config.ruleset_version}:{self.config.fingerprint}",
            checks=tuple(checks),
        )
