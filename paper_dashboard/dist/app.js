const state = { data: null, csrf: "", side: "BUY" };
const $ = (selector) => document.querySelector(selector);
const money = new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", minimumFractionDigits: 2 });
const number = new Intl.NumberFormat("zh-CN");
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const statusText = { APPROVAL_PENDING: "待批准", APPROVED: "已批准", SUBMITTED: "已提交", PARTIALLY_FILLED: "部分成交", FILLED: "已成交", RISK_REJECTED: "风控拒绝", USER_REJECTED: "用户拒绝", CANCELED: "已撤销", REJECTED: "已拒绝" };

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.className = "toast", 3600);
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
    headers["X-Paper-CSRF"] = state.csrf;
  }
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败 ${response.status}`);
  return payload;
}

function pnlClass(value) { return Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : ""; }
function formatTime(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—"; }

function renderSetup(data) {
  $("#dashboard").hidden = true;
  $("#setupScreen").hidden = false;
  const defaults = data.setupDefaults;
  for (const name of ["initial_cash", "max_order_value", "max_symbol_value", "max_total_exposure", "min_cash_buffer", "max_daily_loss"]) {
    $(`[name="${name}"]`).value = defaults[name];
  }
  $("#marketStatus").className = "status-chip";
  $("#marketStatus").innerHTML = "<i></i>等待初始化";
  $("#killButton").disabled = true;
}

function renderDashboard(data) {
  $("#setupScreen").hidden = true;
  $("#dashboard").hidden = false;
  const account = data.account;
  const initial = Number(account.initial_cash);
  const totalPnl = Number(account.equity) - initial;
  $("#equity").textContent = money.format(account.equity);
  $("#cash").textContent = money.format(account.cash);
  $("#marketValue").textContent = money.format(account.market_value);
  $("#totalPnl").textContent = money.format(totalPnl);
  $("#totalPnl").className = pnlClass(totalPnl);
  $("#equityDelta").textContent = `本金 ${money.format(initial)}`;
  $("#cashRatio").textContent = `现金占比 ${account.equity > 0 ? (Number(account.cash) / Number(account.equity) * 100).toFixed(1) : "0.0"}%`;
  $("#exposureRatio").textContent = `仓位 ${account.equity > 0 ? (Number(account.market_value) / Number(account.equity) * 100).toFixed(1) : "0.0"}%`;
  $("#pnlBreakdown").textContent = `已实现 ${money.format(account.realized_pnl)} · 浮动 ${money.format(account.unrealized_pnl)}`;

  const status = $("#marketStatus");
  status.className = `status-chip ${data.killSwitch ? "stopped" : "ok"}`;
  status.innerHTML = `<i></i>${data.killSwitch ? "交易已停止" : "模拟系统正常"}`;
  $("#killButton").textContent = data.killSwitch ? "恢复交易" : "一键停止";
  $("#killButton").className = data.killSwitch ? "button ghost" : "button danger";
  $("#killButton").disabled = false;

  const positions = account.positions || [];
  $("#positionCount").textContent = `${positions.length} 只`;
  $("#positionsEmpty").hidden = positions.length > 0;
  $("#positionsBody").innerHTML = positions.map((row) => `
    <tr><td><b>${esc(row.symbol)}</b><small>模拟持仓</small></td><td>${number.format(row.quantity)} / ${number.format(row.available_quantity)}</td><td>${money.format(row.average_cost)} / ${money.format(row.last)}</td><td>${money.format(row.market_value)}</td><td class="${pnlClass(row.unrealized_pnl)}">${money.format(row.unrealized_pnl)}</td></tr>`).join("");

  const symbols = data.config.whitelist || [];
  const currentSymbol = $("#symbolInput").value;
  $("#symbolInput").innerHTML = symbols.map((symbol) => `<option value="${esc(symbol)}">${esc(symbol)}</option>`).join("");
  if (symbols.includes(currentSymbol)) $("#symbolInput").value = currentSymbol;

  const pending = data.pending || [];
  $("#approvalCount").textContent = `${pending.length} 笔`;
  $("#approvalsEmpty").hidden = pending.length > 0;
  $("#approvalList").innerHTML = pending.map((row) => {
    const canApprove = row.status === "APPROVAL_PENDING";
    const canExecute = row.status === "APPROVED" || row.status === "SUBMITTED" || row.status === "PARTIALLY_FILLED";
    return `<article class="approval-card">
      <div><span class="status-label">${esc(statusText[row.status] || row.status)}</span><h3>${esc(row.side === "BUY" ? "买入" : "卖出")} ${esc(row.symbol)}</h3><p>${esc((row.reason_codes || []).join(" · "))}</p><small>${esc(row.intent_id)}</small></div>
      <div class="approval-metric"><span>数量</span><b>${number.format(row.quantity)} 股</b></div>
      <div class="approval-metric"><span>限价</span><b>${money.format(row.limit_price)}</b></div>
      <div class="approval-metric"><span>有效期</span><b>${esc(formatTime(row.valid_until))}</b></div>
      <div class="approval-actions">${canApprove ? `<button class="button primary" data-action="approve" data-id="${esc(row.intent_id)}">批准</button><button class="button ghost" data-action="reject" data-id="${esc(row.intent_id)}">拒绝</button>` : ""}${canExecute ? `<button class="button primary" data-action="execute" data-id="${esc(row.intent_id)}">模拟执行</button>` : ""}</div>
    </article>`;
  }).join("");

  const orders = data.orders || [];
  $("#ordersEmpty").hidden = orders.length > 0;
  $("#ordersBody").innerHTML = orders.map((row) => `<tr><td>${esc(formatTime(row.updated_at))}</td><td>${esc(row.symbol)}</td><td class="${row.side === "BUY" ? "positive" : "negative"}">${row.side === "BUY" ? "买入" : "卖出"}</td><td>${number.format(row.filled_quantity)} / ${number.format(row.quantity)}</td><td>${money.format(row.limit_price)}</td><td>${esc(statusText[row.status] || row.status)}</td></tr>`).join("");

  const fills = data.fills || [];
  $("#fillsEmpty").hidden = fills.length > 0;
  $("#fillsBody").innerHTML = fills.map((row) => {
    const fees = Number(row.commission) + Number(row.stamp_tax) + Number(row.transfer_fee);
    return `<tr><td>${esc(formatTime(row.created_at))}</td><td>${esc(row.symbol)}</td><td class="${row.side === "BUY" ? "positive" : "negative"}">${row.side === "BUY" ? "买入" : "卖出"}</td><td>${number.format(row.quantity)}</td><td>${money.format(row.price)}</td><td>${money.format(fees)}</td></tr>`;
  }).join("");

  const cfg = data.config;
  const riskRows = [
    ["单笔上限", money.format(cfg.max_order_value)], ["单股上限", money.format(cfg.max_symbol_value)],
    ["总仓位上限", money.format(cfg.max_total_exposure)], ["现金安全垫", money.format(cfg.min_cash_buffer)],
    ["亏损保护", money.format(cfg.max_daily_loss)], ["行情最大时延", `${cfg.max_quote_age_seconds} 秒`],
    ["允许标的", cfg.whitelist.join("、")]
  ];
  $("#riskList").innerHTML = riskRows.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("");
  const healthy = !(data.reconciliation || []).length;
  $("#reconcileBadge").className = `health-badge ${healthy ? "ok" : "bad"}`;
  $("#reconcileBadge").textContent = healthy ? "账本一致" : "需要对账";
  if (data.latestQuote) $("#quoteBox").innerHTML = `<span>最近行情</span><b>${esc(data.latestQuote.symbol)} ${money.format(data.latestQuote.last)}<br>${esc(formatTime(data.latestQuote.timestamp))}</b>`;
}

async function refresh() {
  try {
    const data = await request("/api/state");
    state.data = data;
    state.csrf = data.csrfToken;
    if (data.setupRequired) renderSetup(data); else renderDashboard(data);
    return data;
  } catch (error) { toast(error.message, true); }
}

function requireObject(input, label = "输入") {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error(`${label}必须是对象`);
  return input;
}

function requireString(value, label) {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${label}不能为空`);
  return value.trim();
}

