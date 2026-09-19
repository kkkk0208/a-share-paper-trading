from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from paper_trading.broker import StaticQuoteBroker
from paper_trading.config import PaperConfig
from paper_trading.ledger import PaperLedger
from paper_trading.models import MarketQuote, OrderSide, OrderStatus, SignalIntent
from paper_trading.order_service import OrderService
from paper_trading.reconciliation import reconcile


SHANGHAI = ZoneInfo("Asia/Shanghai")


def config(**overrides) -> PaperConfig:
    values = {
        "account_id": "paper-test",
        "currency": "CNY",
        "initial_cash": "100000.00",
        "paper_mode": True,
        "adapter": "deterministic",
        "whitelist": ("002195.SZ", "002815.SZ"),
        "max_order_value": "10000.00",
        "max_symbol_value": "25000.00",
        "max_total_exposure": "60000.00",
        "min_cash_buffer": "20000.00",
        "max_daily_loss": "3000.00",
        "max_quote_age_seconds": 15,
        "approval_ttl_seconds": 300,
        "lot_size": 100,
        "max_participation_rate": "0.10",
        "slippage_bps": "5",
        "fees": {
            "commission_rate": "0.00025",
            "commission_min": "5.00",
            "stamp_duty_rate": "0.0005",
            "stamp_duty_side": "SELL",
            "transfer_fee_rate": "0.00001",
            "transfer_fee_side": "BOTH",
        },
        "ruleset_version": "risk-v1",
    }
    values.update(overrides)
    return PaperConfig(**values)


def intent(intent_id: str, side: str, created: datetime, quantity: int = 100) -> SignalIntent:
    return SignalIntent(
        intent_id=intent_id,
        created_at=created,
        strategy_id="unit-test-v1",
        symbol="002195.SZ",
        side=OrderSide(side),
        quantity=quantity,
        limit_price="10.10" if side == "BUY" else "9.90",
        valid_until=created + timedelta(minutes=30),
        reason_codes=("test_signal",),
        evidence_snapshot_id=f"evidence-{intent_id}",
    )


def quote(at: datetime, bid: str = "10.00", ask: str = "10.00") -> MarketQuote:
    return MarketQuote(
        symbol="002195.SZ",
        timestamp=at,
        last="10.00",
        bid=bid,
        ask=ask,
        bid_size=1000,
        ask_size=1000,
        source="unit_test",
        tradable=True,
        limit_up="11.00",
        limit_down="9.00",
        snapshot_id=f"quote-{at.isoformat()}",
    )


