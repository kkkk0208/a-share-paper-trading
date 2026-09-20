# A 股模拟交易 MVP 使用说明

本系统的成交结果不代表真实成交，不能作为策略有效性的证据。

## 当前能力

- 腾讯实时盘口快照：最新价、买一、卖一、盘口数量、涨跌停价和行情时间。
- 本地模拟账户：现金、持仓、T+1 可用数量、成本、费用和盈亏。
- 交易意图、硬性风控、人工批准、模拟撮合和完整 SQLite 审计流水。
- 幂等下单：重复执行相同意图不会产生第二笔订单。
- 一键停止：启用后拒绝所有新交易。
- 默认没有真实券商适配器，也没有实盘模式开关。
- 本地浏览器看板：账户、持仓、订单、成交、待审批、风控和一键停止。
- 持仓按买入批次保存，卖出按 FIFO 扣减，T+1 可用量按最新行情日期实时计算。
- 腾讯主行情源与新浪降级源；任何来源缺失关键字段或全部失败时 fail closed。
- 每日净值曲线、沪深 300 对比、回撤和交易绩效。
- 手工录入并应用现金分红、送股与拆股。

实时行情与模拟成交是两件事：行情来自真实市场快照，订单只在本地撮合，不发送到交易所。

## 必须先了解的限制

- 当前实时行情是快照式 Level 1 数据，不是交易所逐笔队列。
- 模拟成交仅适合练习执行纪律和验证下单流程，不适合验证策略；一天几笔的样本量，即使运行一年通常也不足以达到统计显著，策略验证需另做 backtest。
- 交易日历复用回测下载生成的 `data/trading_calendar.csv`；首次使用前必须成功执行一次回测数据下载。文件不存在时交易风控 fail closed，不会把普通工作日当成正式交易日。
- 新浪公开 Level 1 快照不稳定且可能缺少涨跌停价；缺失时系统会拒绝交易，不会按固定百分比推算。
- vn.py PaperAccount 适配边界已经预留，但依赖尚未安装；核心使用本地确定性撮合器。
- 默认佣金率 0.025%、最低佣金 5 元仅是示例，必须按计划模拟的券商账户确认。印花税默认卖出 0.05%，过户费默认双向 0.001%。

## 首次配置

推荐直接启动看板，在网页中确认并创建账户：

```powershell
.\scripts\run_paper_dashboard.ps1
```

启动命令会打印一条带随机访问令牌的本地网址，必须使用该网址进入。服务仅监听本机地址；所有 POST 还会校验同源 Origin 和独立 CSRF 令牌。网页批准时必须手动输入股票代码后四位或完整数量。

如需使用命令行配置：

1. 阅读 `config/paper_trading.example.json`。
2. 明确填写本金、白名单、单笔上限、单股上限、总仓位上限、现金安全垫和亏损保护值。
3. 将审核后的文件复制为 `config/paper_trading.json`。该文件已加入 `.gitignore`。
4. 初始化账户：

```powershell
.\scripts\paper_trading.ps1 init
```

示例文件中的数值仅用于演示，不应在未确认的情况下直接作为正式模拟账户规则。

## 一笔交易的完整流程

### 1. 生成结构化交易意图

```powershell
.\scripts\paper_trading.ps1 draft `
  --symbol 002195.SZ `
  --side BUY `
  --quantity 100 `
  --limit-price 6.10 `
  --reason manual_test `
  --output runtime\paper_trading\intents\buy-002195.json
```

### 2. 使用实时行情进行风控

```powershell
.\scripts\paper_trading.ps1 propose `
  --intent runtime\paper_trading\intents\buy-002195.json
```

行情过期、非连续竞价、停牌、超出白名单、资金不足或违反仓位限制时，意图会被拒绝。

### 3. 人工批准

```powershell
.\scripts\paper_trading.ps1 approve `
  --intent-id <精确的意图ID> `
  --actor user `
  --note "已核对代码、数量、限价和风险占用"
```

### 4. 获取下一条实时行情并模拟执行

```powershell
.\scripts\paper_trading.ps1 execute --intent-id <精确的意图ID>
```

执行器不会用生成意图时已经看到的旧价格回填成交。只有后续合格行情满足限价和盘口数量条件时才会模拟成交。

### 5. 查看账户和对账

```powershell
.\scripts\paper_trading.ps1 status
.\scripts\paper_trading.ps1 reconcile
```

`settle` 已改为兼容 no-op，可用数量不再依赖人工结转：

```powershell
.\scripts\paper_trading.ps1 settle --trade-date 2026-09-21
```

## 意图查询、取消与过期

```powershell
.\scripts\paper_trading.ps1 intents --status pending
.\scripts\paper_trading.ps1 intents --status approved
.\scripts\paper_trading.ps1 intents --status expired
.\scripts\paper_trading.ps1 cancel --intent-id <精确的意图ID> --reason "条件变化"
```

意图默认 30 分钟过期；`propose`、`approve` 和 `execute` 都会阻止继续处理过期意图，并写入审计记录。

## 收盘净值与绩效

收盘后可主动检查净值和沪深 300 快照：

```powershell
.\scripts\paper_trading.ps1 snapshot
.\scripts\paper_trading.ps1 perf --from 2026-01-01 --to 2026-12-31
```

`snapshot` 不再依赖每天手工执行。任意命令都会检查“上一条快照—最新行情交易日”之间的交易日缺口，并优先用固化历史收盘价补齐。若旧日期之后已经发生成交或公司行动，账本无法可靠倒推，系统不会猜测；`perf` 会返回 `snapshot_gap_count`、`snapshot_gap_dates` 和 `metrics_incomplete`。绩效输出累计/年化收益、最大回撤、交易笔数、胜率、盈亏比和相对沪深 300 的超额收益。少于两个收盘快照时拒绝计算。

## 除权除息

公司行动数据先手工录入，再按日期应用；重复应用不会二次调整：

```powershell
.\scripts\paper_trading.ps1 corp-add `
  --symbol 002195.SZ --ex-date 2026-06-01 `
  --cash-dividend 0.10 --stock-dividend 0 --split-ratio 1
.\scripts\paper_trading.ps1 corp-apply --through 2026-06-01
```

如果待应用除权日早于或等于同一证券已有成交，`corp-apply` 会直接拒绝。此时事后修改成本无法修复已产生的已实现盈亏，应从除权日前备份重建账本。

## 撮合保守约束

以下三项已经在撮合代码中强制执行，任一不满足都不会成交：

- `matching.min_depth_ratio`：买入卖一量／卖出买一量至少是委托数量的指定倍数，默认 3 倍。
- `matching.min_ticks_delay`：批准后至少等待指定数量的新行情，默认 1 条；批准前或同一时间戳行情不可成交。
- `matching.max_quote_age_seconds`：撮合行情最大年龄，默认 10 秒；过期行情 fail closed。

成交记录同时保存行情时间戳和当时五档盘口。费用参数只维护在 `config/fees.json`，模拟盘与回测使用同一计算函数。

## 一键停止与恢复

```powershell
.\scripts\paper_trading.ps1 kill
.\scripts\paper_trading.ps1 resume
```

`kill` 后只能查询和对账，所有新交易都会被硬风控拒绝。恢复不会自动执行此前被拒绝或过期的意图。

## 运行数据

- 数据库：`runtime/paper_trading/paper_trading.sqlite3`
- 停止标记：`runtime/paper_trading/kill_switch`
- 交易意图：`runtime/paper_trading/intents/`

运行数据和本地配置不会提交到 Git。删除数据库等同于删除模拟账户历史，操作前必须备份。
