"""Look-ahead-safe A-share strategy backtesting."""

from .config import BacktestConfig, load_backtest_config
from .engine import BacktestResult, run_backtest

__all__ = ["BacktestConfig", "BacktestResult", "load_backtest_config", "run_backtest"]