function requirePositiveNumber(value, label) {
  const result = Number(value);
  if (!Number.isFinite(result) || result <= 0) throw new Error(`${label}必须是正数`);
  return result;
}

async function ensureDashboardState() {
  if (!state.csrf || !state.data) await refresh();
  if (!state.csrf || !state.data) throw new Error("看板状态暂不可用");
  return state.data;
}

function conciseAccount(data) {
  if (data.setupRequired) return { setupRequired: true, setupDefaults: data.setupDefaults };
  return {
    setupRequired: false,
    killSwitch: data.killSwitch,
    account: data.account,
    pending: data.pending,
    orders: data.orders,
    fills: data.fills,
    reconciliation: data.reconciliation,
  };
}

function registerWebMcpTools() {
  const context = typeof document === "undefined" ? undefined : document.modelContext;
  if (!context?.registerTool) return;
  const lifecycle = new AbortController();
  const register = (tool) => {
    try { void Promise.resolve(context.registerTool(tool, { signal: lifecycle.signal })).catch(console.error); }
    catch (error) { console.error(error); }
  };

  register({
    name: "read_paper_trading_account",
    title: "读取模拟账户",
    description: "读取模拟账户的资金、持仓、待审批交易、订单、成交、停止状态和对账结果，不改变任何数据。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, untrustedContentHint: false },
    async execute(input) {
      requireObject(input);
      return conciseAccount(await refresh());
    },
  });

  register({
    name: "read_market_quote",
    title: "查询模拟行情",
    description: "查询一个白名单 A 股标的的当前行情快照，不创建交易。",
    inputSchema: {
      type: "object",
      properties: { symbol: { type: "string", description: "股票代码，例如 002815.SZ" } },
      required: ["symbol"], additionalProperties: false,
    },
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    async execute(input) {
      const symbol = requireString(requireObject(input).symbol, "股票代码").toUpperCase();
      await ensureDashboardState();
      const quote = await request(`/api/quote?symbol=${encodeURIComponent(symbol)}`);
      await refresh();
      return quote;
    },
  });

  register({
    name: "create_paper_trading_account",
    title: "创建模拟账户",
    description: "用明确给定的资金、风控参数和白名单创建本地模拟账户。只支持模拟模式，且已有配置时会失败。",
    inputSchema: {
      type: "object",
      properties: {
        initial_cash: { type: "number", exclusiveMinimum: 0 },
        max_order_value: { type: "number", exclusiveMinimum: 0 },
        max_symbol_value: { type: "number", exclusiveMinimum: 0 },
        max_total_exposure: { type: "number", exclusiveMinimum: 0 },
        min_cash_buffer: { type: "number", minimum: 0 },
        max_daily_loss: { type: "number", exclusiveMinimum: 0 },
        whitelist: { type: "array", minItems: 1, uniqueItems: true, items: { type: "string" } },
      },
      required: ["initial_cash", "max_order_value", "max_symbol_value", "max_total_exposure", "min_cash_buffer", "max_daily_loss", "whitelist"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      const values = requireObject(input);
      const current = await ensureDashboardState();
      if (!current.setupRequired) throw new Error("模拟账户已经创建，不能覆盖现有配置");
      const whitelist = Array.isArray(values.whitelist) ? values.whitelist.map((item) => requireString(item, "股票代码").toUpperCase()) : [];
      if (!whitelist.length) throw new Error("白名单至少需要一个标的");
      const payload = {
        initial_cash: requirePositiveNumber(values.initial_cash, "初始资金"),
        max_order_value: requirePositiveNumber(values.max_order_value, "单笔上限"),
        max_symbol_value: requirePositiveNumber(values.max_symbol_value, "单股上限"),
        max_total_exposure: requirePositiveNumber(values.max_total_exposure, "总仓位上限"),
        min_cash_buffer: Number(values.min_cash_buffer),
        max_daily_loss: requirePositiveNumber(values.max_daily_loss, "亏损保护"),
        whitelist,
      };
      if (!Number.isFinite(payload.min_cash_buffer) || payload.min_cash_buffer < 0) throw new Error("现金安全垫不能为负数");
      await request("/api/setup", { method: "POST", body: JSON.stringify(payload) });
      const data = await refresh();
      return { created: true, paperMode: true, account: data.account, whitelist: data.config.whitelist };
    },
  });

  register({
    name: "stage_trade_proposal",
    title: "提交模拟交易建议",
    description: "对指定标的提交买入或卖出建议并立即经过风控；通过后进入待人工批准，不会直接成交。",
    inputSchema: {
      type: "object",
      properties: {
        symbol: { type: "string" }, side: { type: "string", enum: ["BUY", "SELL"] },
        quantity: { type: "integer", minimum: 100, multipleOf: 100 },
        limit_price: { type: "number", exclusiveMinimum: 0 }, reason: { type: "string" },
      },
      required: ["symbol", "side", "quantity", "limit_price", "reason"], additionalProperties: false,
    },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      const values = requireObject(input);
      const data = await ensureDashboardState();
      if (data.setupRequired) throw new Error("请先创建模拟账户");
      const payload = {
        symbol: requireString(values.symbol, "股票代码").toUpperCase(),
        side: requireString(values.side, "方向").toUpperCase(),
        quantity: requirePositiveNumber(values.quantity, "数量"),
        limit_price: requirePositiveNumber(values.limit_price, "限价"),
        reason: requireString(values.reason, "理由"),
      };
      if (!["BUY", "SELL"].includes(payload.side)) throw new Error("方向只能是 BUY 或 SELL");
      if (!Number.isInteger(payload.quantity) || payload.quantity % 100 !== 0) throw new Error("数量必须是 100 股的整数倍");
      const result = await request("/api/propose", { method: "POST", body: JSON.stringify(payload) });
      await refresh();
      return result;
    },
  });

  register({
    name: "complete_trade_action",
    title: "处理模拟交易",
    description: "对一个明确的交易建议执行批准、拒绝或模拟成交。execute 仅在已批准后才可执行，并仍会经过风控。",
    inputSchema: {
      type: "object",
      properties: { action: { type: "string", enum: ["approve", "reject", "execute"] }, intent_id: { type: "string" } },
      required: ["action", "intent_id"], additionalProperties: false,
    },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      const values = requireObject(input);
      await ensureDashboardState();
      const payload = { action: requireString(values.action, "操作"), intent_id: requireString(values.intent_id, "交易建议 ID") };
      if (!["approve", "reject", "execute"].includes(payload.action)) throw new Error("不支持的交易操作");
      const result = await request("/api/action", { method: "POST", body: JSON.stringify(payload) });
      await refresh();
      return result;
    },
  });

  register({
    name: "set_paper_trading_paused",
    title: "暂停或恢复模拟交易",
    description: "设置模拟交易总开关。paused=true 会阻止新的交易操作，false 会恢复模拟交易。",
    inputSchema: {
      type: "object", properties: { paused: { type: "boolean" } }, required: ["paused"], additionalProperties: false,
    },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      const values = requireObject(input);
      if (typeof values.paused !== "boolean") throw new Error("paused 必须是布尔值");
      await ensureDashboardState();
      await request("/api/action", { method: "POST", body: JSON.stringify({ action: values.paused ? "kill" : "resume" }) });
      const data = await refresh();
      return { paused: data.killSwitch };
    },
  });

  window.addEventListener("pagehide", () => lifecycle.abort(), { once: true });
}

