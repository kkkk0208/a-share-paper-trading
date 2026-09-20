from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from paper_trading.ledger import PaperLedger
from paper_trading.broker import StaticQuoteBroker
from paper_trading.order_service import OrderService
from paper_trading.performance import calculate_performance
from paper_trading.snapshots import SnapshotCatchup
from tests.test_paper_trading import config, intent, quote


SHANGHAI = ZoneInfo("Asia/Shanghai")


class SnapshotSafetyTests(unittest.TestCase):
    def test_calendar_catchup_fills_missing_cash_only_days(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            calendar = root / "data" / "trading_calendar.csv"
            calendar.parent.mkdir(parents=True)
            calendar.write_text("date,close\n2026-01-02,4000\n2026-01-05,4050\n", encoding="utf-8")
            ledger = PaperLedger(root / "paper.sqlite3")
            cfg = config()
            created = datetime(2026, 1, 2, 9, 0, tzinfo=SHANGHAI)
            ledger.initialize_account(cfg, created)
            result = SnapshotCatchup(
                ledger, cfg.account_id, calendar, root / "history",
            ).catch_up(date(2026, 1, 5), datetime(2026, 1, 5, 16, 0, tzinfo=SHANGHAI))
            self.assertEqual(["2026-01-02", "2026-01-05"], result["filled"])
            self.assertEqual([], result["unresolved"])

    def test_performance_explicitly_reports_calendar_gap(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            calendar = root / "trading_calendar.csv"
            calendar.write_text(
                "date,close\n2026-01-02,4000\n2026-01-05,4050\n2026-01-06,4100\n",
                encoding="utf-8",
            )
            ledger = PaperLedger(root / "paper.sqlite3")
            cfg = config()
            ledger.initialize_account(cfg)
            with ledger.transaction() as con:
                con.executemany(
                    "INSERT INTO equity_curve(account_id,trade_date,equity,benchmark_close,created_at) VALUES(?,?,?,?,?)",
                    [
                        (cfg.account_id, "2026-01-02", "100000", "4000", "2026-01-02T16:00:00+08:00"),
                        (cfg.account_id, "2026-01-06", "101000", "4100", "2026-01-06T16:00:00+08:00"),
                    ],
                )
            result = calculate_performance(
                ledger, cfg.account_id, date(2026, 1, 2), date(2026, 1, 6), calendar,
            )
            self.assertEqual(1, result["snapshot_gap_count"])
            self.assertEqual(["2026-01-05"], result["snapshot_gap_dates"])
            self.assertTrue(result["metrics_incomplete"])

    def test_catchup_refuses_to_guess_state_before_later_fill(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            calendar = root / "trading_calendar.csv"
            calendar.write_text("date,close\n2026-01-02,4000\n2026-01-05,4050\n", encoding="utf-8")
            ledger = PaperLedger(root / "paper.sqlite3")
            cfg = config()
            created = datetime(2026, 1, 2, 9, 59, 30, tzinfo=SHANGHAI)
            first = quote(datetime(2026, 1, 5, 10, 0, tzinfo=SHANGHAI))
            ledger.initialize_account(cfg, created)
            service = OrderService(cfg, ledger, StaticQuoteBroker(first), root)
            service.propose(
                intent("later-fill", "BUY", first.timestamp - timedelta(seconds=30)),
                first, first.timestamp,
            )
            service.approve("later-fill", "tester", "checked", first.timestamp)
            service.broker.quote = quote(first.timestamp + timedelta(seconds=2))
            service.execute("later-fill", first.timestamp + timedelta(seconds=2))
            result = SnapshotCatchup(
                ledger, cfg.account_id, calendar, root / "history",
            ).catch_up(
                date(2026, 1, 5), datetime(2026, 1, 5, 16, 0, tzinfo=SHANGHAI),
                {"002195.SZ": first.last},
            )
            self.assertEqual("2026-01-02", result["unresolved"][0]["date"])
            self.assertIn("无法可靠倒推", result["unresolved"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
