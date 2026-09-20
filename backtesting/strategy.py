from __future__ import annotations

import pandas as pd

from .config import StrategyConfig


def moving_average_target(frame: pd.DataFrame, config: StrategyConfig) -> pd.Series:
    """Long when short MA is above long MA; the signal is known only after that close."""
    close = frame["close"].astype(float)
    short = close.rolling(config.short_window, min_periods=config.short_window).mean()
    long = close.rolling(config.long_window, min_periods=config.long_window).mean()
    target = (short > long) & long.notna()
    target.name = "target_long"
    return target
