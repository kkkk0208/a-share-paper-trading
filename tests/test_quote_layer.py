"""Quote layer tests: parsers, one-sided books, session phases, calendar and providers.

Synthetic responses exercise edge cases; pinned real captures also verify
auction books and five-level share units. Captures live in
tests/fixtures/quotes/real/ and have additional checks in test_real_quote_samples.py."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch
from zoneinfo import ZoneInfo

from paper_trading.broker import PaperBroker, StaticQuoteBroker
from paper_trading.exchange_calendar import ExchangeCalendar, sync_trade_dates, write_trade_dates
from paper_trading.ledger import PaperLedger
from paper_trading.market_data import (
    FailoverQuoteProvider, LimitFillingProvider, MemoryLimitCache, StoreFirstQuoteBroker,
)
from paper_trading.matching import PaperMatcher
from paper_trading.models import MarketQuote, OrderSide, SignalIntent
from paper_trading.order_service import OrderService
from paper_trading.quote_quality import Flag, Phase, assess_quote, session_phase
from paper_trading.quote_sources import QuoteSourceError, TencentSource, parse_sina, parse_tencent
from paper_trading.quote_store import SqliteLimitCache
from paper_trading.risk_engine import in_continuous_session
from paper_trading.symbols import from_vendor_code, is_index, split_symbol, to_vendor_code
from tests.test_paper_trading import config


SH = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 10, 15)  # a Thursday
STOCK, OTHER, INDEX = "002195.SZ", "002815.SZ", "000300.SH"


def at(hour: int, minute: int, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=SH)


def tencent_body(code: str, ts: datetime, *, last: str = "10.00", bids=(("9.99", "50"),), asks=(("10.00", "60"),),
                 limit_up: str = "10.89", limit_down: str = "8.91", prev: str = "9.90", market: str = "51") -> str:
    fields = [""] * 53
    fields[0:7] = [market, "测试", code, last, prev, prev, "100000"]
    for start, levels in ((9, bids), (19, asks)):
        padded = list(levels) + [("0.00", "0")] * (5 - len(levels))
        for i, (price, lots) in enumerate(padded):
            fields[start + 2 * i], fields[start + 2 * i + 1] = price, lots
    fields[30] = ts.strftime("%Y%m%d%H%M%S")
    fields[33], fields[34], fields[36], fields[37] = last, last, "100000", "12345.67"
    fields[47], fields[48] = limit_up, limit_down
    return "~".join(fields)


def index_body(ts: datetime, last: str = "4500.12") -> str:
    return tencent_body("000300", ts, last=last, bids=(), asks=(), limit_up="", limit_down="", market="1")


def sina_body(ts: datetime, *, last: str = "10.000", bids=(("5000", "9.990"),), asks=(("6000", "10.000"),),
              status: str = "00", prev: str = "9.900") -> str:
    bid_levels = list(bids) + [("0", "0.000")] * (5 - len(bids))
    ask_levels = list(asks) + [("0", "0.000")] * (5 - len(asks))
    fields = ["测试", prev, prev, last, last, last, bid_levels[0][1], ask_levels[0][1], "10000000", "100000000.000"]
    for size, price in (*bid_levels, *ask_levels):
        fields += [size, price]
    fields += [ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M:%S"), status]
    return ",".join(fields)


def calendar_file(root: Path, days: list[date] | None = None) -> Path:
    path = root / "trade_dates.csv"
    write_trade_dates(path, days or [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1), DAY + timedelta(days=4)])
    return path


def real_session(phase: str, source: str):
    """Pin assertions to the scheduled captures, not a relabelled later quote."""
    stamps = {"opening_call": "092002", "continuous": "100003", "closing_call": "145802"}
    fixtures = Path(__file__).parent / "fixtures" / "quotes" / "real"
    stem = f"20261015_{stamps[phase]}_{phase}_{source}"
    meta = json.loads((fixtures / f"{stem}.json").read_text(encoding="utf-8"))
    captured = datetime.fromisoformat(meta["captured_at"])
    assert meta["error"] is None
    assert meta["label"] == meta["phase"] == session_phase(captured).value == phase
    assert captured.date() == DAY and captured.utcoffset() == timedelta(hours=8)
    assert meta["file"] == f"{stem}.txt" and meta["source"] == source
    parser = {"tencent": parse_tencent, "sina": parse_sina}[source]
    return parser((fixtures / meta["file"]).read_text(encoding="utf-8"), meta["symbols"], captured)


class SymbolTests(unittest.TestCase):
    def test_suffix_is_kept_so_indices_are_not_confused(self):
        self.assertEqual(to_vendor_code("000300.SH"), "sh000300")
        self.assertEqual(to_vendor_code("002195.sz"), "sz002195")
        self.assertEqual(from_vendor_code("sz002815"), "002815.SZ")
        self.assertTrue(is_index("000300.SH") and is_index("399006.SZ"))
        self.assertFalse(is_index("000001.SZ"))
        with self.assertRaises(ValueError):
            split_symbol("002195")


class ParserTests(unittest.TestCase):
    fetched = at(10, 0, 4)

    def test_real_opening_call_has_equal_price_single_level_book(self):
        batch = real_session("opening_call", "tencent")
        for symbol, price, shares in ((STOCK, "5.90", 187900), (OTHER, "23.25", 1093900)):
            with self.subTest(symbol=symbol):
                quote = batch.quotes[symbol]
                self.assertEqual(quote.timestamp, at(9, 20))
                self.assertEqual(quote.bid_levels, ((Decimal(price), shares),))
                self.assertEqual(quote.ask_levels, quote.bid_levels)
                assessment = assess_quote(quote.to_market_quote(), now=at(9, 20, 2))
                self.assertFalse(assessment.blocking)
                self.assertNotIn(Flag.CROSSED_BOOK, assessment.flags)
                self.assertFalse(in_continuous_session(quote.timestamp))

    def test_real_continuous_five_levels_and_share_units_match_sources(self):
        expected_bids = tuple((Decimal(price), shares) for price, shares in (
            ("5.91", 1180900), ("5.90", 575000), ("5.89", 153100), ("5.88", 361000), ("5.87", 183700)))
        expected_asks = tuple((Decimal(price), shares) for price, shares in (
            ("5.92", 85800), ("5.93", 117300), ("5.94", 77000), ("5.95", 74700), ("5.96", 124000)))
        for source in ("tencent", "sina"):
            with self.subTest(source=source):
                batch = real_session("continuous", source)
                self.assertEqual(batch.errors, {})
                quote = batch.quotes[STOCK]
                self.assertEqual(quote.timestamp, at(10, 0))
                self.assertEqual(quote.bid_levels, expected_bids)
                self.assertEqual(quote.ask_levels, expected_asks)
                sealed = batch.quotes[OTHER].to_market_quote()
                self.assertEqual(sealed.last, Decimal("23.25"))
                self.assertTrue(sealed.has_bid)
                self.assertFalse(sealed.has_ask)

    def test_real_closing_call_equal_books_are_not_continuous_trading(self):
        expected = {"tencent": ((STOCK, "5.81", 182300), (OTHER, "23.25", 44400)),
                    "sina": ((STOCK, "5.81", 213500), (OTHER, "23.25", 44400))}
        for source, books in expected.items():
            batch = real_session("closing_call", source)
            for symbol, price, shares in books:
                with self.subTest(source=source, symbol=symbol):
                    quote = batch.quotes[symbol]
                    self.assertEqual(quote.bid_levels, ((Decimal(price), shares),))
                    self.assertEqual(quote.ask_levels, quote.bid_levels)
                    self.assertEqual(session_phase(quote.timestamp), Phase.CLOSING_CALL)
                    self.assertFalse(in_continuous_session(quote.timestamp))
                    self.assertFalse(assess_quote(quote.to_market_quote(), now=at(14, 58, 3)).blocking)

    def test_real_sina_opening_zero_last_is_explicitly_rejected(self):
        batch = real_session("opening_call", "sina")
        self.assertEqual(set(batch.quotes), {INDEX})
        self.assertEqual(set(batch.errors), {STOCK, OTHER})
        for reason in batch.errors.values():
            self.assertIn("last price is missing or zero", reason)

    def test_real_intraday_indices_have_no_stock_book_or_limits(self):
        for phase in ("opening_call", "continuous", "closing_call"):
            for source in ("tencent", "sina"):
                with self.subTest(phase=phase, source=source):
                    quote = real_session(phase, source).quotes[INDEX]
                    self.assertEqual(quote.timestamp.date(), DAY)
                    self.assertEqual(quote.bid_levels, ())
                    self.assertEqual(quote.ask_levels, ())
                    self.assertIsNone(quote.limit_up)
                    self.assertIsNone(quote.limit_down)
                    self.assertFalse(quote.tradable)

    def test_tencent_normal_quote_uses_shares_and_shanghai_time(self):
        text = f'v_sz002195="{tencent_body("002195", at(10, 0, 3))}";'
        quote = parse_tencent(text, [STOCK], self.fetched).quotes[STOCK]
        self.assertEqual(quote.timestamp, at(10, 0, 3))
        self.assertEqual(quote.bid_levels[0], (Decimal("9.99"), 5000))
        self.assertEqual(quote.ask_levels[0], (Decimal("10.00"), 6000))
        self.assertEqual((quote.limit_up, quote.limit_down), (Decimal("10.89"), Decimal("8.91")))
        self.assertEqual(quote.volume_shares, 10_000_000)
        self.assertEqual(quote.amount_yuan, Decimal("123456700.00"))
        market = quote.to_market_quote()
        self.assertTrue(market.has_bid and market.has_ask and market.tradable)

    def test_tencent_limit_up_keeps_quote_with_empty_ask_side(self):
        body = tencent_body("002195", at(10, 0, 3), last="10.89", bids=(("10.89", "9000"),), asks=())
        quote = parse_tencent(f'v_sz002195="{body}";', [STOCK], self.fetched).quotes[STOCK].to_market_quote()
        self.assertTrue(quote.has_bid)
        self.assertFalse(quote.has_ask)
        self.assertTrue(quote.tradable)
        self.assertEqual(quote.ask, Decimal("0.00"))

    def test_tencent_suspension_marker_blocks_even_a_visible_book(self):
        fields = tencent_body("002195", at(10, 0, 3)).split("~")
        fields[40] = "S"
        quote = parse_tencent('v_sz002195="' + "~".join(fields) + '";', [STOCK], self.fetched).quotes[STOCK]
        self.assertTrue(quote.bid_levels and quote.ask_levels)
        self.assertEqual(quote.status, "suspended")
        self.assertFalse(quote.to_market_quote().tradable)

    def test_tencent_real_suspended_and_normal_status_fields(self):
        fixtures = Path(__file__).parent / "fixtures" / "quotes" / "real"
        raw = (fixtures / "20261014_163315_suspended_tencent.txt").read_text(encoding="utf-8")
        self.assertEqual(raw.split('"')[1].split("~")[40], "S")
        halted = parse_tencent(raw, ["002717.SZ"], self.fetched).quotes["002717.SZ"]
        self.assertEqual(halted.status, "suspended")
        self.assertEqual(halted.last, Decimal("0.45"))
        self.assertEqual(halted.timestamp.date(), date(2026, 10, 14))
        self.assertFalse(halted.to_market_quote().tradable)
        normal_raw = (fixtures / "20261014_163314_after_close_tencent.txt").read_text(encoding="utf-8")
        self.assertEqual(normal_raw.split('"')[1].split("~")[40], "")
        normal = parse_tencent(normal_raw, [STOCK], self.fetched).quotes[STOCK]
        self.assertNotEqual(normal.status, "suspended")

    def test_tencent_errors_are_per_symbol(self):
        good = f'v_sz002195="{tencent_body("002195", at(10, 0, 3))}";'
        short = 'v_sz002815="51~测试~002815~10.00";'
        batch = parse_tencent(f"{good}\n{short}\nv_pv_none_match=\"1\";", [STOCK, OTHER, "600000.SH"], self.fetched)
        self.assertIn(STOCK, batch.quotes)
        self.assertIn("fields", batch.errors[OTHER])
        self.assertIn("no match", batch.errors["600000.SH"])

    def test_tencent_index_has_no_book_or_limits(self):
        quote = parse_tencent(f'v_sh000300="{index_body(at(10, 0, 3))}";', [INDEX], self.fetched).quotes[INDEX]
        self.assertEqual(quote.bid_levels, ())
        self.assertIsNone(quote.limit_up)
        self.assertFalse(quote.to_market_quote().tradable)

    def test_sina_quote_and_halt_status(self):
        normal = parse_sina(f'var hq_str_sz002195="{sina_body(at(10, 0, 3))}";', [STOCK], self.fetched).quotes[STOCK]
        self.assertEqual(normal.bid_levels[0], (Decimal("9.990"), 5000))
        self.assertIsNone(normal.limit_up)
        halted = parse_sina(
            f'var hq_str_sz002195="{sina_body(at(10, 0, 3), bids=(), asks=(), status="03")}";', [STOCK], self.fetched,
        ).quotes[STOCK]
        self.assertEqual(halted.status, "suspended")
        self.assertFalse(halted.tradable)
        empty = parse_sina('var hq_str_sz002195="";', [STOCK], self.fetched)
        self.assertIn("empty payload", empty.errors[STOCK])

    def test_sina_halt_with_zero_last_uses_previous_close_as_reference(self):
        raw = sina_body(at(15, 36), last="0.000", bids=(), asks=(), status="03", prev="0.450")
        quote = parse_sina(f'var hq_str_sz002195="{raw}";', [STOCK], self.fetched).quotes[STOCK]
        self.assertEqual(quote.last, Decimal("0.450"))
        self.assertEqual(quote.status, "suspended")
        self.assertFalse(quote.tradable)

    def test_unreadable_payload_is_a_source_error(self):
        source = TencentSource(fetch_text=lambda url: "<html>blocked</html>")
        with self.assertRaises(QuoteSourceError):
            source.fetch([STOCK])

    def test_tencent_retries_over_http_when_https_transport_fails(self):
        raw = f'v_sz002195="{tencent_body("002195", at(10, 0, 3))}";'
        with patch("paper_trading.quote_sources.http_get_gbk", side_effect=[OSError("TLS EOF"), raw]) as fetch:
            self.assertEqual(TencentSource().fetch_text([STOCK]), raw)
        https_url = "https://qt.gtimg.cn/q=sz002195"
        self.assertEqual(fetch.call_args_list, [call(https_url), call(https_url.replace("https://", "http://", 1))])


class ModelAndRuleTests(unittest.TestCase):
    def test_empty_side_is_allowed_negative_is_not(self):
        quote = MarketQuote(symbol=STOCK, timestamp=at(10, 0), last="10.89", bid="10.89", ask="0", bid_size=900,
                            ask_size=0, source="t", limit_up="10.89", limit_down="8.91")
        self.assertEqual(quote.ask_levels, ())
        with self.assertRaises(ValueError):
            MarketQuote(symbol=STOCK, timestamp=at(10, 0), last="10", bid="-1", ask="10", bid_size=1,
                        ask_size=1, source="t")

    def test_session_phases_include_closing_call_auction(self):
        self.assertEqual(session_phase(at(9, 20)), Phase.OPENING_CALL)
        self.assertEqual(session_phase(at(9, 27)), Phase.PRE_OPEN_WAIT)
        self.assertEqual(session_phase(at(11, 30)), Phase.LUNCH_BREAK)
        self.assertEqual(session_phase(at(14, 58)), Phase.CLOSING_CALL)
        self.assertEqual(session_phase(at(15, 0)), Phase.AFTER_CLOSE)
        self.assertEqual(session_phase(at(10, 0, day=date(2026, 10, 17))), Phase.CLOSED)
        self.assertTrue(in_continuous_session(at(14, 56, 59)))
        self.assertFalse(in_continuous_session(at(14, 58)))

    def test_assessment_flags(self):
        sealed = MarketQuote(symbol=STOCK, timestamp=at(10, 0), last="10.89", bid="10.89", ask="0", bid_size=900,
                             ask_size=0, source="t", limit_up="10.89", limit_down="8.91")
        self.assertIn(Flag.LIMIT_UP_SEALED, assess_quote(sealed, now=at(10, 0, 1)).flags)
        crossed = MarketQuote(symbol=STOCK, timestamp=at(10, 0), last="10", bid="10.02", ask="10.01",
                              bid_size=1, ask_size=1, source="t", limit_up="11", limit_down="9")
        self.assertTrue(assess_quote(crossed, now=at(10, 0, 1)).blocking)
        old = assess_quote(sealed, now=at(10, 0, 1), expected_date=DAY + timedelta(days=1))
        self.assertIn(Flag.NOT_TODAY, old.flags)
        future = assess_quote(sealed, now=at(9, 59, 50))
        self.assertIn(Flag.CLOCK_SKEW, future.flags)

    def test_matching_and_risk_allow_selling_into_limit_up(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            ledger = PaperLedger(root / "paper.sqlite3")
            ledger.initialize_account(cfg, at(9, 0, day=DAY - timedelta(days=1)))
            calendar = calendar_file(root)
            normal = MarketQuote(symbol=STOCK, timestamp=at(10, 0, day=DAY - timedelta(days=1)), last="10.00",
                                 bid="10.00", ask="10.00", bid_size=10000, ask_size=10000, source="t",
                                 limit_up="11.00", limit_down="9.00", snapshot_id="q1")
            service = OrderService(cfg, ledger, StaticQuoteBroker(normal), root, calendar)
            buy = SignalIntent(intent_id="b1", created_at=normal.timestamp - timedelta(seconds=30), strategy_id="s",
                               symbol=STOCK, side=OrderSide.BUY, quantity=100, limit_price="10.10",
                               valid_until=normal.timestamp + timedelta(minutes=30), reason_codes=("t",),
                               evidence_snapshot_id="e")
            service.propose(buy, normal, normal.timestamp + timedelta(seconds=1))
            service.approve("b1", "me", "", normal.timestamp + timedelta(seconds=1))
            service.broker.quote = MarketQuote(**(normal.to_dict() | {
                "timestamp": normal.timestamp + timedelta(seconds=3), "snapshot_id": "q2"}))
            self.assertEqual(service.execute("b1", normal.timestamp + timedelta(seconds=4))["status"], "FILLED")

            sealed = MarketQuote(symbol=STOCK, timestamp=at(10, 0), last="11.00", bid="11.00", ask="0",
                                 bid_size=90000, ask_size=0, source="t", limit_up="11.00", limit_down="9.00",
                                 snapshot_id="q3")
            sell = SignalIntent(intent_id="s1", created_at=at(9, 59, 30), strategy_id="s", symbol=STOCK,
                                side=OrderSide.SELL, quantity=100, limit_price="11.00",
                                valid_until=at(10, 30), reason_codes=("t",), evidence_snapshot_id="e")
            proposed = service.propose(sell, sealed, at(10, 0, 1))
            self.assertEqual(proposed["decision"], "APPROVAL_REQUIRED", proposed["rejection_reasons"])
            buy_more = SignalIntent(intent_id="b2", created_at=at(9, 59, 30), strategy_id="s", symbol=STOCK,
                                    side=OrderSide.BUY, quantity=100, limit_price="11.00",
                                    valid_until=at(10, 30), reason_codes=("t",), evidence_snapshot_id="e")
            rejected = service.propose(buy_more, sealed, at(10, 0, 1))
            self.assertTrue(any("卖盘为空" in reason for reason in rejected["rejection_reasons"]))
            fill = PaperMatcher(cfg).match(sell, sealed, 100, at(9, 59, 50), 1, at(10, 0, 1))
            self.assertEqual(fill.price, Decimal("11.00"))
            self.assertIsNone(PaperMatcher(cfg).match(buy_more, sealed, 100, at(9, 59, 50), 1, at(10, 0, 1)))


class CalendarTests(unittest.TestCase):
    def test_statuses_and_messages(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            calendar = ExchangeCalendar(calendar_file(root))
            self.assertEqual(calendar.status(DAY), "trading")
            self.assertEqual(calendar.status(DAY + timedelta(days=2)), "closed")
            self.assertEqual(calendar.status(DAY + timedelta(days=30)), "after_coverage")
            ok, message = calendar.check(DAY + timedelta(days=30))
            self.assertFalse(ok)
            self.assertIn("只覆盖到", message)
            missing_ok, missing_message = ExchangeCalendar(root / "none.csv").check(DAY)
            self.assertFalse(missing_ok)
            self.assertIn("sync-calendar", missing_message)

    def test_old_benchmark_style_file_is_still_readable(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "old.csv"
            path.write_text("date,close\n2026-10-14,4000\n2026-10-15,4010\n", encoding="utf-8")
            self.assertTrue(ExchangeCalendar(path).is_trading_day(DAY))

    def test_sync_writes_sorted_unique_dates(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "data" / "trade_dates.csv"
            result = sync_trade_dates(path, loader=lambda: [DAY, DAY - timedelta(days=1), DAY])
            self.assertEqual(result["count"], 2)
            self.assertEqual(path.read_text(encoding="utf-8").splitlines(), ["trade_date", "2026-10-14", "2026-10-15"])
            with self.assertRaises(ValueError):
                sync_trade_dates(path, loader=lambda: [])


class ProviderTests(unittest.TestCase):
    def quote(self, ts: datetime, limits: bool = True, source: str = "tencent_level1_snapshot") -> MarketQuote:
        return MarketQuote(symbol=STOCK, timestamp=ts, last="10", bid="9.99", ask="10", bid_size=100, ask_size=100,
                           source=source, limit_up="10.89" if limits else None, limit_down="8.91" if limits else None)

    def test_failover_returns_to_primary_after_recovery_interval(self):
        ticks = {"now": 0.0}
        state = {"primary_down": True, "primary_calls": 0}
        expected_primary = self.quote(at(10, 0))
        expected_secondary = self.quote(at(10, 0), source="sina")

        class Primary:
            def latest_quote(self, symbol):
                state["primary_calls"] += 1
                if state["primary_down"]:
                    raise RuntimeError("down")
                return expected_primary

        class Secondary:
            def latest_quote(self, symbol):
                return expected_secondary

        provider = FailoverQuoteProvider(Primary(), Secondary(), 1, recovery_seconds=60, clock=lambda: ticks["now"])
        self.assertIs(provider.latest_quote(STOCK), expected_secondary)
        ticks["now"] = 30
        self.assertIs(provider.latest_quote(STOCK), expected_secondary)
        self.assertEqual(state["primary_calls"], 1)
        state["primary_down"] = False
        ticks["now"] = 61
        self.assertIs(provider.latest_quote(STOCK), expected_primary)
        self.assertFalse(provider.using_secondary)

    def test_limit_filling_uses_same_day_value_only(self):
        sequence = [self.quote(at(10, 0)), self.quote(at(10, 1), limits=False, source="sina_level1_snapshot"),
                    self.quote(at(10, 0, day=DAY + timedelta(days=1)), limits=False, source="sina_level1_snapshot")]

        class Inner:
            def latest_quote(self, symbol):
                return sequence.pop(0)

        provider = LimitFillingProvider(Inner(), MemoryLimitCache())
        provider.latest_quote(STOCK)
        filled = provider.latest_quote(STOCK)
        self.assertEqual(filled.limit_up, Decimal("10.89"))
        self.assertTrue(filled.source.endswith("+cached_limits"))
        self.assertIsNone(provider.latest_quote(STOCK).limit_up)

    def test_sqlite_limit_cache_keeps_first_value(self):
        with TemporaryDirectory() as temp:
            cache = SqliteLimitCache(Path(temp) / "paper.sqlite3")
            self.assertTrue(cache.put(STOCK, DAY, Decimal("10.89"), Decimal("8.91"), "tencent"))
            self.assertFalse(cache.put(STOCK, DAY, Decimal("11.00"), Decimal("9.00"), "tencent"))
            self.assertEqual(cache.get(STOCK, DAY), (Decimal("10.89"), Decimal("8.91")))

    def test_store_first_broker_prefers_fresh_collector_tick(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "paper.sqlite3"
            ledger = PaperLedger(db)
            stored = self.quote(at(10, 0))
            ledger.save_quote(MarketQuote(**(stored.to_dict() | {"snapshot_id": "stored"})))
            live_quote = self.quote(at(10, 0, 30), source="live")

            class Live(PaperBroker):
                def latest_quote(self, symbol):
                    return live_quote

                def adapter_name(self):
                    return "deterministic"

            clock = {"now": at(10, 0, 2)}
            broker = StoreFirstQuoteBroker(db, Live(), max_age_seconds=6, clock=lambda: clock["now"])
            self.assertEqual(broker.latest_quote(STOCK).snapshot_id, "stored")
            clock["now"] = at(10, 0, 30)
            self.assertIs(broker.latest_quote(STOCK), live_quote)


if __name__ == "__main__":
    unittest.main()
