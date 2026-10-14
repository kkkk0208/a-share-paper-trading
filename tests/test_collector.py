"""Collector tests with a fake market that serves Tencent and Sina style text."""

from __future__ import annotations

import json
import unittest
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paper_trading.collector import CollectorSettings, QuoteCollector, build_report, capture_samples, main
from paper_trading.exchange_calendar import ExchangeCalendar
from paper_trading.quote_sources import SinaSource, TencentSource
from paper_trading.quote_store import QuoteStore
from paper_trading.symbols import to_vendor_code
from tests.test_quote_layer import DAY, INDEX, OTHER, STOCK, at, calendar_file, index_body, sina_body, tencent_body


class FakeMarket:
    """Serves Tencent and Sina style text for whatever codes a URL asks for."""

    def __init__(self) -> None:
        self.tencent_bodies: dict[str, str] = {}
        self.sina_bodies: dict[str, str] = {}
        self.tencent_error: Exception | None = None
        self.sina_error: Exception | None = None
        self.calls: Counter[str] = Counter()

    def tencent(self, url: str) -> str:
        self.calls["tencent"] += 1
        if self.tencent_error:
            raise self.tencent_error
        codes = url.split("q=", 1)[1].split(",")
        return "\n".join(f'v_{code}="{self.tencent_bodies[code]}";' for code in codes if code in self.tencent_bodies)

    def sina(self, url: str) -> str:
        self.calls["sina"] += 1
        if self.sina_error:
            raise self.sina_error
        codes = url.split("list=", 1)[1].split(",")
        return "\n".join(f'var hq_str_{code}="{self.sina_bodies[code]}";' for code in codes if code in self.sina_bodies)

    def set_all(self, ts: datetime, last: str = "10.00") -> None:
        for symbol in (STOCK, OTHER):
            code = to_vendor_code(symbol)
            self.tencent_bodies[code] = tencent_body(code[2:], ts, last=last)
            self.sina_bodies[code] = sina_body(ts, last=last)
        self.tencent_bodies["sh000300"] = index_body(ts)
        self.sina_bodies["sh000300"] = sina_body(ts, last="4500.120", bids=(), asks=())


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_collector(root: Path, market: FakeMarket, clock: Clock, calendar_days: list[date] | None = None,
                   **settings) -> QuoteCollector:
    return QuoteCollector(
        QuoteStore(root / "paper.sqlite3"),
        TencentSource(fetch_text=market.tencent), SinaSource(fetch_text=market.sina),
        ExchangeCalendar(calendar_file(root, calendar_days)),
        CollectorSettings(symbols=(STOCK, OTHER, INDEX), **settings), clock=clock,
    )


def events(store: QuoteStore, event_type: str | None = None) -> list[dict]:
    rows = store.events_for_day(DAY)
    return [row for row in rows if event_type is None or row["event_type"] == event_type]


