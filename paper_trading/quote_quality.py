"""Trading-session phases and snapshot quality checks.

The checks only label a snapshot. They never repair or estimate data: a snapshot
with a blocking flag is kept out of market_snapshots and written to quote_events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from .models import MarketQuote


SHANGHAI = ZoneInfo("Asia/Shanghai")


class Phase(StrEnum):
    CLOSED = "closed"                # weekend
    PRE_MARKET = "pre_market"        # before 09:15
    OPENING_CALL = "opening_call"    # 09:15-09:25 opening call auction
    PRE_OPEN_WAIT = "pre_open_wait"  # 09:25-09:30 orders accepted, nothing matched
    CONTINUOUS = "continuous"        # 09:30-11:30 and 13:00-14:57
    LUNCH_BREAK = "lunch_break"      # 11:30-13:00
    CLOSING_CALL = "closing_call"    # 14:57-15:00 closing call auction
    AFTER_CLOSE = "after_close"      # from 15:00


CONTINUOUS_SEGMENTS = ((time(9, 30), time(11, 30)), (time(13, 0), time(14, 57)))


def session_phase(timestamp: datetime) -> Phase:
    """Phase by clock time in Shanghai. Holidays are decided by the exchange calendar."""
    local = timestamp.astimezone(SHANGHAI)
    if local.weekday() >= 5:
        return Phase.CLOSED
    current = local.time().replace(tzinfo=None)
    if current < time(9, 15):
        return Phase.PRE_MARKET
    if current < time(9, 25):
        return Phase.OPENING_CALL
    if current < time(9, 30):
        return Phase.PRE_OPEN_WAIT
    if current < time(11, 30):
        return Phase.CONTINUOUS
    if current < time(13, 0):
        return Phase.LUNCH_BREAK
    if current < time(14, 57):
        return Phase.CONTINUOUS
    if current < time(15, 0):
        return Phase.CLOSING_CALL
    return Phase.AFTER_CLOSE


class Flag(StrEnum):
    # Blocking: the snapshot is not stored as a tick.
    NOT_TODAY = "not_today"
    NON_MONOTONIC = "non_monotonic"
    CROSSED_BOOK = "crossed_book"
    PRICE_OUTSIDE_LIMITS = "price_outside_limits"
    # Warning: stored, but worth a look.
    STALE = "stale"
    CLOCK_SKEW = "clock_skew"
    LOCKED_BOOK = "locked_book"
    ONE_SIDED_BOOK = "one_sided_book"
    MISSING_LIMITS = "missing_limits"
    LARGE_JUMP = "large_jump"
    # Info: real market states that matter for trading.
    LIMIT_UP_SEALED = "limit_up_sealed"
    LIMIT_DOWN_SEALED = "limit_down_sealed"
    NO_BOOK = "no_book"
    CACHED_LIMITS = "cached_limits"


BLOCKING_FLAGS = frozenset({Flag.NOT_TODAY, Flag.NON_MONOTONIC, Flag.CROSSED_BOOK, Flag.PRICE_OUTSIDE_LIMITS})
WARNING_FLAGS = frozenset({
    Flag.STALE, Flag.CLOCK_SKEW, Flag.LOCKED_BOOK, Flag.ONE_SIDED_BOOK, Flag.MISSING_LIMITS, Flag.LARGE_JUMP,
})


def severity(flag: Flag) -> str:
    if flag in BLOCKING_FLAGS:
        return "blocking"
    if flag in WARNING_FLAGS:
        return "warning"
    return "info"


@dataclass(frozen=True)
class PreviousTick:
    timestamp: datetime
    last: Decimal


@dataclass(frozen=True)
class Assessment:
    phase: Phase
    flags: tuple[Flag, ...]

    @property
    def blocking(self) -> bool:
        return any(flag in BLOCKING_FLAGS for flag in self.flags)

    @property
    def warnings(self) -> tuple[Flag, ...]:
        return tuple(flag for flag in self.flags if flag in WARNING_FLAGS)


def assess_quote(
    quote: MarketQuote,
    *,
    now: datetime,
    previous: PreviousTick | None = None,
    expected_date: date | None = None,
    index: bool = False,
    max_age_seconds: float = 30,
    max_future_seconds: float = 3,
    jump_ratio: Decimal = Decimal("0.05"),
) -> Assessment:
    flags: list[Flag] = []
    phase = session_phase(quote.timestamp)
    quote_date = quote.timestamp.astimezone(SHANGHAI).date()

    if expected_date is not None and quote_date != expected_date:
        flags.append(Flag.NOT_TODAY)
    if previous is not None and quote.timestamp < previous.timestamp:
        flags.append(Flag.NON_MONOTONIC)

    age = (now - quote.timestamp).total_seconds()
    if age > max_age_seconds:
        flags.append(Flag.STALE)
    if age < -max_future_seconds:
        # The source cannot be ahead of real time, so the local clock is behind.
        flags.append(Flag.CLOCK_SKEW)

    if not index:
        if quote.has_bid and quote.has_ask:
            if phase == Phase.CONTINUOUS and quote.bid > quote.ask:
                flags.append(Flag.CROSSED_BOOK)
            elif phase == Phase.CONTINUOUS and quote.bid == quote.ask:
                flags.append(Flag.LOCKED_BOOK)
        elif quote.has_bid:
            sealed = quote.limit_up is not None and quote.bid >= quote.limit_up
            flags.append(Flag.LIMIT_UP_SEALED if sealed else Flag.ONE_SIDED_BOOK)
        elif quote.has_ask:
            sealed = quote.limit_down is not None and quote.ask <= quote.limit_down
            flags.append(Flag.LIMIT_DOWN_SEALED if sealed else Flag.ONE_SIDED_BOOK)
        else:
            flags.append(Flag.NO_BOOK)

        if quote.limit_up is None or quote.limit_down is None:
            flags.append(Flag.MISSING_LIMITS)
        elif quote.last > quote.limit_up or quote.last < quote.limit_down:
            flags.append(Flag.PRICE_OUTSIDE_LIMITS)

    if (
        previous is not None and previous.last > 0
        and previous.timestamp.astimezone(SHANGHAI).date() == quote_date
        and abs(quote.last / previous.last - 1) > jump_ratio
    ):
        flags.append(Flag.LARGE_JUMP)

    return Assessment(phase=phase, flags=tuple(flags))
