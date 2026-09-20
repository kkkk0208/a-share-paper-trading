from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .config import load_backtest_config
from .data import AkShareHistoryDownloader, CachedHistoryStore
from .engine import run_backtest
from .metrics import segmented_metrics


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _round_numbers(value: object) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_numbers(item) for item in value]
    return value


def download(args: argparse.Namespace) -> int:
    config = load_backtest_config(args.config)
    store = CachedHistoryStore(args.cache_dir)
    downloader = AkShareHistoryDownloader(store, Path("data") / "trading_calendar.csv")
    paper_close_store = CachedHistoryStore(Path("data") / "paper_closes")
    paper_close_downloader = AkShareHistoryDownloader(paper_close_store)
    path = downloader.download_benchmark(config.benchmark_symbol, config.start_date.isoformat(), config.end_date.isoformat())
    print(f"downloaded {config.benchmark_symbol}: {path}")
    for symbol in config.symbols:
        path = downloader.download_stock(symbol, config.start_date.isoformat(), config.end_date.isoformat(), config.price_adjustment)
        print(f"downloaded {symbol}: {path}")
        paper_path = paper_close_downloader.download_stock(
            symbol, config.start_date.isoformat(), config.end_date.isoformat(), "none",
        )
        print(f"downloaded unadjusted paper closes {symbol}: {paper_path}")
    return 0


def run(args: argparse.Namespace) -> int:
    config = load_backtest_config(args.config)
    store = CachedHistoryStore(args.cache_dir)
    histories = {symbol: store.load(symbol) for symbol in config.symbols}
    benchmark = store.load(config.benchmark_symbol)
    result = run_backtest(config, histories, benchmark)
    summary = segmented_metrics(
        result.equity_curve, result.trades, config.splits, config.risk_free_rate_annual,
        config.fees,
    )
    summary["strategy_configuration"] = {
        "position_sizing": "each new position targets max_single_weight of opening equity",
        "max_positions": min(
            len(config.symbols), math.floor(1 / float(config.execution.max_position_weight)),
        ),
        "max_single_weight": float(config.execution.max_position_weight),
    }
    warnings = list(result.warnings) + list(summary["warnings"])
    warnings.append("ma_cross_v1 参数固定且未在 train 上拟合；三段当前只代表不同市场阶段")
    if config.universe_definition == "manually_selected_demo_symbols":
        warnings.append("标的是手工选择的演示股票，结果存在选择偏差，不能外推到全市场")
    summary["warnings"] = warnings
    summary = _round_numbers(summary)

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    result.equity_curve.reset_index().to_csv(output / "equity_curve.csv", index=False, encoding="utf-8")
    result.trades.to_csv(output / "trades.csv", index=False, encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    source_hashes = {
        symbol: _sha256(store.path_for(symbol))
        for symbol in (*config.symbols, config.benchmark_symbol)
    }
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": config.to_dict(),
        "source_sha256": source_hashes,
        "source_downloads": store.download_metadata(),
        "signal_timing": "daily close",
        "execution_timing": "next available trading-day open",
        "research_status": "example strategy; not evidence of future performance",
        "parameter_selection": "none; ma_cross_v1 parameters were fixed before the run",
        "universe_definition": config.universe_definition,
    }
    (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"artifacts: {output.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="可复现的 A 股日线回测工具（示例策略）")
    parser.add_argument("--config", default="config/backtest.example.json")
    parser.add_argument("--cache-dir", default="runtime/backtesting/cache")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("download", help="下载并固化本次研究所用历史数据").set_defaults(handler=download)
    run_parser = subcommands.add_parser("run", help="仅使用本地固化数据执行回测")
    run_parser.add_argument("--output-dir", default="runtime/backtesting/runs/latest")
    run_parser.set_defaults(handler=run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except Exception as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
