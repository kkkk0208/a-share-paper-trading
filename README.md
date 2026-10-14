# 本地 A 股研究与模拟执行

跨聊天继续项目前，请先阅读 [`PROJECT_STATUS.md`](PROJECT_STATUS.md)。

模拟交易模块使用真实行情快照和本地账本，所有订单只在本地撮合，不连接真实资金账户。

实时模拟适合练习执行纪律和验证下单流程，不适合验证策略。一天几笔的样本量，即使运行一年通常也达不到统计显著；策略有效性必须另做覆盖足够市场周期、包含交易成本和样本外检验的 backtest。

使用方法、风险边界和命令清单见 [`docs/paper-trading-quickstart.md`](docs/paper-trading-quickstart.md)。

独立策略回测已经拆成单独模块，说明和示例命令见 [`docs/backtesting.md`](docs/backtesting.md)。当前 MA5/MA20 规则只用于验证框架，不代表正式投资策略。