class PaperTradingTests(unittest.TestCase):
    def test_configuration_refuses_live_mode_and_unknown_adapter(self):
        with self.assertRaisesRegex(ValueError, "paper_mode"):
            config(paper_mode=False)
        with self.assertRaisesRegex(ValueError, "adapter"):
            config(adapter="live_broker")

    def test_intent_requires_timezone_and_exchange_suffix(self):
        naive = datetime(2026, 9, 21, 9, 59)
        with self.assertRaisesRegex(ValueError, "timezone"):
            intent("naive", "BUY", naive)
        aware = naive.replace(tzinfo=SHANGHAI)
        row = intent("valid", "BUY", aware)
        with self.assertRaisesRegex(ValueError, "exchange suffix"):
            SignalIntent.from_dict(row.to_dict() | {"symbol": "002195"})

    def test_end_to_end_buy_is_approved_filled_idempotent_and_t1_derived_from_quote_date(self):
        created = datetime(2026, 9, 21, 9, 59, 30, tzinfo=SHANGHAI)
        tick_at = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        now = tick_at + timedelta(seconds=1)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            tick = quote(tick_at)
            service = OrderService(cfg, ledger, StaticQuoteBroker(tick), root)

            proposed = service.propose(intent("buy-001", "BUY", created), tick, now)
            self.assertEqual(proposed["status"], OrderStatus.APPROVAL_PENDING.value)
            self.assertEqual(service.approve("buy-001", "tester", "approved", now)["status"], "APPROVED")

            result = service.execute("buy-001", now + timedelta(seconds=1))
            self.assertEqual(result["status"], OrderStatus.FILLED.value)
            self.assertEqual(result["filled_quantity"], 100)
            duplicate = service.execute("buy-001", now + timedelta(seconds=2))
            self.assertEqual(duplicate["status"], OrderStatus.FILLED.value)
            self.assertEqual(ledger.position(cfg.account_id, "002195.SZ")["quantity"], 100)
            self.assertEqual(ledger.position(cfg.account_id, "002195.SZ")["available_quantity"], 0)

            changed = ledger.settle_t1(cfg.account_id, datetime(2026, 9, 22).date(), now)
            self.assertEqual(changed, 0)
            next_day = quote(datetime(2026, 9, 22, 10, 0, tzinfo=SHANGHAI))
            ledger.save_quote(next_day, next_day.timestamp)
            self.assertEqual(ledger.position(cfg.account_id, "002195.SZ")["available_quantity"], 100)
            self.assertEqual(reconcile(ledger, cfg.account_id), [])

            stale_service = OrderService(
                cfg, ledger,
                StaticQuoteBroker(quote(datetime(2026, 9, 21, 9, 30, tzinfo=SHANGHAI))),
                root,
            )
            final_again = stale_service.execute("buy-001", now + timedelta(hours=2))
            self.assertEqual(final_again["status"], OrderStatus.FILLED.value)

    def test_sell_is_rejected_before_t1(self):
        created = datetime(2026, 9, 21, 9, 59, 30, tzinfo=SHANGHAI)
        tick_at = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        now = tick_at + timedelta(seconds=1)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            tick = quote(tick_at)
            service = OrderService(cfg, ledger, StaticQuoteBroker(tick), root)
            service.propose(intent("buy-before-sell", "BUY", created), tick, now)
            service.approve("buy-before-sell", "tester", "", now)
            service.execute("buy-before-sell", now + timedelta(seconds=1))

            sell_intent = intent("sell-same-day", "SELL", created + timedelta(minutes=2))
            result = service.propose(sell_intent, tick, now + timedelta(minutes=2))
            self.assertEqual(result["decision"], "REJECTED")
            self.assertTrue(any("T+1" in reason for reason in result["rejection_reasons"]))

    def test_stale_quote_and_kill_switch_fail_closed(self):
        created = datetime(2026, 9, 21, 9, 59, 0, tzinfo=SHANGHAI)
        stale_tick = quote(datetime(2026, 9, 21, 9, 59, 1, tzinfo=SHANGHAI))
        now = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            service = OrderService(cfg, ledger, StaticQuoteBroker(stale_tick), root)
            result = service.propose(intent("stale", "BUY", created), stale_tick, now)
            self.assertIn("行情时延", " ".join(result["rejection_reasons"]))

            fresh = quote(now)
            (root / "kill_switch").write_text("on", encoding="utf-8")
            service = OrderService(cfg, ledger, StaticQuoteBroker(fresh), root)
            result = service.propose(intent("killed", "BUY", created), fresh, now)
            self.assertIn("一键停止", " ".join(result["rejection_reasons"]))

    def test_changed_payload_cannot_reuse_intent_id(self):
        created = datetime(2026, 9, 21, 9, 59, 0, tzinfo=SHANGHAI)
        with TemporaryDirectory() as temp:
            ledger = PaperLedger(Path(temp) / "paper.sqlite3")
            first = intent("same-id", "BUY", created, 100)
            ledger.add_intent(first, created)
            with self.assertRaisesRegex(ValueError, "different payload"):
                ledger.add_intent(intent("same-id", "BUY", created, 200), created)

    def test_execute_without_approval_does_not_create_order(self):
        created = datetime(2026, 9, 21, 9, 59, 30, tzinfo=SHANGHAI)
        tick_at = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        now = tick_at + timedelta(seconds=1)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            tick = quote(tick_at)
            service = OrderService(cfg, ledger, StaticQuoteBroker(tick), root)
            service.propose(intent("unapproved", "BUY", created), tick, now)
            result = service.execute("unapproved", now + timedelta(seconds=1))
            self.assertEqual(result["status"], OrderStatus.APPROVAL_PENDING.value)
            self.assertIsNone(ledger.order_for_intent("unapproved"))

    def test_small_buy_records_minimum_commission_and_transfer_fee(self):
        created = datetime(2026, 9, 21, 9, 59, 30, tzinfo=SHANGHAI)
        tick_at = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        now = tick_at + timedelta(seconds=1)
        small_quote = MarketQuote(
            symbol="002195.SZ", timestamp=tick_at, last="6.10", bid="6.09", ask="6.10",
            bid_size=10000, ask_size=10000, source="unit_test", tradable=True,
            limit_up="6.71", limit_down="5.49", snapshot_id="small-buy-quote",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            service = OrderService(cfg, ledger, StaticQuoteBroker(small_quote), root)
            small_intent = SignalIntent(
                intent_id="small-buy", created_at=created, strategy_id="unit-test-v1",
                symbol="002195.SZ", side=OrderSide.BUY, quantity=100, limit_price="6.10",
                valid_until=created + timedelta(minutes=30), reason_codes=("fee_test",),
                evidence_snapshot_id="fee-test",
            )
            service.propose(small_intent, small_quote, now)
            service.approve("small-buy", "tester", "", now)
            service.execute("small-buy", now + timedelta(seconds=1))
            with ledger.connection() as con:
                fill = con.execute("SELECT * FROM fills").fetchone()
            total_fees = sum(Decimal(fill[key]) for key in ("commission", "stamp_tax", "transfer_fee"))
            self.assertEqual(total_fees, Decimal("5.01"))
            self.assertAlmostEqual(float(total_fees / Decimal("610")), 0.0082, places=4)


if __name__ == "__main__":
    unittest.main()
