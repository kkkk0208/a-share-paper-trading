# A 股持仓日报数据增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 600601 自动化增加两只持仓的近期公告、估值历史位置、融资融券、未来解禁和近期大宗交易，并在增强源失效时保留原有日报。

**Architecture:** 新增独立的 `a_stock_enrichment.py`，把数据源适配、字段校验、缓存与摘要计算隔离在报告生成器之外。`generate_600601_report.py` 仅传入持仓代码、交易日和腾讯当前 PE/PB，接收统一字典后渲染文本、HTML 和 PDF；增强错误使用独立状态，不改变现有主链 `STATUS=OK` 判定。

**Tech Stack:** Python 3.9+、`requests`、可选 `baostock`、`unittest`、PowerShell 回归测试、ReportLab。

---

## 文件结构

- Create: `a_stock_enrichment.py` — 数据源适配、缓存、聚合和纯摘要函数。
- Create: `tests/test_a_stock_enrichment.py` — 无网络单元测试。
- Modify: `generate_600601_report.py` — 解析腾讯 PE/PB、调用增强模块、保存原始 JSON、渲染新章节。
- Modify: `test_600601_outputs.ps1` — 验证新章节及降级语义。
- Keep unchanged: `run_600601_automation.ps1` — baostock 缺失属于增强降级，不加入强制依赖预检。

## Task 1：建立纯计算接口的失败测试

**Files:**
- Create: `tests/test_a_stock_enrichment.py`

- [ ] **Step 1: 写估值分位、公告筛选和风险摘要的失败测试**

```python
import unittest
from datetime import date, timedelta

from a_stock_enrichment import (
    build_block_trade_summary,
    build_margin_summary,
    build_valuation_summary,
    filter_recent_announcements,
)


class EnrichmentSummaryTests(unittest.TestCase):
    def test_valuation_ignores_non_positive_values(self):
        start = date(2025, 1, 1)
        history = [
            {"date": (start + timedelta(days=index)).isoformat(), "peTTM": value, "pbMRQ": value / 10}
            for index, value in enumerate(range(1, 122))
        ]
        history += [
            {"date": "2025-12-30", "peTTM": 0, "pbMRQ": -1},
            {"date": "2025-12-29", "peTTM": -20, "pbMRQ": 0},
        ]
        out = build_valuation_summary(history, current_pe=61, current_pb=6.1)
        self.assertEqual(out["sample_count_pe"], 121)
        self.assertAlmostEqual(out["pe_percentile"], 49.6, places=1)
        self.assertAlmostEqual(out["pb_percentile"], 49.6, places=1)

    def test_valuation_requires_120_valid_observations(self):
        history = [{"date": "2026-09-01", "peTTM": 20, "pbMRQ": 2}] * 119
        out = build_valuation_summary(history, current_pe=20, current_pb=2)
        self.assertIsNone(out["pe_percentile"])
        self.assertEqual(out["status"], "insufficient_history")

    def test_announcements_keep_seven_days_and_prioritize_risk(self):
        rows = [
            {"date": "2026-09-17", "title": "日常经营进展", "type": "其他", "url": "u1"},
            {"date": "2026-09-16", "title": "风险提示公告", "type": "风险提示", "url": "u2"},
            {"date": "2026-09-08", "title": "过期公告", "type": "其他", "url": "u3"},
        ]
        out = filter_recent_announcements(rows, date(2026, 9, 17), days=7, limit=5)
        self.assertEqual([row["title"] for row in out], ["风险提示公告", "日常经营进展"])

    def test_margin_uses_latest_five_disclosures(self):
        rows = [{"date": f"2026-09-{day:02d}", "rzye": value} for day, value in [(17, 120), (16, 115), (15, 110), (14, 105), (13, 100), (12, 80)]]
        out = build_margin_summary(rows)
        self.assertEqual(out["latest_balance"], 120)
        self.assertAlmostEqual(out["change_5d_pct"], 20.0)

    def test_block_trade_uses_amount_weighted_premium(self):
        rows = [
            {"date": "2026-09-16", "amount": 100, "premium_pct": 10},
            {"date": "2026-09-15", "amount": 300, "premium_pct": -10},
        ]
        out = build_block_trade_summary(rows, date(2026, 9, 17), days=30)
        self.assertEqual(out["count"], 2)
        self.assertAlmostEqual(out["weighted_premium_pct"], -5.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run:

```powershell
C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe -m unittest tests.test_a_stock_enrichment -v
```

Expected: `ModuleNotFoundError: No module named 'a_stock_enrichment'`。

## Task 2：实现纯计算、缓存和聚合骨架

**Files:**
- Create: `a_stock_enrichment.py`
- Modify: `tests/test_a_stock_enrichment.py`

- [ ] **Step 1: 实现最小纯函数使 Task 1 通过**

模块必须提供以下公开接口及算法：

```python
def _as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def filter_recent_announcements(rows, as_of, days=7, limit=5):
    cutoff = _as_date(as_of) - timedelta(days=days - 1)
    keywords = ("风险", "监管", "处罚", "立案", "减持", "质押", "业绩", "合同", "诉讼")
    kept = [row for row in rows if cutoff <= _as_date(row["date"]) <= _as_date(as_of)]
    return sorted(kept, key=lambda row: (not any(k in f'{row.get("type", "")}{row.get("title", "")}' for k in keywords), row["date"]), reverse=False)[:limit]


