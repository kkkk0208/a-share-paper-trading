"""Real-time quote collector. It records quotes and never places orders.

    python -m paper_trading.collector run            # trading hours, exits after the close
    python -m paper_trading.collector once           # one cycle right now, at any time
    python -m paper_trading.collector report --date 2026-10-15
    python -m paper_trading.collector capture --symbols 002195.SZ 000300.SH --label continuous
    python -m paper_trading.collector sync-calendar

Each cycle sends one batched request to the primary source (Tencent) and asks the
backup (Sina) only for symbols the primary did not return. Every snapshot is
checked. Accepted ticks go to market_snapshots, everything else to quote_events.
Nothing is estimated, back-filled or replaced with an older value.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time as _time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from .exchange_calendar import ExchangeCalendar, sync_trade_dates
from .models import MarketQuote
from .quote_quality import (
    CONTINUOUS_SEGMENTS, SHANGHAI, Flag, Phase, PreviousTick, assess_quote, session_phase,
)
from .quote_sources import BatchQuoteSource, ParsedQuote, SinaSource, SourceBatch, TencentSource
from .quote_store import QuoteStore, tick_snapshot_id
from .symbols import split_symbol


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "paper_trading.json"
DEFAULT_DB = PROJECT_ROOT / "runtime" / "paper_trading" / "paper_trading.sqlite3"
DEFAULT_CALENDAR = PROJECT_ROOT / "data" / "trade_dates.csv"
DEFAULT_CAPTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "quotes" / "real"

COLLECT_PHASES = frozenset({Phase.OPENING_CALL, Phase.PRE_OPEN_WAIT, Phase.CONTINUOUS, Phase.CLOSING_CALL})
MARKET_STATE_FLAGS = frozenset({Flag.LIMIT_UP_SEALED, Flag.LIMIT_DOWN_SEALED, Flag.NO_BOOK})
SOURCE_ERROR_REPEAT_SECONDS = 60


def local_now() -> datetime:
    return datetime.now().astimezone()


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class CollectorSettings:
    symbols: tuple[str, ...]
    interval_seconds: float = 3.0
    idle_seconds: float = 30.0
    cross_check_seconds: float = 300.0
    stall_seconds: float = 60.0
    max_age_seconds: float = 30.0
    after_close_minutes: int = 5
    mismatch_bps: Decimal = Decimal("30")
    cross_check_max_skew_seconds: float = 10.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(dict.fromkeys(symbol.strip().upper() for symbol in self.symbols)))
        if not self.symbols:
            raise ValueError("collector needs at least one symbol")
        for symbol in self.symbols:
            split_symbol(symbol)
        if self.interval_seconds <= 0 or self.idle_seconds <= 0:
            raise ValueError("intervals must be positive")
        if not 0 <= self.after_close_minutes < 60:
            raise ValueError("after_close_minutes must be within the 15:00 hour")


@dataclass
class CycleReport:
    at: datetime
    window: str
    collected: bool
    stored: int = 0
    unchanged: int = 0
    rejected: int = 0
    missing: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"at": self.at.isoformat()}


class QuoteCollector:
    def __init__(
        self,
        store: QuoteStore,
        primary: BatchQuoteSource,
        secondary: BatchQuoteSource | None,
        calendar: ExchangeCalendar | None,
        settings: CollectorSettings,
        *,
        clock: Callable[[], datetime] = local_now,
        dry_run: bool = False,
    ) -> None:
        self.store = store
        self.primary = primary
        self.secondary = secondary
        self.calendar = calendar
        self.settings = settings
        self.clock = clock
        self.dry_run = dry_run
        self.active_source = primary.name
        self.recent_events: list[dict[str, Any]] = []
        self.totals = {"cycles": 0, "stored": 0, "unchanged": 0, "rejected": 0, "source_errors": 0}
        self._started_at = clock()
        self._previous: dict[str, PreviousTick] = {}
        self._latest: dict[str, MarketQuote] = {}
        self._event_keys: dict[tuple[str, str], Any] = {}
        self._failing_sources: set[str] = set()
        self._stalled: set[str] = set()
        self._first_tick_day: dict[str, date] = {}
        self._limits_memo: dict[tuple[str, date], tuple[Decimal, Decimal] | None] = {}
        self._last_cross_check: datetime | None = None
        self._calendar_warned: date | None = None
        for symbol, quote in store.latest_ticks(settings.symbols).items():
            self._previous[symbol] = PreviousTick(quote.timestamp, quote.last)

    # ----- schedule -------------------------------------------------------------------

    def _day_status(self, day: date) -> str:
        if self.calendar is None:
            return "missing"
        try:
            return self.calendar.status(day)
        except (OSError, ValueError):
            return "unreadable"

    def window(self, now: datetime) -> tuple[bool, str]:
        local = now.astimezone(SHANGHAI)
        phase = session_phase(local)
        if phase == Phase.CLOSED:
            return False, "weekend"
        day_status = self._day_status(local.date())
        if day_status == "closed":
            return False, "holiday"
        if day_status != "trading" and self._calendar_warned != local.date():
            self._calendar_warned = local.date()
            self._event("CALENDAR_UNKNOWN", "warning", now=now, detail={
                "status": day_status,
                "note": "交易日历不能确认今天是否开市，按工作日照常采集；交易风控仍然 fail closed",
            })
        if phase in COLLECT_PHASES:
            return True, phase.value
        if phase == Phase.AFTER_CLOSE and local.time() < time(15, self.settings.after_close_minutes):
            return True, "after_close_window"
        return False, phase.value

    def finished_for_today(self, now: datetime) -> bool:
        active, label = self.window(now)
        if label in {"weekend", "holiday"}:
            return True
        return session_phase(now) == Phase.AFTER_CLOSE and not active

    def idle_delay(self, now: datetime) -> float:
        local = now.astimezone(SHANGHAI)
        for boundary in (time(9, 15), time(13, 0)):
            target = datetime.combine(local.date(), boundary, tzinfo=SHANGHAI)
            if target > local:
                return max(1.0, min(self.settings.idle_seconds, (target - local).total_seconds()))
        return self.settings.idle_seconds

    # ----- one cycle --------------------------------------------------------------------

    def run_cycle(self, force: bool = False) -> CycleReport:
        now = self.clock()
        active, label = self.window(now)
        if force and not active:
            active, label = True, f"forced:{label}"
        report = CycleReport(at=now, window=label, collected=active)
        if not active:
            return report
        self.totals["cycles"] += 1

        symbols = list(self.settings.symbols)
        received: dict[str, ParsedQuote] = {}
        primary_batch = self._fetch(self.primary, symbols, now, report)
        if primary_batch is not None:
            received.update(primary_batch.quotes)
        missing = [symbol for symbol in symbols if symbol not in received]
        if missing and self.secondary is not None:
            secondary_batch = self._fetch(self.secondary, missing, now, report)
            if secondary_batch is not None:
                for symbol, parsed in secondary_batch.quotes.items():
                    received.setdefault(symbol, parsed)
        self.active_source = self.primary.name if primary_batch is not None else (
            self.secondary.name if self.secondary is not None else "none"
        )
        report.missing = [symbol for symbol in symbols if symbol not in received]
        for symbol in symbols:
            if symbol in received:
                self._ingest(received[symbol], now, report)

        if session_phase(now) == Phase.CONTINUOUS:
            self._check_stalls(now)
            if primary_batch is not None and self.secondary is not None and self._cross_check_due(now):
                self._cross_check(primary_batch, now)

        for key in ("stored", "unchanged", "rejected"):
            self.totals[key] += getattr(report, key)
        self._write_status(now, report)
        return report

    def _fetch(
        self, source: BatchQuoteSource, symbols: Sequence[str], now: datetime, report: CycleReport,
    ) -> SourceBatch | None:
        try:
            batch = source.fetch(symbols, now)
        except Exception as exc:
            self.totals["source_errors"] += 1
            report.errors.append(f"{source.name}: {exc}")
            self._failing_sources.add(source.name)
            bucket = int(now.timestamp() // SOURCE_ERROR_REPEAT_SECONDS)
            self._event_once(source.name, (str(exc), bucket), "SOURCE_ERROR", "warning", now=now,
                             source=source.name, detail={"error": str(exc), "symbols": list(symbols)})
            return None
        if source.name in self._failing_sources:
            self._failing_sources.discard(source.name)
            self._event("SOURCE_RECOVERED", "info", now=now, source=source.name)
        for symbol, reason in batch.errors.items():
            self._event_once(f"{symbol}@{source.name}", reason, "SYMBOL_UNAVAILABLE", "warning", now=now,
                             symbol=symbol, source=source.name, detail={"reason": reason})
        return batch

    def _limits_for(self, symbol: str, trade_date: date) -> tuple[Decimal, Decimal] | None:
        key = (symbol, trade_date)
        if key not in self._limits_memo or self._limits_memo[key] is None:
            self._limits_memo[key] = self.store.limits.get(symbol, trade_date)
        return self._limits_memo[key]

    def _apply_limits(self, parsed: ParsedQuote, now: datetime) -> tuple[ParsedQuote, bool]:
        if parsed.index:
            return parsed, False
        trade_date = parsed.timestamp.astimezone(SHANGHAI).date()
        if parsed.limit_up is not None and parsed.limit_down is not None:
            if session_phase(parsed.timestamp) not in {Phase.CLOSED, Phase.PRE_MARKET} and not self.dry_run:
                existing = self._limits_for(parsed.symbol, trade_date)
                current = (parsed.limit_up, parsed.limit_down)
                if existing is None:
                    self.store.limits.put(parsed.symbol, trade_date, *current, parsed.source, now)
                    self._limits_memo[(parsed.symbol, trade_date)] = current
                elif existing != current:
                    self._event_once(parsed.symbol, current, "LIMIT_CHANGED", "warning", now=now,
                                     symbol=parsed.symbol, source=parsed.source, quote_timestamp=parsed.timestamp,
                                     detail={"stored": [str(v) for v in existing], "reported": [str(v) for v in current]})
            return parsed, False
        cached = self._limits_for(parsed.symbol, trade_date)
        if cached is None:
            return parsed, False
        return parsed.with_limits(cached[0], cached[1], "cached_limits"), True

    def _ingest(self, parsed: ParsedQuote, now: datetime, report: CycleReport) -> None:
        symbol = parsed.symbol
        parsed, cached_limits = self._apply_limits(parsed, now)
        quote = parsed.to_market_quote()
        quote = replace(quote, snapshot_id=tick_snapshot_id(quote, parsed.source))
        previous = self._previous.get(symbol)
        if previous is not None and quote.timestamp == previous.timestamp:
            report.unchanged += 1
            return

        today = now.astimezone(SHANGHAI).date()
        assessment = assess_quote(
            quote, now=now, previous=previous, expected_date=today, index=parsed.index,
            max_age_seconds=self.settings.max_age_seconds,
        )
        flags = [*assessment.flags, *([Flag.CACHED_LIMITS] if cached_limits else [])]
        flag_values = [flag.value for flag in flags]
        if assessment.blocking:
            report.rejected += 1
            self._event_once(symbol, (tuple(flag_values), quote.timestamp.isoformat()), "SNAPSHOT_REJECTED",
                             "blocking", now=now, symbol=symbol, source=parsed.source,
                             quote_timestamp=quote.timestamp, detail={
                                 "flags": flag_values, "last": str(quote.last), "bid": str(quote.bid),
                                 "ask": str(quote.ask), "raw": parsed.raw,
                             })
            return

        extra: dict[str, Any] = {
            "source": parsed.source, "phase": assessment.phase.value, "flags": flag_values,
            "status": parsed.status, "prev_close": _text(parsed.prev_close), "open": _text(parsed.open),
            "high": _text(parsed.high), "low": _text(parsed.low), "volume_shares": parsed.volume_shares,
            "amount_yuan": _text(parsed.amount_yuan),
        }
        warnings = sorted(flag.value for flag in assessment.warnings)
        if self._first_tick_day.get(symbol) != today or warnings:
            extra["raw"] = parsed.raw
        if not self.dry_run:
            self.store.save_tick(quote, extra, now)
        self._first_tick_day[symbol] = today
        self._previous[symbol] = PreviousTick(quote.timestamp, quote.last)
        self._latest[symbol] = quote
        report.stored += 1
        report.sources[parsed.source] = report.sources.get(parsed.source, 0) + 1

        if symbol in self._stalled:
            self._stalled.discard(symbol)
            self._event("FEED_RESUMED", "info", now=now, symbol=symbol, source=parsed.source,
                        quote_timestamp=quote.timestamp)
        self._track_flags(symbol, "QUALITY", tuple(warnings), parsed, quote, now, "QUALITY_WARNING",
                          "QUALITY_RECOVERED", "warning")
        states = tuple(sorted(flag.value for flag in assessment.flags if flag in MARKET_STATE_FLAGS))
        self._track_flags(symbol, "MARKET_STATE", states, parsed, quote, now, "MARKET_STATE",
                          "MARKET_STATE", "info")

    def _track_flags(
        self, symbol: str, slot_name: str, current: tuple[str, ...], parsed: ParsedQuote, quote: MarketQuote,
        now: datetime, set_event: str, clear_event: str, set_severity: str,
    ) -> None:
        slot = (symbol, slot_name)
        before = self._event_keys.get(slot, ())
        if current == before:
            return
        self._event_keys[slot] = current
        if current:
            self._event(set_event, set_severity, now=now, symbol=symbol, source=parsed.source,
                        quote_timestamp=quote.timestamp, detail={"flags": list(current), "raw": parsed.raw})
        else:
            self._event(clear_event, "info", now=now, symbol=symbol, source=parsed.source,
                        quote_timestamp=quote.timestamp, detail={"flags": [], "previous_flags": list(before)})

    def _check_stalls(self, now: datetime) -> None:
        local = now.astimezone(SHANGHAI)
        segment_start = next((
            datetime.combine(local.date(), start, tzinfo=SHANGHAI)
            for start, end in CONTINUOUS_SEGMENTS if start <= local.time().replace(tzinfo=None) < end
        ), None)
        if segment_start is None:
            return
        for symbol in self.settings.symbols:
            previous = self._previous.get(symbol)
            reference = max(segment_start, self._started_at.astimezone(SHANGHAI))
            if previous is not None:
                reference = max(reference, previous.timestamp)
            gap = (now - reference).total_seconds()
            if gap > self.settings.stall_seconds and symbol not in self._stalled:
                self._stalled.add(symbol)
                self._event("FEED_STALLED", "warning", now=now, symbol=symbol, detail={
                    "seconds_without_new_tick": round(gap, 1),
                    "last_tick": previous.timestamp.isoformat() if previous else None,
                })

    def _cross_check_due(self, now: datetime) -> bool:
        return (
            self._last_cross_check is None
            or (now - self._last_cross_check).total_seconds() >= self.settings.cross_check_seconds
        )

    def _cross_check(self, primary_batch: SourceBatch, now: datetime) -> None:
        assert self.secondary is not None
        self._last_cross_check = now
        symbols = list(primary_batch.quotes)
        try:
            other = self.secondary.fetch(symbols, now)
        except Exception as exc:
            self._event("CROSS_CHECK_FAILED", "warning", now=now, source=self.secondary.name,
                        detail={"error": str(exc)})
            return
        for symbol, mine in primary_batch.quotes.items():
            theirs = other.quotes.get(symbol)
            if theirs is None:
                continue
            skew = abs((mine.timestamp - theirs.timestamp).total_seconds())
            diff_bps = (abs(theirs.last - mine.last) / mine.last * 10000).quantize(Decimal("0.1"))
            comparable = skew <= self.settings.cross_check_max_skew_seconds
            mismatch = comparable and diff_bps > self.settings.mismatch_bps
            self._event(
                "SOURCE_MISMATCH" if mismatch else "CROSS_CHECK", "warning" if mismatch else "info",
                now=now, symbol=symbol, source=f"{mine.source}|{theirs.source}", quote_timestamp=mine.timestamp,
                detail={
                    "primary_last": str(mine.last), "secondary_last": str(theirs.last), "diff_bps": str(diff_bps),
                    "timestamp_skew_seconds": skew, "comparable": comparable,
                    "primary_timestamp": mine.timestamp.isoformat(), "secondary_timestamp": theirs.timestamp.isoformat(),
                },
            )

    # ----- events and status ------------------------------------------------------------

    def _event(self, event_type: str, severity: str, *, now: datetime, **kwargs: Any) -> None:
        record = {"event_type": event_type, "severity": severity, "at": now.isoformat(),
                  **{key: value for key, value in kwargs.items() if key != "quote_timestamp"}}
        self.recent_events = (self.recent_events + [record])[-50:]
        if not self.dry_run:
            self.store.record_event(event_type, severity, now=now, **kwargs)

    def _event_once(self, scope: str, key: Any, event_type: str, severity: str, *, now: datetime, **kwargs: Any) -> None:
        slot = (scope, event_type)
        if self._event_keys.get(slot) == key:
            return
        self._event_keys[slot] = key
        self._event(event_type, severity, now=now, **kwargs)

    def _write_status(self, now: datetime, report: CycleReport) -> None:
        if self.dry_run:
            return
        self.store.update_status(now, {
            "pid": os.getpid(), "started_at": self._started_at.isoformat(), "active_source": self.active_source,
            "symbols": list(self.settings.symbols), "interval_seconds": self.settings.interval_seconds,
            "totals": self.totals, "last_cycle": report.to_dict(),
        })

    # ----- loop -------------------------------------------------------------------------

    def run(
        self, *, max_cycles: int | None = None, sleep: Callable[[float], None] = _time.sleep,
        keep_running: bool = False, on_cycle: Callable[[CycleReport], None] | None = None,
    ) -> dict[str, int]:
        cycles = 0
        while True:
            started = _time.monotonic()
            report = self.run_cycle()
            cycles += 1
            if on_cycle is not None:
                on_cycle(report)
            if max_cycles is not None and cycles >= max_cycles:
                break
            if not keep_running and self.finished_for_today(report.at):
                break
            delay = self.settings.interval_seconds if report.collected else self.idle_delay(report.at)
            sleep(max(0.0, delay - (_time.monotonic() - started)))
        return dict(self.totals)


# ----- data quality report --------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "min_coverage": 0.90,           # share of interval buckets in continuous trading with a new tick
    "max_gap_seconds": 15.0,        # longest wait for a new tick inside continuous trading
    "max_abs_latency_seconds": 3.0, # median of (stored at - quote time); a large value means clock drift
}


def _segment_stats(timestamps: list[datetime], day: date, interval: float, now: datetime | None) -> dict[str, Any]:
    covered = expected = 0
    gaps: list[float] = []
    for start_time, end_time in CONTINUOUS_SEGMENTS:
        start = datetime.combine(day, start_time, tzinfo=SHANGHAI)
        end = datetime.combine(day, end_time, tzinfo=SHANGHAI)
        if now is not None:
            end = min(end, now.astimezone(SHANGHAI))
        if end <= start:
            continue
        inside = sorted(ts for ts in timestamps if start <= ts < end)
        expected += math.ceil((end - start).total_seconds() / interval)
        covered += len({int((ts - start).total_seconds() // interval) for ts in inside})
        points = [start, *inside, end]
        gaps.extend((later - earlier).total_seconds() for earlier, later in zip(points, points[1:]))
    return {
        "expected_buckets": expected, "covered_buckets": covered,
        "coverage": round(covered / expected, 4) if expected else None,
        "max_gap_seconds": max(gaps) if gaps else None,
    }


def build_report(
    store: QuoteStore, day: date, symbols: Sequence[str], *, calendar: ExchangeCalendar | None = None,
    interval_seconds: float = 3.0, now: datetime | None = None, thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    limits = DEFAULT_THRESHOLDS | (thresholds or {})
    events = store.events_for_day(day)
    current = now if now is not None and now.astimezone(SHANGHAI).date() == day else None
    result: dict[str, Any] = {
        "date": day.isoformat(),
        "calendar_status": calendar.status(day) if calendar is not None and calendar.path.exists() else "missing",
        "thresholds": limits,
        "events_by_type": dict(Counter(event["event_type"] for event in events)),
        "collector_status": store.status(),
        "symbols": {},
    }
    for symbol in symbols:
        ticks = store.ticks_for_day(symbol, day)
        stamps = [datetime.fromisoformat(tick["timestamp"]) for tick in ticks]
        meta = [tick["payload"].get("collector") or {} for tick in ticks]
        latencies = [
            (datetime.fromisoformat(tick["created_at"]) - stamp).total_seconds() for tick, stamp in zip(ticks, stamps)
        ]
        flag_counts = Counter(flag for item in meta for flag in item.get("flags", []))
        mine = [event for event in events if event.get("symbol") == symbol]
        comparisons = [event for event in mine if event["event_type"] in {"CROSS_CHECK", "SOURCE_MISMATCH"}]
        comparable = [Decimal(event["detail"]["diff_bps"]) for event in comparisons if event["detail"].get("comparable")]
        segments = _segment_stats(stamps, day, interval_seconds, current)
        median_latency = statistics.median(latencies) if latencies else None
        stats = {
            "ticks": len(ticks),
            "first_tick": ticks[0]["timestamp"] if ticks else None,
            "last_tick": ticks[-1]["timestamp"] if ticks else None,
            "by_phase": dict(Counter(item.get("phase") or session_phase(stamp).value for item, stamp in zip(meta, stamps))),
            "by_source": dict(Counter(item.get("source") or tick["payload"].get("source") for item, tick in zip(meta, ticks))),
            "flags": dict(flag_counts),
            **segments,
            "latency_seconds": None if not latencies else {
                "min": min(latencies), "median": median_latency, "max": max(latencies),
            },
            "rejected_snapshots": sum(1 for event in mine if event["event_type"] == "SNAPSHOT_REJECTED"),
            "feed_stalls": sum(1 for event in mine if event["event_type"] == "FEED_STALLED"),
            "cross_checks": len(comparisons),
            "cross_check_max_diff_bps": str(max(comparable)) if comparable else None,
            "source_mismatches": sum(1 for event in comparisons if event["event_type"] == "SOURCE_MISMATCH"),
        }
        checks = {
            "coverage": segments["coverage"] is not None and segments["coverage"] >= limits["min_coverage"],
            "max_gap": segments["max_gap_seconds"] is not None and segments["max_gap_seconds"] <= limits["max_gap_seconds"],
            "latency": median_latency is not None and abs(median_latency) <= limits["max_abs_latency_seconds"],
            "no_source_mismatch": stats["source_mismatches"] == 0,
            "no_feed_stall": stats["feed_stalls"] == 0,
        }
        result["symbols"][symbol] = stats | {"checks": checks, "ok": all(checks.values())}
    result["ok"] = bool(result["symbols"]) and all(item["ok"] for item in result["symbols"].values())
    return result


# ----- raw samples for tests --------------------------------------------------------------

def capture_samples(
    symbols: Sequence[str], out_dir: str | Path, label: str = "",
    sources: Sequence[BatchQuoteSource] | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Save raw responses (as UTF-8) plus a small JSON description, for parser tests."""
    for symbol in symbols:
        split_symbol(symbol)
    captured_at = now or local_now()
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", label).strip("-") or "sample"
    written: list[dict[str, Any]] = []
    for source in sources or (TencentSource(), SinaSource()):
        stem = f"{captured_at:%Y%m%d_%H%M%S}_{slug}_{source.name}"
        try:
            text, error = source.fetch_text(symbols), None
        except Exception as exc:
            text, error = "", str(exc)
        if error is None:
            (target / f"{stem}.txt").write_text(text, encoding="utf-8")
        meta = {
            "source": source.name, "url": source.request_url(symbols), "symbols": list(symbols),
            "captured_at": captured_at.isoformat(), "phase": session_phase(captured_at).value, "label": label,
            "file": f"{stem}.txt" if error is None else None, "error": error,
        }
        (target / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(meta)
    return written


# ----- command line -----------------------------------------------------------------------

def _symbols(args: argparse.Namespace) -> tuple[str, ...]:
    symbols = list(getattr(args, "symbol", None) or [])
    if not symbols:
        from .config import load_config

        config_path = Path(args.config)
        if not config_path.exists():
            raise SystemExit(
                f"找不到 {config_path}；请先创建模拟账户配置，或用 --symbol 002195.SZ --symbol 000300.SH 指定标的"
            )
        config = load_config(config_path)
        symbols = [*config.whitelist, config.benchmark_symbol]
    return tuple(dict.fromkeys([*symbols, *(getattr(args, "extra_symbol", None) or [])]))


def _collector(args: argparse.Namespace, dry_run: bool = False) -> QuoteCollector:
    settings = CollectorSettings(
        symbols=_symbols(args),
        interval_seconds=getattr(args, "interval", 3.0),
        cross_check_seconds=getattr(args, "cross_check_seconds", 300.0),
    )
    return QuoteCollector(
        QuoteStore(args.db), TencentSource(), SinaSource(), ExchangeCalendar(args.calendar), settings, dry_run=dry_run,
    )


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A 股实时行情采集（只记录行情，不下单）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--calendar", default=str(DEFAULT_CALENDAR))
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="交易时段持续采集，收盘后自动退出")
    once = commands.add_parser("once", help="马上采集一轮（不看时段），用于检查网络和解析")
    for sub in (run, once):
        sub.add_argument("--symbol", action="append", help="直接指定标的；不填则读取配置里的白名单和基准")
        sub.add_argument("--extra-symbol", action="append", default=[], help="在配置之外额外采集的标的")
    run.add_argument("--interval", type=float, default=3.0)
    run.add_argument("--cross-check-seconds", type=float, default=300.0)
    run.add_argument("--max-cycles", type=int)
    run.add_argument("--keep-running", action="store_true", help="收盘后不退出（一般用计划任务每天启动即可）")
    once.add_argument("--dry-run", action="store_true", help="只显示结果，不写数据库")

    report = commands.add_parser("report", help="输出某天的数据质量报告；全部通过时退出码为 0，否则为 2")
    report.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    report.add_argument("--symbol", action="append")
    report.add_argument("--interval", type=float, default=3.0)
    report.add_argument("--out", help="同时把报告写入这个 JSON 文件（UTF-8）")

    capture = commands.add_parser("capture", help="保存腾讯、新浪原始返回，作为解析测试样本")
    capture.add_argument("--symbols", nargs="+", required=True)
    capture.add_argument("--label", default="", help="例如 limit_up、suspended、opening_call")
    capture.add_argument("--out", default=str(DEFAULT_CAPTURE_DIR))

    commands.add_parser("sync-calendar", help="通过 AKShare 下载交易日历（含当年剩余交易日）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sync-calendar":
        try:
            _print(sync_trade_dates(args.calendar))
        except ImportError:
            print("需要先安装 akshare：pip install -r requirements-backtesting.txt", file=sys.stderr)
            return 1
        return 0
    if args.command == "capture":
        _print(capture_samples(args.symbols, args.out, args.label))
        return 0
    if args.command == "report":
        day = date.fromisoformat(args.date) if args.date else local_now().astimezone(SHANGHAI).date()
        result = build_report(
            QuoteStore(args.db), day, _symbols(args), calendar=ExchangeCalendar(args.calendar),
            interval_seconds=args.interval, now=local_now(),
        )
        _print(result)
        if args.out:
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return 0 if result["ok"] else 2
    if args.command == "once":
        collector = _collector(args, dry_run=args.dry_run)
        report = collector.run_cycle(force=True)
        _print({"cycle": report.to_dict(), "events": collector.recent_events, "dry_run": args.dry_run})
        return 0

    collector = _collector(args)
    print(f"collector started: {', '.join(collector.settings.symbols)}; db={args.db}", flush=True)

    def log(report: CycleReport) -> None:
        if report.errors or report.rejected or report.missing:
            print(json.dumps(report.to_dict(), ensure_ascii=False, default=str), flush=True)
        elif report.collected and collector.totals["cycles"] % 100 == 0:
            print(f"{report.at:%H:%M:%S} ok: {collector.totals}", flush=True)

    try:
        totals = collector.run(max_cycles=args.max_cycles, keep_running=args.keep_running, on_cycle=log)
    except KeyboardInterrupt:
        totals = dict(collector.totals)
    _print({"status": "STOPPED", "totals": totals})
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
