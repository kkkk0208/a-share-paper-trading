from __future__ import annotations

import json
from datetime import datetime, timezone
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

    def record_download(self, symbol: str, path: Path) -> None:
        metadata_path = self.root / "download_manifest.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        metadata[symbol] = {
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "file": path.name,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    def download_metadata(self) -> dict[str, object]:
        path = self.root / "download_manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class AkShareHistoryDownloader:
    def __init__(self, store: CachedHistoryStore, calendar_path: str | Path | None = None) -> None:
        self.store = store
        self.calendar_path = Path(calendar_path) if calendar_path else None

    def download_stock(self, symbol: str, start: str, end: str, adjustment: str) -> Path:
        import akshare as ak

        adjust = "" if adjustment == "none" else adjustment
        code, exchange = symbol.split(".", 1)
        try:
            frame = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start.replace("-", ""), end_date=end.replace("-", ""), adjust=adjust,
            )
        except Exception:
            frame = ak.stock_zh_a_daily(
                symbol=f"{exchange.lower()}{code}",
                start_date=start.replace("-", ""), end_date=end.replace("-", ""), adjust=adjust,
            )
        path = self.store.save(symbol, frame)
        self.store.record_download(symbol, path)
        return path

    def download_benchmark(self, symbol: str, start: str, end: str) -> Path:
        import akshare as ak

        code, exchange = symbol.split(".", 1)
        if code.startswith(("H", "N")):
            raw = ak.stock_zh_index_hist_csindex(
                symbol=code, start_date=start.replace("-", ""), end_date=end.replace("-", ""),
            )
            close = pd.to_numeric(raw.iloc[:, 9], errors="coerce")
            frame = pd.DataFrame({
                "date": raw.iloc[:, 0], "open": close, "high": close,
                "low": close, "close": close, "volume": 0,
            })
        else:
            prefix = exchange.lower() + code
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
        normalized = normalized[normalized.index.dayofweek < 5]
        path = self.store.save(symbol, normalized.reset_index())
        self.store.record_download(symbol, path)
        if self.calendar_path:
            self.calendar_path.parent.mkdir(parents=True, exist_ok=True)
            normalized.reset_index().loc[:, ["date", "close"]].to_csv(
                self.calendar_path, index=False, encoding="utf-8",
            )
        return path