$("#setupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.whitelist = form.getAll("whitelist");
  try { await request("/api/setup", { method: "POST", body: JSON.stringify(payload) }); toast("模拟账户已创建"); await refresh(); }
  catch (error) { toast(error.message, true); }
});

document.querySelectorAll(".segmented button").forEach((button) => button.addEventListener("click", () => {
  state.side = button.dataset.side;
  document.querySelectorAll(".segmented button").forEach((item) => item.classList.remove("active", "buy"));
  button.classList.add("active");
  if (state.side === "BUY") button.classList.add("buy");
}));

$("#symbolInput").addEventListener("change", async (event) => {
  try {
    const quote = await request(`/api/quote?symbol=${encodeURIComponent(event.target.value)}`);
    $("#priceInput").value = state.side === "BUY" ? quote.ask : quote.bid;
    $("#quoteBox").innerHTML = `<span>买一 / 卖一</span><b>${money.format(quote.bid)} / ${money.format(quote.ask)}<br>${esc(formatTime(quote.timestamp))}</b>`;
  } catch (error) { toast(error.message, true); }
});

$("#tradeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = { symbol: $("#symbolInput").value, side: state.side, quantity: Number($("#quantityInput").value), limit_price: $("#priceInput").value, reason: $("#reasonInput").value };
  try {
    const result = await request("/api/propose", { method: "POST", body: JSON.stringify(payload) });
    toast(result.decision === "REJECTED" ? `风控拒绝：${(result.rejection_reasons || []).join("；")}` : "交易建议已进入待审批", result.decision === "REJECTED");
    await refresh();
  } catch (error) { toast(error.message, true); }
});

$("#approvalList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  try {
    const result = await request("/api/action", { method: "POST", body: JSON.stringify({ action: button.dataset.action, intent_id: button.dataset.id }) });
    toast(result.status === "RISK_REJECTED" ? `执行被风控拒绝：${(result.rejection_reasons || []).join("；")}` : `状态：${statusText[result.status] || result.status}`, result.status === "RISK_REJECTED");
    await refresh();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

$("#killButton").addEventListener("click", async () => {
  const action = state.data?.killSwitch ? "resume" : "kill";
  try { await request("/api/action", { method: "POST", body: JSON.stringify({ action }) }); toast(action === "kill" ? "已停止所有新交易" : "已恢复模拟交易"); await refresh(); }
  catch (error) { toast(error.message, true); }
});

$("#refreshButton").addEventListener("click", refresh);
registerWebMcpTools();
refresh();