class CollectorTests(unittest.TestCase):
    def test_scheduled_capture_rejects_a_missed_phase_or_date_without_fetching(self):
        base = ["capture", "--symbols", STOCK, "--label", "opening_call",
                "--expected-date", DAY.isoformat(), "--expected-phase", "opening_call"]
        for now in (at(15, 2), at(9, 20, day=DAY + timedelta(days=1))):
            with self.subTest(now=now), patch("paper_trading.collector.local_now", return_value=now), \
                    patch("paper_trading.collector.capture_samples") as capture:
                self.assertEqual(main(base), 2)
                capture.assert_not_called()

    def test_scheduled_capture_records_correct_phase_and_propagates_source_errors(self):
        args = ["capture", "--symbols", STOCK, "--label", "opening_call",
                "--expected-date", DAY.isoformat(), "--expected-phase", "opening_call"]
        for error, expected_exit in ((None, 0), ("network down", 2)):
            with self.subTest(error=error), patch("paper_trading.collector.local_now", return_value=at(9, 20)), \
                    patch("paper_trading.collector.capture_samples", return_value=[{"error": error}]) as capture:
                self.assertEqual(main(args), expected_exit)
                self.assertEqual(capture.call_args.kwargs["now"], at(9, 20))

    def test_cycle_stores_ticks_once_and_marks_phase(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(10, 0, 4))
            market.set_all(at(10, 0, 3))
            collector = make_collector(root, market, clock)
            first = collector.run_cycle()
            self.assertEqual((first.stored, first.unchanged, first.missing), (3, 0, []))
            clock.sleep(3)
            self.assertEqual(collector.run_cycle().unchanged, 3)
            market.set_all(at(10, 0, 6))
            clock.sleep(3)
            self.assertEqual(collector.run_cycle().stored, 3)
            ticks = collector.store.ticks_for_day(STOCK, DAY)
            self.assertEqual(len(ticks), 2)
            self.assertEqual(ticks[0]["payload"]["collector"]["phase"], "continuous")
            self.assertIn("raw", ticks[0]["payload"]["collector"])
            self.assertNotIn("raw", ticks[1]["payload"]["collector"])
            self.assertEqual(collector.store.limits.get(STOCK, DAY), (Decimal("10.89"), Decimal("8.91")))
            self.assertEqual(collector.store.ledger.latest_trade_date(STOCK), DAY)

    def test_backup_source_fills_cached_limits_and_errors_are_logged_once(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(10, 0, 4))
            market.set_all(at(10, 0, 3))
            collector = make_collector(root, market, clock)
            collector.run_cycle()
            market.tencent_error = OSError("timed out")
            for second in (6, 9):
                market.set_all(at(10, 0, second))
                clock.sleep(3)
                self.assertEqual(collector.run_cycle().sources, {"sina": 3})
            tick = collector.store.ticks_for_day(STOCK, DAY)[-1]["payload"]
            self.assertEqual(tick["limit_up"], "10.89")
            self.assertIn("cached_limits", tick["collector"]["flags"])
            self.assertTrue(tick["source"].endswith("+cached_limits"))
            self.assertEqual(len(events(collector.store, "SOURCE_ERROR")), 1)
            market.tencent_error = None
            market.set_all(at(10, 0, 12))
            clock.sleep(3)
            self.assertEqual(collector.run_cycle().sources, {"tencent": 3})
            self.assertEqual(len(events(collector.store, "SOURCE_RECOVERED")), 1)

    def test_blocking_snapshots_become_events(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(10, 0, 4))
            market.set_all(at(10, 0, 3))
            yesterday = at(15, 0, 3, day=DAY - timedelta(days=1))
            market.tencent_bodies["sz002815"] = tencent_body("002815", yesterday)
            collector = make_collector(root, market, clock)
            self.assertEqual(collector.run_cycle().rejected, 1)
            clock.sleep(3)
            collector.run_cycle()
            rejected = events(collector.store, "SNAPSHOT_REJECTED")
            self.assertEqual(len(rejected), 1)
            self.assertIn("not_today", rejected[0]["detail"]["flags"])
            self.assertEqual(collector.store.ticks_for_day(OTHER, DAY), [])

            market.tencent_error = OSError("down")
            market.sina_bodies["sz002195"] = sina_body(at(10, 0, 1))
            clock.sleep(3)
            self.assertGreaterEqual(collector.run_cycle().rejected, 1)
            self.assertTrue(any(
                "non_monotonic" in row["detail"]["flags"] for row in events(collector.store, "SNAPSHOT_REJECTED")
            ))

    def test_limit_up_is_recorded_as_market_state(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(10, 0, 4))
            market.set_all(at(10, 0, 3))
            market.tencent_bodies["sz002195"] = tencent_body(
                "002195", at(10, 0, 3), last="10.89", bids=(("10.89", "9000"),), asks=())
            collector = make_collector(root, market, clock)
            collector.run_cycle()
            tick = collector.store.ticks_for_day(STOCK, DAY)[0]["payload"]
            self.assertIn("limit_up_sealed", tick["collector"]["flags"])
            self.assertEqual(tick["ask_levels"], [])
            state = events(collector.store, "MARKET_STATE")
            self.assertEqual([row["detail"]["flags"] for row in state], [["limit_up_sealed"]])

    def test_cross_check_flags_mismatch_between_sources(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(10, 0, 4))
            market.set_all(at(10, 0, 3))
            market.sina_bodies["sz002195"] = sina_body(at(10, 0, 3), last="10.100")
            collector = make_collector(root, market, clock)
            collector.run_cycle()
            mismatches = events(collector.store, "SOURCE_MISMATCH")
            self.assertEqual([row["symbol"] for row in mismatches], [STOCK])
            self.assertEqual(mismatches[0]["detail"]["diff_bps"], "100.0")
            self.assertGreaterEqual(len(events(collector.store, "CROSS_CHECK")), 2)
            calls = market.calls["sina"]
            clock.sleep(3)
            collector.run_cycle()
            self.assertEqual(market.calls["sina"], calls)

    def test_stall_and_resume_are_logged(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(10, 0, 4))
            market.set_all(at(10, 0, 3))
            collector = make_collector(root, market, clock, stall_seconds=20)
            collector.run_cycle()
            for _ in range(8):
                clock.sleep(3)
                collector.run_cycle()
            stalled = events(collector.store, "FEED_STALLED")
            self.assertEqual(sorted(row["symbol"] for row in stalled), sorted([OTHER, STOCK, INDEX]))
            market.set_all(at(10, 0, 30))
            clock.sleep(3)
            collector.run_cycle()
            self.assertEqual(len(events(collector.store, "FEED_RESUMED")), 3)

    def test_run_exits_after_close_and_on_holidays(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(14, 59, 50))
            collector = make_collector(root, market, clock)

            def tick(seconds: float) -> None:
                clock.sleep(seconds)
                market.set_all(clock.now - timedelta(seconds=1))

            market.set_all(at(14, 59, 49))
            totals = collector.run(sleep=tick)
            self.assertGreaterEqual(clock.now, at(15, 5))
            self.assertLess(clock.now, at(15, 6))
            self.assertGreater(totals["stored"], 0)

        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(10, 0))
            # DAY is a weekday inside the calendar's coverage but not listed, so it is a holiday.
            collector = make_collector(root, market, clock, calendar_days=[DAY - timedelta(days=1), DAY + timedelta(days=1)])
            totals = collector.run(sleep=clock.sleep)
            self.assertEqual(totals["cycles"], 0)
            self.assertEqual(market.calls["tencent"], 0)

    def test_report_measures_coverage_gaps_and_latency(self):
        with TemporaryDirectory() as temp:
            root, market, clock = Path(temp), FakeMarket(), Clock(at(9, 30, 1))
            collector = make_collector(root, market, clock, stall_seconds=600, cross_check_seconds=10_000)
            while clock.now < at(9, 40):
                quote_time = clock.now - timedelta(seconds=1)
                if not (at(9, 35) <= quote_time < at(9, 35, 30)):
                    market.set_all(quote_time)
                collector.run_cycle()
                clock.sleep(3)
            report = build_report(collector.store, DAY, [STOCK, INDEX], calendar=collector.calendar, now=at(9, 40))
            stock = report["symbols"][STOCK]
            self.assertEqual(report["calendar_status"], "trading")
            self.assertGreater(stock["coverage"], 0.9)
            self.assertGreater(stock["max_gap_seconds"], 30)
            self.assertFalse(stock["checks"]["max_gap"])
            self.assertAlmostEqual(stock["latency_seconds"]["median"], 1.0)
            self.assertFalse(report["ok"])
            self.assertEqual(stock["by_source"], {"tencent": stock["ticks"]})

    def test_capture_writes_raw_text_and_description(self):
        with TemporaryDirectory() as temp:
            market = FakeMarket()
            market.set_all(at(10, 0, 3))
            market.sina_error = OSError("blocked")
            written = capture_samples(
                [STOCK, INDEX], Path(temp), "continuous",
                sources=[TencentSource(fetch_text=market.tencent), SinaSource(fetch_text=market.sina)], now=at(10, 0, 4),
            )
            self.assertEqual([item["source"] for item in written], ["tencent", "sina"])
            self.assertIsNone(written[0]["error"])
            self.assertIn("blocked", written[1]["error"])
            text = (Path(temp) / written[0]["file"]).read_text(encoding="utf-8")
            self.assertIn("v_sz002195", text)
            meta = json.loads((Path(temp) / f"{written[0]['file'][:-4]}.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["phase"], "continuous")


if __name__ == "__main__":
    unittest.main()