def _percentile(values, current, minimum):
    valid = [value for item in values if (value := _number(item)) is not None and value > 0]
    now = _number(current)
    if now is None or now <= 0 or len(valid) < minimum:
        return None, len(valid)
    return round(sum(value < now for value in valid) / len(valid) * 100, 1), len(valid)


def build_valuation_summary(history, current_pe, current_pb, minimum=120):
    pe, pe_count = _percentile((row.get("peTTM") for row in history), current_pe, minimum)
    pb, pb_count = _percentile((row.get("pbMRQ") for row in history), current_pb, minimum)
    return {"current_pe": _number(current_pe), "current_pb": _number(current_pb), "pe_percentile": pe,
            "pb_percentile": pb, "sample_count_pe": pe_count, "sample_count_pb": pb_count,
            "status": "ok" if pe is not None or pb is not None else "insufficient_history"}


def build_margin_summary(rows):
    ordered = sorted(rows, key=lambda row: row.get("date", ""), reverse=True)[:5]
    latest = _number(ordered[0].get("rzye")) if ordered else None
    oldest = _number(ordered[-1].get("rzye")) if ordered else None
    change = ((latest / oldest - 1) * 100) if latest is not None and oldest not in (None, 0) else None
    return {"latest_date": ordered[0]["date"] if ordered else None, "latest_balance": latest,
            "change_5d_pct": round(change, 2) if change is not None else None, "sample_count": len(ordered)}


def build_unlock_summary(rows, as_of, days=90):
    start = _as_date(as_of)
    end = start + timedelta(days=days)
    upcoming = [row for row in rows if start <= _as_date(row["date"]) <= end]
    return {"count": len(upcoming), "upcoming": sorted(upcoming, key=lambda row: row["date"])}


def build_block_trade_summary(rows, as_of, days=30):
    cutoff = _as_date(as_of) - timedelta(days=days - 1)
    recent = [row for row in rows if cutoff <= _as_date(row["date"]) <= _as_date(as_of)]
    weighted = [(float(row["amount"]), float(row["premium_pct"])) for row in recent
                if _number(row.get("amount")) not in (None, 0) and _number(row.get("premium_pct")) is not None]
    total = sum(amount for amount, premium in weighted)
    premium = sum(amount * value for amount, value in weighted) / total if total else None
    return {"count": len(recent), "amount": sum(_number(row.get("amount")) or 0 for row in recent),
            "weighted_premium_pct": round(premium, 2) if premium is not None else None}
```

实现规则：日期按 ISO `YYYY-MM-DD` 解析；公告风险优先级关键词固定为“风险、监管、处罚、立案、减持、质押、业绩、合同、诉讼”；PE/PB 仅使用大于零的有限数；分位为 `小于当前值的样本数 / 有效样本数 × 100`；融资变化取按日期降序后的第 1 与第 5 条；大宗交易溢折价按成交金额加权。

- [ ] **Step 2: 运行单元测试并确认纯函数通过**

Run:

```powershell
C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe -m unittest tests.test_a_stock_enrichment -v
```

Expected: 5 tests, 0 failures。

- [ ] **Step 3: 增加聚合器失败测试**

```python
from tempfile import TemporaryDirectory
from pathlib import Path
from a_stock_enrichment import EnrichmentAdapters, collect_enrichment


class EnrichmentCollectorTests(unittest.TestCase):
    def test_collect_enrichment_isolates_each_source_failure(self):
        def fail(code, as_of):
            raise RuntimeError("offline")

        adapters = EnrichmentAdapters(
            announcements=fail,
            valuation_history=fail,
            margin=fail,
            lockup=fail,
            block_trades=fail,
        )
        with TemporaryDirectory() as temp:
            out = collect_enrichment(
                codes=["002195", "002815"],
                as_of="2026-09-17",
                current_valuations={"002195": {"pe": 40.0, "pb": 3.0}, "002815": {"pe": 20.0, "pb": 2.0}},
                raw_dir=Path(temp),
                adapters=adapters,
            )
        self.assertEqual(set(out["holdings"]), {"002195", "002815"})
        self.assertEqual(len(out["errors"]), 10)
        self.assertEqual(out["holdings"]["002195"]["announcements_status"], "unavailable")
