from __future__ import annotations

from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def normalize_history(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume", "date": "date", "open": "open",
        "high": "high", "low": "low", "close": "close", "volume": "volume",
    }
    result = frame.rename(columns={column: aliases.get(str(column), str(column)) for column in frame.columns}).copy()
    missing = [column for column in REQUIRED_COLUMNS if column not in result.columns]
    if missing:
        raise ValueError(f"history data missing columns: {', '.join(missing)}")
    result = result.loc[:, REQUIRED_COLUMNS]
    result["date"] = pd.to_datetime(result["date"]).dt.normalize()
    for column in REQUIRED_COLUMNS[1:]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna().drop_duplicates("date", keep="last").sort_values("date").set_index("date")
    result = result[(result[["open", "high", "low", "close"]] > 0).all(axis=1) & (result["volume"] >= 0)]
    if result.empty:
        raise ValueError("history data is empty after normalization")
    return result


class CachedHistoryStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{symbol.replace('.', '_')}.csv"

    def load(self, symbol: str) -> pd.DataFrame:
        path = self.path_for(symbol)
        if not path.exists():
            raise FileNotFoundError(f"missing cached history for {symbol}: {path}; run backtest download first")
        return normalize_history(pd.read_csv(path))

    def save(self, symbol: str, frame: pd.DataFrame) -> Path:
        normalized = normalize_history(frame).reset_index()
        path = self.path_for(symbol)
        normalized.to_csv(path, index=False, encoding="utf-8")
        return path


class AkShareHistoryDownloader:
    def __init__(self, store: CachedHistoryStore) -> None:
        self.store = store

    def download_stock(self, symbol: str, start: str, end: str, adjustment: str) -> Path:
        import akshare as ak

        adjust = "" if adjustment == "none" else adjustment
        frame = ak.stock_zh_a_hist(
            symbol=symbol.split(".", 1)[0], period="daily",
            start_date=start.replace("-", ""), end_date=end.replace("-", ""), adjust=adjust,
        )
        return self.store.save(symbol, frame)

    def download_benchmark(self, symbol: str, start: str, end: str) -> Path:
        import akshare as ak

        prefix = symbol.split(".", 1)[1].lower() + symbol.split(".", 1)[0]
        try:
            frame = ak.stock_zh_index_daily_em(symbol=prefix, start_date=start.replace("-", ""), end_date=end.replace("-", ""))
        except Exception:
            frame = ak.stock_zh_index_daily_tx(symbol=prefix)
        # Some index endpoints expose only turnover amount, while the engine needs
        # benchmark close prices only. Keep the common cache schema with a neutral
        # volume value instead of confusing turnover amount with share volume.
        translated_columns = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close"}
        frame = frame.rename(columns=translated_columns)
        if "volume" not in frame.columns and "成交量" not in frame.columns:
            frame["volume"] = 0
        normalized = normalize_history(frame)
        normalized = normalized.loc[pd.Timestamp(start):pd.Timestamp(end)]
        return self.store.save(symbol, normalized.reset_index())
