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
from paper_trading.market_data import FailoverQuoteProvider
from paper_trading.models import MarketQuote, OrderSide, OrderStatus, SignalIntent
from paper_trading.order_service import OrderService
from paper_trading.performance import calculate_performance
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
        "approval_ttl_seconds": 300,
        "intent_ttl_minutes": 30,
        "quote_failover_failures": 3,
        "benchmark_symbol": "000300.SH",
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
        "matching": {
            "min_depth_ratio": "3.0",
            "min_ticks_delay": 1,
            "max_quote_age_seconds": 10,
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
    def test_legacy_position_is_migrated_to_one_historical_lot_once(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "paper.sqlite3"
            cfg = config()
            ledger = PaperLedger(db)
            ledger.initialize_account(cfg)
            with ledger.transaction() as con:
                con.execute("DELETE FROM schema_migrations WHERE name='positions_to_lots_v1'")
                con.execute(
                    "INSERT INTO positions(account_id,symbol,quantity,available_quantity,t0_quantity,t0_date,average_cost,realized_pnl,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (cfg.account_id, "002195.SZ", 300, 300, 0, None, "6.50", "0", "2026-01-01T00:00:00+08:00"),
                )
            migrated = PaperLedger(db)
            position = migrated.position(cfg.account_id, "002195.SZ", datetime(2026, 9, 1).date())
            self.assertEqual(position["quantity"], 300)
            self.assertEqual(position["available_quantity"], 300)
            self.assertEqual(position["average_cost"], Decimal("6.50"))
            PaperLedger(db)
            with migrated.connection() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) count FROM position_lots").fetchone()["count"], 1)

    def test_quote_provider_fails_closed_then_switches_without_cache(self):
        expected = quote(datetime(2026, 9, 21, 10, 0, tzinfo=SHANGHAI))

        class FailingProvider:
            def latest_quote(self, symbol):
                raise RuntimeError("primary down")

        class SecondaryProvider:
            def __init__(self, fail=False):
                self.fail = fail

            def latest_quote(self, symbol):
                if self.fail:
                    raise RuntimeError("secondary down")
                return expected

        provider = FailoverQuoteProvider(FailingProvider(), SecondaryProvider(), failure_threshold=2)
        with self.assertRaisesRegex(RuntimeError, "fail closed"):
            provider.latest_quote("002195.SZ")
        self.assertIs(provider.latest_quote("002195.SZ"), expected)

        failed = FailoverQuoteProvider(FailingProvider(), SecondaryProvider(fail=True), failure_threshold=1)
        with self.assertRaisesRegex(RuntimeError, "all quote providers failed"):
            failed.latest_quote("002195.SZ")

    def test_performance_uses_equity_curve_and_csi300(self):
        with TemporaryDirectory() as temp:
            cfg = config()
            ledger = PaperLedger(Path(temp) / "paper.sqlite3")
            ledger.initialize_account(cfg)
            with ledger.transaction() as con:
                con.executemany(
                    "INSERT INTO equity_curve(account_id,trade_date,equity,benchmark_close,created_at) VALUES(?,?,?,?,?)",
                    [
                        (cfg.account_id, "2026-01-02", "100000", "4000", "2026-01-02T15:01:00+08:00"),
                        (cfg.account_id, "2026-01-03", "90000", "3900", "2026-01-03T15:01:00+08:00"),
                        (cfg.account_id, "2026-01-12", "110000", "4200", "2026-01-12T15:01:00+08:00"),
                    ],
                )
            result = calculate_performance(
                ledger, cfg.account_id, datetime(2026, 1, 1).date(), datetime(2026, 1, 31).date(),
            )
            self.assertEqual(result["cumulative_return"], Decimal("0.1"))
            self.assertEqual(result["max_drawdown"], Decimal("-0.1"))
            self.assertEqual(result["benchmark_return"], Decimal("0.05"))
            self.assertEqual(result["excess_return"], Decimal("0.05"))

    def test_corporate_action_adjusts_each_lot_once(self):
        with TemporaryDirectory() as temp:
            cfg = config()
            ledger = PaperLedger(Path(temp) / "paper.sqlite3")
            now = datetime(2026, 6, 1, 16, 0, tzinfo=SHANGHAI)
            ledger.initialize_account(cfg, now)
            with ledger.transaction() as con:
                con.execute(
                    "INSERT INTO position_lots(account_id,symbol,trade_date,quantity,cost_price,created_at) VALUES(?,?,?,?,?,?)",
                    (cfg.account_id, "002195.SZ", "2026-05-01", 100, "10.00", now.isoformat()),
                )
            ledger.add_corporate_action(
                "002195.SZ", datetime(2026, 6, 1).date(), Decimal("0.50"),
                Decimal("0.10"), Decimal("1"), now,
            )
            applied = ledger.apply_corporate_actions(cfg.account_id, datetime(2026, 6, 1).date(), now)
            self.assertEqual(applied[0]["quantity_after"], 110)
            self.assertEqual(applied[0]["cash_dividend"], Decimal("50.00"))
            self.assertEqual(ledger.account(cfg.account_id)["cash"], Decimal("100050.00"))
            position = ledger.position(cfg.account_id, "002195.SZ", datetime(2026, 6, 2).date())
            self.assertEqual(position["quantity"], 110)
            self.assertEqual(position["average_cost"], Decimal("8.64"))
            self.assertEqual(reconcile(ledger, cfg.account_id), [])
            self.assertEqual(
                ledger.apply_corporate_actions(cfg.account_id, datetime(2026, 6, 1).date(), now), []
            )

    def test_corporate_action_refuses_retroactive_adjustment_after_fill(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            created = datetime(2026, 6, 1, 9, 59, 30, tzinfo=SHANGHAI)
            first_quote = quote(datetime(2026, 6, 1, 10, 0, tzinfo=SHANGHAI))
            ledger.initialize_account(cfg, created)
            service = OrderService(cfg, ledger, StaticQuoteBroker(first_quote), root)
            service.propose(intent("retroactive-ca", "BUY", created), first_quote, first_quote.timestamp)
            service.approve("retroactive-ca", "tester", "checked", first_quote.timestamp)
            service.broker.quote = quote(first_quote.timestamp + timedelta(seconds=2))
            service.execute("retroactive-ca", first_quote.timestamp + timedelta(seconds=2))
            ledger.add_corporate_action(
                "002195.SZ", datetime(2026, 6, 1).date(), Decimal("0.10"),
                Decimal("0"), Decimal("1"), first_quote.timestamp,
            )
            with self.assertRaisesRegex(ValueError, "拒绝事后调整"):
                ledger.apply_corporate_actions(
                    cfg.account_id, datetime(2026, 6, 1).date(), first_quote.timestamp,
                )

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

            service.broker.quote = quote(tick_at + timedelta(seconds=2))
            result = service.execute("buy-001", now + timedelta(seconds=2))
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
            service.broker.quote = quote(tick_at + timedelta(seconds=2))
            service.execute("buy-before-sell", now + timedelta(seconds=2))

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

    def test_missing_snapshot_limit_prices_fails_closed(self):
        created = datetime(2026, 9, 21, 9, 59, 30, tzinfo=SHANGHAI)
        now = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        no_limits = MarketQuote(
            symbol="002195.SZ", timestamp=now, last="10", bid="9.99", ask="10.01",
            bid_size=1000, ask_size=1000, source="unit_test", tradable=True,
            limit_up=None, limit_down=None, snapshot_id="no-limits",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            service = OrderService(cfg, ledger, StaticQuoteBroker(no_limits), root)
            result = service.propose(intent("no-limits", "BUY", created), no_limits, now)
            self.assertEqual(result["decision"], "REJECTED")
            self.assertIn("缺少涨停价", " ".join(result["rejection_reasons"]))

    def test_configured_trading_calendar_fails_closed_when_missing(self):
        created = datetime(2026, 9, 21, 9, 59, 30, tzinfo=SHANGHAI)
        tick = quote(datetime(2026, 9, 21, 10, 0, tzinfo=SHANGHAI))
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            service = OrderService(
                cfg, ledger, StaticQuoteBroker(tick), root, root / "missing-calendar.csv",
            )
            result = service.propose(intent("calendar-missing", "BUY", created), tick, tick.timestamp)
            self.assertIn(
                "正式交易日历不可用或行情日期不是交易日",
                result["rejection_reasons"],
            )

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
            execution_quote = MarketQuote(**(
                small_quote.to_dict() | {
                    "timestamp": (tick_at + timedelta(seconds=2)).isoformat(),
                    "snapshot_id": "small-buy-execution-quote",
                }
            ))
            service.broker.quote = execution_quote
            service.execute("small-buy", now + timedelta(seconds=2))
            with ledger.connection() as con:
                fill = con.execute("SELECT * FROM fills").fetchone()
            total_fees = sum(Decimal(fill[key]) for key in ("commission", "stamp_tax", "transfer_fee"))
            self.assertEqual(total_fees, Decimal("5.01"))
            self.assertAlmostEqual(float(total_fees / Decimal("610")), 0.0082, places=4)
            self.assertEqual(fill["quote_timestamp"], execution_quote.timestamp.isoformat())
            self.assertEqual(json.loads(fill["order_book_json"])["asks"][0], ["6.10", 10000])

    def test_matching_requires_post_approval_tick_and_depth(self):
        created = datetime(2026, 9, 21, 9, 59, 30, tzinfo=SHANGHAI)
        tick_at = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        now = tick_at + timedelta(seconds=1)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            service = OrderService(cfg, ledger, StaticQuoteBroker(quote(tick_at)), root)
            service.propose(intent("depth-check", "BUY", created), quote(tick_at), now)
            service.approve("depth-check", "tester", "", now)
            shallow = MarketQuote(**(
                quote(tick_at + timedelta(seconds=2)).to_dict()
                | {"ask_size": 299, "ask_levels": [["10.00", 299]]}
            ))
            service.broker.quote = shallow
            result = service.execute("depth-check", now + timedelta(seconds=2))
            self.assertEqual(result["status"], OrderStatus.SUBMITTED.value)
            with ledger.connection() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) count FROM fills").fetchone()["count"], 0)

    def test_approved_intent_expires_and_cancel_is_audited(self):
        created = datetime(2026, 9, 21, 10, 0, 0, tzinfo=SHANGHAI)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(intent_ttl_minutes=1)
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, created)
            tick = quote(created)
            service = OrderService(cfg, ledger, StaticQuoteBroker(tick), root)
            service.propose(intent("will-expire", "BUY", created), tick, created)
            service.approve("will-expire", "tester", "", created)
            result = service.execute("will-expire", created + timedelta(minutes=2))
            self.assertEqual(result["status"], OrderStatus.EXPIRED.value)
            self.assertEqual(len(ledger.list_intents("expired")), 1)
            with ledger.connection() as con:
                event = con.execute(
                    "SELECT event_type FROM intent_events WHERE intent_id=? ORDER BY id DESC LIMIT 1",
                    ("will-expire",),
                ).fetchone()
            self.assertEqual(event["event_type"], OrderStatus.EXPIRED.value)

            service.propose(intent("will-cancel", "BUY", created), tick, created)
            canceled = ledger.cancel_intent("will-cancel", "operator canceled", created)
            self.assertEqual(canceled["status"], OrderStatus.CANCELED.value)
            self.assertEqual(len(ledger.list_intents("rejected")), 1)


if __name__ == "__main__":
    unittest.main()