```

- [ ] **Step 4: 运行新增测试并确认 `EnrichmentAdapters` 尚不存在**

Run the same unittest command. Expected: import error for `EnrichmentAdapters` or `collect_enrichment`。

- [ ] **Step 5: 实现 `EnrichmentAdapters` 与 `collect_enrichment`**

```python
@dataclass(frozen=True)
class EnrichmentAdapters:
    announcements: Callable[[str, str], list[dict]]
    valuation_history: Callable[[str, str], list[dict]]
    margin: Callable[[str, str], list[dict]]
    lockup: Callable[[str, str], list[dict]]
    block_trades: Callable[[str, str], list[dict]]


def collect_enrichment(codes, as_of, current_valuations, raw_dir, adapters=None) -> dict:
    """逐标的、逐来源隔离错误，返回 holdings/source_status/errors/fetched_at。"""
```

每个适配器独立 `try/except`；空结果是 `ok_empty`，异常是 `unavailable`。估值历史缓存路径固定为 `raw_dir / "cache" / f"valuation_{code}_{as_of}.json"`，只读取与 `as_of` 完全一致的文件。

- [ ] **Step 6: 运行单元测试并确认聚合器降级测试通过**

Expected: 6 tests, 0 failures。

## Task 3：实现并测试真实数据源适配器

**Files:**
- Modify: `a_stock_enrichment.py`
- Modify: `tests/test_a_stock_enrichment.py`

- [ ] **Step 1: 为巨潮和东方财富字段映射写失败测试**

测试使用假的 `requests.Session`，覆盖：巨潮时间戳、公告代码不匹配、东财 `result.data` 缺失、融资字段、`FREE_SHARES/ABLE_FREE_SHARES/FREE_RATIO`、大宗交易缺收盘价时不计算溢折价。

```python
def test_block_trade_without_close_keeps_premium_null(self):
    rows = normalize_block_trades([{
        "TRADE_DATE": "2026-09-16", "SECURITY_CODE": "002195",
        "DEAL_PRICE": 12.3, "CLOSE_PRICE": None, "DEAL_VOLUME": 10,
        "DEAL_AMT": 123, "BUYER_NAME": "A", "SELLER_NAME": "B",
    }], expected_code="002195")
    self.assertIsNone(rows[0]["premium_pct"])
```

- [ ] **Step 2: 运行测试并确认规范化函数不存在**

Expected: import or name failure for the new normalizers。

- [ ] **Step 3: 实现数据源适配器**

必须实现以下公开函数：`fetch_cninfo_announcements(code, as_of, session=None)`、`fetch_eastmoney_rows(report_name, filter_str, sort_columns, sort_types, page_size, session=None)`、`fetch_margin_rows(code, as_of, session=None)`、`fetch_lockup_rows(code, as_of, session=None)`、`fetch_block_trade_rows(code, as_of, session=None)`、`fetch_baostock_valuation(code, as_of)` 和 `live_adapters()`。

`live_adapters()` 的完整绑定关系固定如下：

```python
def live_adapters():
    return EnrichmentAdapters(
        announcements=lambda code, as_of: fetch_cninfo_announcements(code, as_of),
        valuation_history=lambda code, as_of: fetch_baostock_valuation(code, as_of),
        margin=lambda code, as_of: fetch_margin_rows(code, as_of),
        lockup=lambda code, as_of: fetch_lockup_rows(code, as_of),
        block_trades=lambda code, as_of: fetch_block_trade_rows(code, as_of),
    )
```

巨潮使用官方 `szse_stock.json` 获取 `orgId` 后 POST `new/hisAnnouncement/query`；东方财富使用 `https://datacenter-web.eastmoney.com/api/data/v1/get`。每次请求超时 15 秒，最多重试 2 次；响应缺关键结构时抛 `RuntimeError`，不得静默返回空。baostock 在函数内部导入，确保登录成功后无论结果如何都执行 logout；代码映射为深市 `sz.002195`/`sz.002815`。

- [ ] **Step 4: 运行所有新增单元测试**

Expected: all tests pass with no network access。

- [ ] **Step 5: 安装可选 baostock 到专用环境并验证导入**

Run:

```powershell
C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe -m pip install baostock
C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe -c "import baostock; print('BAOSTOCK_OK')"
```

Expected: `BAOSTOCK_OK`。如果安装失败，记录增强估值为降级，但继续完成其他三类数据源。

## Task 4：把增强结果接入报告生成器

**Files:**
- Modify: `generate_600601_report.py`
- Modify: `tests/test_a_stock_enrichment.py`

- [ ] **Step 1: 写腾讯 PE/PB 字段解析失败测试**

