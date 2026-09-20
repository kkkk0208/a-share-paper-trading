from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pandas as pd

from backtesting.config import BacktestConfig, ExecutionConfig, SplitConfig, StrategyConfig, load_backtest_config
from backtesting.cli import run as cli_run
from backtesting.data import CachedHistoryStore, normalize_history
from backtesting.engine import run_backtest
from backtesting.metrics import chronological_slices, segmented_metrics
from paper_trading.config import FeeConfig


def frame(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=len(closes), freq="B")
    opens = opens or closes
    return normalize_history(pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": [max(a, b) * 1.02 for a, b in zip(opens, closes)],
        "low": [min(a, b) * 0.98 for a, b in zip(opens, closes)],
        "close": closes,
        "volume": [1_000_000] * len(closes),
    }))


def make_config(days: int = 8) -> BacktestConfig:
    end = pd.date_range("2024-01-02", periods=days, freq="B")[-1].date()
    return BacktestConfig(
        symbols=("000001.SZ",),
        benchmark_symbol="000300.SH",
        start_date="2024-01-02",
        end_date=end,
        initial_cash="100000",
        price_adjustment="qfq",
        strategy=StrategyConfig("ma_cross_v1", 1, 2),
        execution=ExecutionConfig(100, Decimal("5"), Decimal("0.5"), Decimal("0.01")),
        fees=FeeConfig(
            Decimal("0.00025"), Decimal("5"), Decimal("0.0005"), "SELL",
            Decimal("0.00001"), "BOTH",
        ),
        splits=SplitConfig(Decimal("0.5"), Decimal("0.25"), Decimal("0.25")),
    )


class BacktestTests(unittest.TestCase):
    def test_signal_executes_at_next_open_without_lookahead(self) -> None:
        stock = frame([10, 12, 13, 8, 7, 9], [10, 11, 20, 13, 6, 8])
        result = run_backtest(make_config(6), {"000001.SZ": stock}, frame([100, 101, 102, 103, 104, 105]))
        buy = result.trades.iloc[0]
        self.assertEqual("BUY", buy["side"])
        self.assertEqual("2024-01-03", buy["signal_date"])
        self.assertEqual("2024-01-04", buy["execution_date"])
        self.assertEqual(20.0, buy["raw_open"])
        self.assertEqual(20.01, buy["fill_price"])
        self.assertEqual(0, buy["quantity"] % 100)

    def test_costs_and_realized_pnl_are_recorded(self) -> None:
        stock = frame([10, 12, 13, 8, 7, 9], [10, 11, 20, 13, 6, 8])
        result = run_backtest(make_config(6), {"000001.SZ": stock}, frame([100] * 6))
        buy, sell = result.trades.iloc[0], result.trades.iloc[1]
        self.assertGreaterEqual(buy["commission"], 5.0)
        self.assertEqual(0.0, buy["stamp_duty"])
        self.assertGreater(sell["stamp_duty"], 0.0)
        self.assertFalse(pd.isna(sell["realized_pnl"]))

    def test_chronological_segments_do_not_overlap(self) -> None:
        stock = frame([10, 12, 13, 8, 7, 9, 11, 10])
        test_config = make_config(8)
        result = run_backtest(test_config, {"000001.SZ": stock}, frame([100] * 8))
        pieces = chronological_slices(result.equity_curve, test_config.splits)
        self.assertLess(pieces["train"].index[-1], pieces["validation"].index[0])
        self.assertLess(pieces["validation"].index[-1], pieces["test"].index[0])
        metrics = segmented_metrics(result.equity_curve, result.trades, test_config.splits)
        self.assertEqual({"full", "train", "validation", "test"}, set(metrics))

    def test_cache_and_config_are_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CachedHistoryStore(root / "cache")
            source = frame([10, 11, 12])
            path = store.save("000001.SZ", source.reset_index())
            self.assertTrue(path.exists())
            pd.testing.assert_frame_equal(source, store.load("000001.SZ"), check_freq=False)
            payload = make_config(8).to_dict()
            config_path = root / "config.json"
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(make_config(8), load_backtest_config(config_path))

    def test_cli_run_writes_auditable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            output = root / "run"
            store = CachedHistoryStore(cache)
            store.save("000001.SZ", frame([10, 12, 13, 8, 7, 9, 11, 10]).reset_index())
            store.save("000300.SH", frame([100, 101, 102, 103, 104, 105, 106, 107]).reset_index())
            config_path = root / "config.json"
            config_path.write_text(json.dumps(make_config(8).to_dict()), encoding="utf-8")
            with redirect_stdout(StringIO()):
                exit_code = cli_run(Namespace(config=str(config_path), cache_dir=str(cache), output_dir=str(output)))
            self.assertEqual(0, exit_code)
            self.assertEqual(
                {"equity_curve.csv", "trades.csv", "summary.json", "run_manifest.json"},
                {path.name for path in output.iterdir()},
            )
            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("daily close", manifest["signal_timing"])
            self.assertEqual(64, len(manifest["source_sha256"]["000001.SZ"]))


if __name__ == "__main__":
    unittest.main()