```python
def test_current_valuation_comes_from_tencent_fields(self):
    fields = [""] * 51
    fields[39] = "117.73"
    fields[46] = "3.37"
    out = current_valuation_from_tencent(fields)
    self.assertEqual(out, {"pe": 117.73, "pb": 3.37})
```

- [ ] **Step 2: 确认测试因函数不存在而失败**

- [ ] **Step 3: 实现腾讯字段解析并调用聚合器**

在 `FIELD_NAMES` 增加：

```python
39: "pe_ttm",
44: "float_market_cap_yi",
45: "total_market_cap_yi",
46: "pb_mrq",
```

新增 `current_valuation_from_tencent(fields)`，空字符串返回 `None`。在 `main()` 解析 quotes 后构造两只股票的 current valuation，调用 `collect_enrichment()`，并将结果写入 `RAW_DIR / f"enrichment_{now:%Y%m%d-%H%M%S}.json"`。把 `enrichment` 与 `enrichment_raw_path` 加入 `data`，但不要把 `enrichment["errors"]` 合并到 `market["errors"]`。

- [ ] **Step 4: 为文本和 HTML 渲染写失败测试**

测试固定数据必须断言包含：`公告、估值与事件风险`、`PE 117.73`、`PB 3.37`、`未来90天无待解禁`、`增强数据状态`。

- [ ] **Step 5: 实现渲染**

`build_text()` 新增 `## 7. 公告、估值与事件风险`；现有后续标题顺延。`build_html()` 新增 `id="enrichment"` 的全宽 panel。每只标的顺序固定为公告、估值、融资融券、解禁、大宗交易；`unavailable` 显示“接口暂不可用”，`ok_empty` 显示该窗口无数据。PDF 继续由同一文本渲染，确保内容一致。

- [ ] **Step 6: 运行 Python 单元测试**

Expected: all tests pass。

## Task 5：扩展输出回归测试

**Files:**
- Modify: `test_600601_outputs.ps1`

- [ ] **Step 1: 先增加会失败的新章节断言**

在 HTML 必需内容中加入：

```powershell
"公告、估值与事件风险", "增强数据状态"
```

在 HTML 布局标记中加入：

```powershell
'id="enrichment"'
```

文本必需内容加入：

```powershell
"公告、估值与事件风险", "增强原始数据"
```

- [ ] **Step 2: 对旧输出运行测试并确认新断言失败**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\15369\Documents\Codex\2026-05-21\new-chat\test_600601_outputs.ps1
```

Expected: missing new enrichment section marker。

- [ ] **Step 3: 生成固定数据报告并确认输出测试通过**

测试应通过依赖注入或测试夹具生成报告，避免把公网成功作为单元测试条件。

## Task 6：运行实时冒烟和完整自动化回归

**Files:**
- Verify: `a_stock_enrichment.py`
- Verify: `generate_600601_report.py`
- Verify: `test_600601_outputs.ps1`

- [ ] **Step 1: 运行全部 Python 单元测试**

```powershell
C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe -m unittest discover -s tests -v
```

Expected: 0 failures, 0 errors。

- [ ] **Step 2: 对两只持仓运行一次增强源冒烟测试**

```powershell
C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe -c "from pathlib import Path; from a_stock_enrichment import collect_enrichment; r=collect_enrichment(['002195','002815'],'2026-09-17',{'002195':{'pe':406.32,'pb':3.47},'002815':{'pe':117.73,'pb':3.37}},Path(r'C:\Users\15369\Documents\Codex\600601简报\raw')); print(len(r['holdings']), len(r['source_status']), len(r['errors']))"
```

Expected: first number is `2`; source failures may be nonzero but must be represented in `errors` rather than crash。

- [ ] **Step 3: 运行正式 runner**

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\15369\Documents\Codex\2026-05-21\new-chat\run_600601_automation.ps1
```

Expected: exit code 0, generator prints a line beginning with `STATUS=OK(`, regression script prints `OUTPUT_TESTS_OK`。若现有主链外部接口当天失败，只报告实际 `DEGRADED`/失败原因，不把旧文件当作通过。

- [ ] **Step 4: 检查最新 HTML、PDF、文本和原始 JSON**

确认两只持仓、日期、PE/PB、公告、融资数据、解禁、大宗交易和来源日期一致；确认旧标的 `铜陵有色/江西铜业/000630/600362` 未出现。

- [ ] **Step 5: 查看精确改动并仅提交本功能文件**

当前仓库的生产文件原本未被 Git 跟踪。提交前必须逐个指定文件，禁止 `git add .`：

```powershell
git add -- a_stock_enrichment.py tests/test_a_stock_enrichment.py generate_600601_report.py test_600601_outputs.ps1
git commit -m "feat: enrich A-share holding report data"
```

如果用户不希望把既有未跟踪生产文件纳入首个代码提交，则跳过 commit，只保留经过验证的工作区改动并明确说明。
