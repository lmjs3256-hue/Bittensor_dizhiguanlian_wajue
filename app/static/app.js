"use strict";

const $ = (selector) => document.querySelector(selector);
const state = {
  lastResult: null,
  library: [],
  books: [],
  categories: [],
  bookFilter: "",
  depthFilter: "all",
  // 金额筛选：按流入/流出 TAO 过滤掉金额很小的“轻微关联”，只影响显示（和地址 TXT 导出），不重新查询。
  amountFilter: { mode: "off", min: "" },
  taostatsConfigured: null,
  taostatsKeyStorage: "",
  apiKeyBodyOpen: false,
  // 手动「移除关联」的地址：本次会话内刷新重查时作为 removed_addresses 传给后端，
  // 这些地址及其线索的下游都不会再出现。新查询（非刷新）会清空。
  removedAddresses: new Set(),
  // 本次会话被「标为交易所」的地址：它们下游只经由该地址连通的关联地址先在
  // 页面上隐藏（不发回后端，避免误伤还有别的通路的地址）；刷新重查由后端按终点规则处理。
  exchangePruned: new Set(),
  // 关联地址分页：每页条数可选 10/20/50/100，默认 10。
  page: 1,
  pageSize: 10,
  // 同步目标标签组的默认值只在换了一个扫描地址时重算，避免刷新结果时覆盖手动选择。
  syncTargetSeed: "",
  syncTargetTouched: false,
  // 最近一次批量导入摘要：null 表示没有可撤回的导入。
  lastImport: null,
  // 外发预置地址库概况：{groups, addresses}，只用来提示按钮内容。
  seedInfo: { groups: 0, addresses: 0 },
  // 整组改分类：{kind: "book"|"label", name: "组名"}，null 表示没有在改。
  groupCategory: null,
  // 整组改名：{kind: "book"|"label", name: "组名"}，null 表示没有在改名。
  groupRename: null,
  // 组内「手动添加地址」：{kind, name}，null 表示没有打开输入框。
  groupAdd: null,
  // 合并群组面板：picked 存 `${kind}\u0000${name}`，mode 为 new / inherit。
  merge: { open: false, picked: new Set(), mode: "new" },
  libraryGroupsOpen: new Set(),
  history: { offset: 0, total: 0, items: [] },
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function number(value) {
  if (value === null || value === undefined) return "-";
  return new Intl.NumberFormat("zh-CN").format(value);
}

function shortAddr(value) {
  const text = String(value || "");
  return text.length > 8 ? `${text.slice(0, 4)}…${text.slice(-4)}` : text;
}

function formatTao(value, suffix = false) {
  if (value === null || value === undefined || value === "") return "未获取";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "未获取";
  const formatted = new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(parsed);
  return suffix ? `${formatted} TAO` : formatted;
}

function libraryName(label, category) {
  const name = String(label || category || "未命名").trim();
  return name.endsWith("地址库") ? name : `${name}地址库`;
}

function sourceLabel(value) {
  return ({
    taostats: "Taostats",
    local: "本地索引",
  })[value] || value;
}

function coverageLabel(value) {
  return ({
    TAOSTATS_INDEX: "Taostats 历史索引",
    TAOSTATS_AND_LOCAL: "Taostats + 本地索引",
    LOCAL_INDEX: "本地索引",
    NO_DATA: "无可用数据",
  })[value] || value;
}

function freshnessLabel(value, data) {
  if (value === "EMPTY_INDEX" && data.data_source === "taostats" && data.api_requests > 0) {
    return "接口查询完成 · 0 条转账";
  }
  return ({
    LOCAL_INDEX: "本地索引结果",
    EMPTY_INDEX: "索引为空",
    TAOSTATS_INDEX: "Taostats 查询结果",
    TAOSTATS_PARTIAL: "Taostats 部分结果",
    LOCAL_FALLBACK: "仅本地回退结果",
    TAOSTATS_UNAVAILABLE: "Taostats 暂不可用",
  })[value] || value;
}

function setApiKeyBody(open) {
  $("#api-key-body").classList.toggle("hidden", !open);
  $("#toggle-api-key").textContent = open ? "收起" : "填写 / 修改";
  state.apiKeyBodyOpen = open;
}

function showTaostatsKeyStatus(configured, storage) {
  const status = $("#taostats-status");
  const detail = $("#taostats-key-status");
  if (typeof configured !== "boolean") {
    status.textContent = "后台版本较旧；请重启服务后设置 API key";
    detail.textContent = "设置 API key 前请先更新并重启服务。";
    $("#clear-taostats-key").disabled = true;
    state.taostatsConfigured = null;
    setApiKeyBody(true);
    return;
  }
  state.taostatsConfigured = configured;
  state.taostatsKeyStorage = storage || state.taostatsKeyStorage || "";
  status.textContent = configured ? "API key 已配置" : "尚未设置 API key（无法使用 Taostats 查询）";
  status.classList.toggle("warn-text", !configured);
  if (!configured) {
    detail.textContent = "勾选「使用 Taostats 历史索引」前必须先保存 key，否则查询会被拒绝。";
  } else if (state.taostatsKeyStorage === "dpapi") {
    detail.textContent = "key 已加密保存在本机（Windows DPAPI，绑定当前用户），服务重启后自动恢复，不会回显。";
  } else if (state.taostatsKeyStorage === "plain") {
    detail.textContent = "key 已保存在本机 data\\settings.json（当前系统不支持加密，为明文），服务重启后自动恢复，不会回显。";
  } else {
    detail.textContent = "key 只存在当前进程内存：服务重启后需要重新输入。";
  }
  $("#clear-taostats-key").disabled = !configured;
  setApiKeyBody(!configured);
}

function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show${error ? " error" : ""}`;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { el.className = "toast"; }, 4600);
}

function setTheme(mode, persist = true) {
  if (mode === "warm") document.documentElement.dataset.theme = "eye";
  else if (mode === "darkgray") document.documentElement.dataset.theme = "darkgray";
  else delete document.documentElement.dataset.theme;
  $("#theme-select").value = mode;
  if (persist) {
    try {
      localStorage.setItem("wallet-link-theme", mode);
      localStorage.removeItem("wallet-link-eye-mode");
    }
    catch (_) { /* Preference storage is optional. */ }
  }
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

/* -- 离线授权：未激活时只显示激活页，激活后页面右下角常驻团队水印 ------- */
async function loadLicenseStatus() {
  try {
    return await api("/api/license");
  } catch (_) {
    // 服务不可用时不要误锁界面，健康检查会提示连接问题。
    return { licensed: true, watermark: "" };
  }
}

function applyLicenseStatus(status) {
  const gate = $("#license-gate");
  const mark = $("#license-watermark");
  if (!status.licensed) {
    $("#license-machine").textContent = status.machine || "-";
    $("#license-reason").textContent = status.reason || "工具需要授权码才能使用。";
    gate.classList.remove("hidden");
    document.body.classList.add("license-locked");
    return;
  }
  gate.classList.add("hidden");
  document.body.classList.remove("license-locked");
  if (status.watermark) {
    mark.textContent = status.watermark;
    mark.title = `授权给 ${status.team}${status.expires ? `，到期 ${status.expires}` : "，长期有效"}`;
    mark.hidden = false;
  }
}

async function activateLicense() {
  const button = $("#license-activate");
  const token = $("#license-token").value.trim();
  if (!token) { toast("请先粘贴授权码。", true); return; }
  button.disabled = true;
  button.textContent = "校验中…";
  try {
    const data = await api("/api/license/activate", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    toast(`激活成功：${data.team}。正在重新加载…`);
    window.setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    toast(`激活失败：${error.message}`, true);
    button.disabled = false;
    button.textContent = "激活";
  }
}

function navigate(view, updateHash = true) {
  document.querySelectorAll(".nav").forEach((button) => {
    button.classList.toggle("active", button.dataset.nav === view);
  });
  ["scan", "history", "library", "method", "release"].forEach((name) => {
    $(`#view-${name}`).classList.toggle("hidden", name !== view);
  });
  if (updateHash) {
    // 刷新或分享链接时仍停在同一页
    try {
      if (view === "scan") history.replaceState(null, "", location.pathname);
      else history.replaceState(null, "", `#${view}`);
    } catch (_) { /* 本地文件打开时忽略 */ }
  }
  if (view === "library") loadLibrary();
  if (view === "history") loadHistory();
  if (view === "release") loadVersionInfo();
}

async function loadOverview() {
  try {
    const data = await api("/api/overview");
    const index = data.index;
    $("#health-dot").className = "dot ok";
    $("#health-text").textContent = "扫描服务已连接";
    $("#index-dot").className = "dot ok";
    $("#index-status").textContent = index.transfer_count ? "本地索引可查询" : "本地索引为空，可用 Taostats";
    $("#metric-transfers").textContent = number(index.transfer_count);
    $("#metric-range").textContent = index.first_block
      ? `${number(index.first_block)} – ${number(index.last_block)}`
      : "未采集";
    $("#metric-cursor").textContent = index.cursor_block ? number(index.cursor_block) : "未建立";
    if (!$("#seed").value && data.default_seed) $("#seed").value = data.default_seed;
    showTaostatsKeyStatus(data.taostats_configured, data.taostats_key_storage);
    const usage = data.api_usage || {};
    $("#usage-today").textContent = number(usage.today);
    $("#usage-month").textContent = number(usage.month);
    $("#usage-total").textContent = number(usage.total);
    $("#usage-limit").textContent = usage.monthly_limit == null ? "未设置" : number(usage.monthly_limit);
    $("#usage-remaining").textContent = usage.monthly_remaining == null ? "未设置" : number(usage.monthly_remaining);
    $("#usage-note").textContent = `${usage.counting_scope || "统计本机实际发起的 Taostats 网络请求"}；免费档按 credits 计费，参考上限不等同账号精确剩余次数。`;
    if (index.jobs && index.jobs[0] && index.jobs[0].error) {
      toast(`索引任务失败：${index.jobs[0].error}`, true);
    }
  } catch (error) {
    $("#health-dot").className = "dot bad";
    $("#health-text").textContent = "扫描服务不可用";
    $("#index-dot").className = "dot bad";
    $("#index-status").textContent = "服务不可用";
    toast(error.message, true);
  }
}

function renderReleaseNotes(data) {
  const currentVersion = String(data.version || "").trim();
  $("#app-version").textContent = currentVersion ? `当前版本：${currentVersion}` : "当前版本：未知";
  const entries = Array.isArray(data.entries) ? data.entries : [];
  if (!entries.length) {
    $("#release-list").innerHTML = '<div class="placeholder">暂无更新日志。</div>';
    return;
  }
  $("#release-list").innerHTML = entries.map((entry) => {
    const current = entry.version === currentVersion ? '<span class="tag">当前版本</span>' : "";
    const changes = (entry.changes || []).length
      ? `<ul>${entry.changes.map((change) => `<li>${esc(change)}</li>`).join("")}</ul>`
      : "<p>本版本暂无详细变更说明。</p>";
    return `<article class="release-entry${entry.version === currentVersion ? " current" : ""}"><div class="release-heading"><h3>v${esc(entry.version)} · ${esc(entry.title)}</h3><div class="release-meta">${esc(entry.date)} ${current}</div></div>${changes}</article>`;
  }).join("");
}

async function loadVersionInfo() {
  try {
    renderReleaseNotes(await api("/api/version"));
  } catch (error) {
    $("#app-version").textContent = "当前版本：读取失败";
    $("#release-list").innerHTML = `<div class="placeholder">更新日志加载失败：${esc(error.message)}</div>`;
  }
}

async function saveTaostatsKey() {
  const apiKey = $("#taostats-api-key").value.trim();
  if (!apiKey) { toast("请先填写 Taostats API key。", true); return; }
  const button = $("#save-taostats-key");
  button.disabled = true;
  try {
    const data = await api("/api/settings/taostats", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    });
    $("#taostats-api-key").value = "";
    showTaostatsKeyStatus(data.configured, data.storage);
    setApiKeyBody(false);
    toast(
      data.storage === "dpapi"
        ? "API key 已保存（本机加密存储），重启服务后不用重填。设置已折叠，点「填写 / 修改」可再次展开。"
        : data.storage === "plain"
          ? "API key 已保存到本机 data\\settings.json（明文），重启后不用重填。设置已折叠，点「填写 / 修改」可再次展开。"
          : "API key 已保存到当前服务内存，重启后需要重填。设置已折叠，点「填写 / 修改」可再次展开。",
      false
    );
  } catch (error) {
    toast(`API key 保存失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function clearTaostatsKey() {
  if (!window.confirm("确定清除本机保存的 Taostats API key？\n\n清除后 Taostats 余额/转账查询需要重新填写 key。")) return;
  const button = $("#clear-taostats-key");
  button.disabled = true;
  try {
    const data = await api("/api/settings/taostats", { method: "DELETE" });
    $("#taostats-api-key").value = "";
    showTaostatsKeyStatus(data.configured, data.storage);
    setApiKeyBody(true);
    toast("API key 已从本机清除，Taostats 查询需要重新填写。", false);
  } catch (error) {
    toast(`清除 API key 失败：${error.message}`, true);
    button.disabled = false;
  }
}

// 交易所人工结论：确认 / 否定 / 重置三态都能改，判错了随时可以改回来。
function exchangeDecisionControl(row) {
  const tracked = String(row.library_category || "").toUpperCase() === "EXCHANGE";
  const rejected = tracked && Boolean(row.exchange_rejected);
  const confirmed = tracked && Boolean(row.exchange_confirmed) && !rejected;
  const address = esc(row.address);
  const btn = (decision, text, title, css = "ghost") =>
    `<button class="${css} note-save" data-exchange-decision="${decision}" data-address="${address}" title="${esc(title)}" type="button">${text}</button>`;
  const reset = btn("reset", "重置为待确认", "清除人工结论，回到“疑似交易所（待确认）”");
  if (confirmed) {
    return `<div class="exchange-decision"><span class="tag">已确认交易所</span>${btn("reject", "改为否定", "判错了？改成否定，该地址不再进入交易所过滤")}${reset}</div>`;
  }
  if (rejected) {
    return `<div class="exchange-decision"><span class="tag warn">已否定交易所</span>${btn("confirm", "改为交易所", "判错了？改成确认：刷新后作为关联终点，不再向下扩展", "secondary")}${reset}</div>`;
  }
  if (tracked) {
    return `<div class="exchange-decision"><span class="tag warn">待人工确认</span>${btn("confirm", "确认交易所", "确认后作为关联终点，刷新后不再向下扩展", "secondary")}${btn("reject", "否定交易所", "否定后不会再被列为交易所候选")}</div>`;
  }
  return `<div class="exchange-decision">${btn("confirm", "标为交易所", "公共地址或未录入的交易所？标记后刷新，其下游只经由它连通的地址会自动移除")}</div>`;
}

function relatedTable(rows) {
  if (!rows.length) return '<div class="placeholder">没有达到过滤条件的关联地址。</div>';
  return `<table><thead><tr><th>关联钱包地址</th><th>当前余额 TAO</th><th>层级</th><th>流入 TAO</th><th>流出 TAO</th><th>累计转账 TAO</th><th>地址库</th><th>备注</th></tr></thead><tbody>${rows.map((row) => {
    const role = row.address_role
      ? `<span class="tag role-tag" title="${esc(row.role_reason || "")}">${esc(row.address_role)}</span>`
      : "";
    const library = row.match
      ? `<span class="tag warn">${esc(libraryName(row.library_label, row.library_category))}</span>`
      : "-";
    // 「标为交易所」对所有关联地址开放：公共地址或未录入系统的交易所地址，
    // 标记后其下游只经由它连通的地址会在刷新后自动消失。已下结论的地址也保留
    // 改判入口，避免一次误点后无法纠正。
    const exchangeActions = exchangeDecisionControl(row);
    // 2/3 级关联：展示「路径明细」按钮，展开从起始地址到该地址的逐笔转账。
    const pathButton = (row.path_transfers || []).length > 1
      ? `<button class="ghost note-save" data-path-toggle="${esc(row.address)}" type="button" title="从起始地址到该地址的逐笔转账记录">路径明细</button>`
      : "";
    const removeButton = `<button class="ghost copy-address remove-relation" data-remove-relation="${esc(row.address)}" title="确认非同一人？移除后点「刷新结果」，这条线索的下游地址也会一并消失" type="button">移除关联</button>`;
    return `<tr data-related-address="${esc(row.address)}"><td class="address related-address-cell"><div class="address-main">${addressLink(row.address, shortAddr(row.address))}<button class="ghost copy-address" data-copy-address="${esc(row.address)}" title="复制完整地址" type="button">复制</button>${removeButton}${role}</div></td><td class="amount">${esc(formatTao(row.balance_tao))}</td><td><span class="depth-cell">${row.depth}${pathButton}</span></td><td class="amount">${esc(formatTao(row.incoming_tao))}</td><td class="amount">${esc(formatTao(row.outgoing_tao))}</td><td class="amount">${esc(formatTao(row.total_tao))}</td><td>${library}${exchangeActions}</td><td><div class="note-editor"><textarea data-relation-note-input placeholder="备注此地址" maxlength="2000">${esc(row.library_note || "")}</textarea><button class="ghost note-save" data-save-relation-note type="button">保存备注</button></div></td></tr>`;
  }).join("")}</tbody></table>`;
}

function pathDetailRowHtml(row) {
  const hops = row.path_transfers || [];
  const seedAddress = state.lastResult?.seed || "";
  const body = hops.map((hop, index) => {
    const kind = hop.edge_type === "stake_transfer"
      ? `子网 stake 转移${hop.netuid != null ? ` · 子网 ${hop.netuid}` : ""}`
      : "普通转账";
    // 按「起始地址 → 当前地址」的方向展示：原始转账可能是对方转入，
    // 直接铺 from→to 会让路径看起来从别的地址起步。
    const from = hop.path_from || hop.from_address;
    const to = hop.path_to || hop.to_address;
    const directionTag = hop.direction === "in"
      ? '<span class="tag">对方转入</span>'
      : hop.direction === "out"
        ? '<span class="tag">向外转出</span>'
        : "";
    return `<tr><td>第 ${index + 1} 跳</td><td class="address"><div class="address-main">${addressLink(from, shortAddr(from))}<span class="path-arrow">→</span>${addressLink(to, shortAddr(to))}</div></td><td>${esc(kind)}${directionTag}</td><td class="amount">${esc(formatTao(hop.amount_tao))}</td><td class="mono history-time">${esc(historyTime(hop.timestamp))}</td><td class="mono">${esc(hop.block_number)}</td></tr>`;
  }).join("");
  const seedLabel = seedAddress
    ? `从起始地址 <code>${esc(shortAddr(seedAddress))}</code> 到 <code>${esc(shortAddr(row.address))}</code> 的关联路径`
    : `从起始地址到 <code>${esc(shortAddr(row.address))}</code> 的关联路径`;
  return `<tr class="path-detail-row" data-path-detail="${esc(row.address)}"><td colspan="8"><div class="path-detail"><div class="path-detail-title">${seedLabel}（共 ${hops.length} 笔转账，按路径顺序）</div><table><thead><tr><th>跳数</th><th>方向（起始地址 → 当前地址）</th><th>类型</th><th>金额 TAO</th><th>时间</th><th>区块</th></tr></thead><tbody>${body}</tbody></table></div></td></tr>`;
}

function togglePathDetail(address) {
  const target = document.querySelector(`tr[data-related-address="${CSS.escape(address)}"]`);
  if (!target) return;
  const existing = target.nextElementSibling;
  if (existing && existing.classList.contains("path-detail-row")) {
    existing.remove();
    const button = target.querySelector("[data-path-toggle]");
    if (button) button.textContent = "路径明细";
    return;
  }
  const row = (state.lastResult?.related || []).find((item) => item.address === address);
  if (!row || !(row.path_transfers || []).length) return;
  target.insertAdjacentHTML("afterend", pathDetailRowHtml(row));
  const button = target.querySelector("[data-path-toggle]");
  if (button) button.textContent = "收起明细";
}

// -- 金额筛选：按流入/流出 TAO 过滤掉金额很小的“轻微关联” ------------------
// 只在前端过滤已有结果，不重新查询、不消耗 API；口径与表格里的金额列一致。
const AMOUNT_FILTER_KEY = "wb.relatedAmountFilter";

function loadAmountFilter() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(AMOUNT_FILTER_KEY) || "null");
    if (!saved || typeof saved !== "object") return;
    const modes = ["off", "either", "incoming", "outgoing", "total", "both"];
    if (modes.includes(saved.mode)) state.amountFilter.mode = saved.mode;
    state.amountFilter.min = saved.min === 0 || saved.min ? String(saved.min) : "";
  } catch (error) {
    /* 隐私模式下 localStorage 不可用，保持默认“不限”。 */
  }
}

function saveAmountFilter() {
  try {
    window.localStorage.setItem(AMOUNT_FILTER_KEY, JSON.stringify(state.amountFilter));
  } catch (error) {
    /* 存不了就只在本次会话生效。 */
  }
}

function amountMinValue() {
  const value = Number(String(state.amountFilter.min ?? "").replace(/,/g, "").trim());
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function amountFilterActive() {
  return state.amountFilter.mode !== "off" && amountMinValue() > 0;
}

const AMOUNT_MODE_LABELS = {
  either: "流入或流出",
  incoming: "流入",
  outgoing: "流出",
  total: "累计",
  both: "流入且流出",
};

function amountModeLabel(mode = state.amountFilter.mode) {
  return AMOUNT_MODE_LABELS[mode] || "金额";
}

function rowAmounts(row) {
  const incoming = Number(row?.incoming_tao) || 0;
  const outgoing = Number(row?.outgoing_tao) || 0;
  const rawTotal = Number(row?.total_tao);
  return { incoming, outgoing, total: Number.isFinite(rawTotal) ? rawTotal : incoming + outgoing };
}

// 通过筛选 = 该地址的金额达到阈值（“隐藏低于阈值的轻微关联”）。
function rowPassesAmountFilter(row) {
  if (!amountFilterActive()) return true;
  const min = amountMinValue();
  const { incoming, outgoing, total } = rowAmounts(row);
  switch (state.amountFilter.mode) {
    case "incoming":
      return incoming >= min;
    case "outgoing":
      return outgoing >= min;
    case "total":
      return total >= min;
    case "both":
      return incoming >= min && outgoing >= min;
    case "either":
    default:
      return Math.max(incoming, outgoing) >= min;
  }
}

function syncAmountFilterControls() {
  const mode = $("#related-amount-mode");
  const min = $("#related-amount-min");
  if (mode) mode.value = state.amountFilter.mode;
  if (min) min.value = state.amountFilter.min;
  const reset = $("#related-amount-reset");
  if (reset) reset.disabled = !state.amountFilter.mode || state.amountFilter.mode === "off";
}

const PAGE_SIZES = [10, 20, 50, 100];

try {
  const savedPageSize = Number(window.localStorage.getItem("wb.relatedPageSize"));
  if (PAGE_SIZES.includes(savedPageSize)) state.pageSize = savedPageSize;
} catch (error) {
  /* 隐私模式下 localStorage 不可用时用默认 10 条。 */
}

// 层级 / 手动移除 / 交易所剪枝：不涉及金额的基础可见集合。
function relatedBaseRows() {
  const allRows = state.lastResult?.related || [];
  return allRows.filter((row) => {
    if (state.depthFilter !== "all" && String(row.depth) !== state.depthFilter) return false;
    if (state.removedAddresses.has(row.address)) return false;
    if (state.exchangePruned.has(row.address)) return false;
    return true;
  });
}

function relatedVisibleRows() {
  return relatedBaseRows().filter(rowPassesAmountFilter);
}

function renderAmountFilterHint(total, visible) {
  const hint = $("#related-filter-hint");
  if (!hint) return;
  const hidden = total - visible;
  if (!amountFilterActive()) {
    hint.classList.add("hidden");
    hint.textContent = "";
    return;
  }
  const min = amountMinValue();
  const minText = Number.isInteger(min) ? number(min) : String(min);
  const parts = [`金额筛选：${amountModeLabel()} ≥ ${minText} TAO`];
  parts.push(hidden > 0 ? `已隐藏 ${number(hidden)} 条轻微关联（显示 ${number(visible)} / ${number(total)} 条）` : `当前 ${number(visible)} 条都已达标，没有隐藏`);
  parts.push("只影响表格显示与「导出地址 TXT」，不重新查询");
  hint.textContent = parts.join("；");
  hint.classList.remove("hidden");
}

function refreshRelatedTable() {
  const baseRows = relatedBaseRows();
  const visible = baseRows.filter(rowPassesAmountFilter);
  $("#count-related").textContent = number(visible.length);
  const totalPages = Math.max(1, Math.ceil(visible.length / state.pageSize));
  if (state.page > totalPages) state.page = totalPages;
  if (state.page < 1) state.page = 1;
  const start = (state.page - 1) * state.pageSize;
  $("#related").innerHTML = relatedTable(visible.slice(start, start + state.pageSize));
  renderRelatedPagination(visible.length, totalPages);
  renderAmountFilterHint(baseRows.length, visible.length);
  const removedNote = $("#removed-note");
  if (removedNote) {
    const notes = [];
    if (state.removedAddresses.size) {
      notes.push(`已手动移除 ${state.removedAddresses.size} 个关联地址（点「刷新结果」后，这些地址、与其有转账交集的地址及下游线索都会一并消失）`);
    }
    if (state.exchangePruned.size) {
      notes.push(`已按「标为交易所」隐藏 ${state.exchangePruned.size} 个只经由交易所连通的地址（刷新后由后端确认）`);
    }
    if (notes.length) {
      removedNote.textContent = notes.join("；");
      removedNote.classList.remove("hidden");
    } else {
      removedNote.classList.add("hidden");
      removedNote.textContent = "";
    }
  }
  updateRefreshButton();
}

function renderRelatedPagination(total, totalPages) {
  const box = $("#related-pagination");
  if (!box) return;
  if (!total) {
    box.innerHTML = "";
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const start = (state.page - 1) * state.pageSize + 1;
  const end = Math.min(total, state.page * state.pageSize);
  const sizeOptions = PAGE_SIZES.map((size) =>
    `<option value="${size}"${size === state.pageSize ? " selected" : ""}>${size}</option>`
  ).join("");
  box.innerHTML = `<div class="pagination">
    <div class="pagination-range">第 ${number(start)}–${number(end)} 条，共 ${number(total)} 条</div>
    <label class="pagination-size">每页<select id="related-page-size" aria-label="每页显示条数">${sizeOptions}</select>条</label>
    <div class="pagination-nav">
      <button class="ghost" type="button" data-page-jump="first"${state.page === 1 ? " disabled" : ""}>首页</button>
      <button class="ghost" type="button" data-page-jump="prev"${state.page === 1 ? " disabled" : ""}>上一页</button>
      <span class="pagination-input">第<input id="related-page-input" type="number" min="1" max="${totalPages}" value="${state.page}" aria-label="跳转到第几页">/ ${number(totalPages)} 页</span>
      <button class="ghost" type="button" data-page-jump="next"${state.page === totalPages ? " disabled" : ""}>下一页</button>
      <button class="ghost" type="button" data-page-jump="last"${state.page === totalPages ? " disabled" : ""}>末页</button>
    </div>
  </div>`;
}

function gotoRelatedPage(target) {
  const totalPages = Math.max(1, Math.ceil(relatedVisibleRows().length / state.pageSize));
  let page = target;
  if (target === "first") page = 1;
  else if (target === "last") page = totalPages;
  else if (target === "prev") page = state.page - 1;
  else if (target === "next") page = state.page + 1;
  page = Number(page);
  if (!Number.isFinite(page)) return;
  page = Math.min(totalPages, Math.max(1, Math.trunc(page)));
  if (page === state.page) return;
  state.page = page;
  refreshRelatedTable();
  const panel = $("#related");
  if (panel) panel.scrollIntoView({ block: "nearest" });
}

function setRelatedPageSize(value) {
  const size = Number(value);
  if (!PAGE_SIZES.includes(size)) return;
  state.pageSize = size;
  state.page = 1;
  try {
    window.localStorage.setItem("wb.relatedPageSize", String(size));
  } catch (error) {
    /* 隐私模式下 localStorage 可能不可用，忽略即可。 */
  }
  refreshRelatedTable();
}

function updateRefreshButton() {
  const button = $("#refresh-result");
  if (!button) return;
  const count = state.removedAddresses.size;
  const label = $("#refresh-result-label");
  const text = count ? `刷新结果（已移除 ${count}）` : "刷新结果";
  if (label) label.textContent = text;
  else button.textContent = text;
  button.disabled = !state.lastResult;
  /* 有手动移除时按钮保持高亮呼吸，提示"这里点了才会真的重查"。 */
  button.classList.toggle("is-armed", count > 0);
  button.title = count
    ? `已手动移除 ${count} 个关联地址，点这里按当前设置重新查询`
    : "按当前设置重新查询：已手动移除的地址及其线索下游不会再出现";
}

function removeRelation(address) {
  if (!state.removedAddresses.has(address)) {
    state.removedAddresses.add(address);
  }
  refreshRelatedTable();
  toast("已移除该关联。点「刷新结果」重查后，这条线索下游的地址也会一并消失；重新扫描会恢复全部关联。", false);
}

/* 0 个关联地址时，必须告诉用户"为什么是空表"，否则很容易被当成工具坏了。
   最常见的两种：Taostats API key 没配（只回退了本地索引）、过滤/阈值把结果挡光。 */
function emptyResultReason(data) {
  if ((data.related || []).length) return null;
  const warnings = [...(data.warnings || []), ...(data.limitations || [])].join(" ");
  const keyMissing = /API key/i.test(warnings)
    || (data.data_source === "taostats" && !data.api_requests && data.freshness === "LOCAL_FALLBACK");
  const points = [];
  if (keyMissing) {
    points.push("当前服务没有可用的 Taostats API key，这次查询只读了本地索引。");
  }
  if (data.freshness === "LOCAL_FALLBACK") {
    points.push(`本地索引里没有这个地址的转账记录（本地共 ${number(data.local_transfer_count || 0)} 条转账，覆盖区间有限）。`);
  } else if (data.data_source === "local" && data.freshness === "EMPTY_INDEX") {
    points.push("本地索引是空的：先在「更新链上索引」里扫描最新区块，或勾选「使用 Taostats 历史索引」再查。");
  }
  if (keyMissing || data.freshness === "LOCAL_FALLBACK") {
    points.push("保存 API key（或取消勾选「使用 Taostats 历史索引」只查本地）后重新查询。");
  } else if (data.data_source === "taostats" && !(data.taostats_transfer_count > 0)) {
    points.push("Taostats 返回 0 条转账：这个地址在该数据源里没有可用的转账历史。");
  }
  const filtered = (data.exchange_addresses_filtered || 0) + (data.validator_addresses_filtered || 0);
  if (filtered > 0) {
    points.push(`过滤条件排除了 ${number(filtered)} 个地址（交易所/验证者），可以取消过滤再看一次。`);
  }
  const policy = data.policy || {};
  if (Number(policy.min_edge_tao) > 0 || Number(policy.min_total_tao) > 0) {
    points.push(`当前金额门槛是单边 ≥ ${policy.min_edge_tao} TAO、累计 ≥ ${policy.min_total_tao} TAO，低于门槛的转账不会出现在表里。`);
  }
  if (data.pagination_complete === false) {
    points.push("历史分页没有读完整（额度或服务端窗口限制），可以稍后重查或调高查询轮数。");
  }
  if (!points.length) {
    points.push("数据源里没有符合当前层级/方向/金额条件的转账记录。");
  }
  return { keyMissing, points };
}

function emptyResultSummary(data) {
  const reason = emptyResultReason(data);
  if (!reason) return "";
  return reason.keyMissing ? "未配置 Taostats API key，只查了本地索引" : "数据源里没有符合条件的转账";
}

function focusTaostatsKey(message) {
  toast(message, true);
  const input = $("#taostats-api-key");
  if (!input) return;
  input.scrollIntoView({ behavior: "smooth", block: "center" });
  input.focus();
}

function renderResultAlert(data) {
  const box = $("#result-alert");
  if (!box) return;
  const reason = emptyResultReason(data);
  if (!reason) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = `<div class="alert-title">本次查询没有得到关联地址</div><ul>${reason.points.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>${reason.keyMissing ? '<div class="alert-actions"><button class="secondary" type="button" data-alert-action="key">去填写 Taostats API key</button></div>' : ""}`;
  const action = box.querySelector("[data-alert-action='key']");
  if (action) {
    action.addEventListener("click", () => {
      focusTaostatsKey("粘贴 Taostats API key 后点「保存 API key」，再重新查询。");
    });
  }
}

function renderResult(data) {
  state.lastResult = data;
  $("#empty").classList.add("hidden");
  $("#result").classList.remove("hidden");
  renderResultAlert(data);
  const sourcesLabel = (data.data_sources_used || []).map(sourceLabel).join(" + ") || "无数据来源";
  const apiUsage = data.data_source === "taostats"
    ? ` · API 请求 ${data.api_requests}/${data.api_request_budget ?? "?"} 次（优先一级关联转账） · 返回转账 ${data.taostats_transfer_count} 条 · 缓存命中 ${data.api_cache_hits} 次`
    : "";
  const stakeUsage = data.stake_transfer_count > 0 ? ` · 子网 stake 转移 ${data.stake_transfer_count} 条` : "";
  $("#result-subtitle").textContent = `${data.seed} · 最多 ${data.max_depth} 级 · 来源 ${sourcesLabel}${apiUsage}${stakeUsage} · 数据截止 ${data.data_cutoff_block ? number(data.data_cutoff_block) : "未采集"} 区块`;
  $("#seed-result-address").textContent = data.seed || "-";
  $("#seed-result-balance").textContent = formatTao(data.seed_balance_tao, true);
  const paginationBadge = data.data_source === "taostats" && data.pagination_complete === false
    ? '<span class="badge warn">分页未完整</span>' : "";
  const windowBadge = data.provider_window_truncated
    ? '<span class="badge warn">服务端历史窗口有限</span>' : "";
  $("#result-badges").innerHTML = `<span class="badge ${data.freshness === "LOCAL_INDEX" || data.freshness === "TAOSTATS_INDEX" ? "" : "warn"}">${esc(freshnessLabel(data.freshness, data))}</span><span class="badge">${esc(coverageLabel(data.coverage))}</span>${paginationBadge}${windowBadge}${data.seed_is_in_library ? '<span class="badge warn">起始地址已命中地址库</span>' : ""}`;
  $("#related-depth-filter").value = state.depthFilter;
  syncAmountFilterControls();
  $("#cutoff").textContent = data.data_cutoff_block ? number(data.data_cutoff_block) : "-";
  const seedHit = $("#seed-library-hit");
  const seedMatches = data.seed_library_matches || [];
  if (seedMatches.length) {
    seedHit.classList.remove("hidden");
    $("#seed-library-hit-content").innerHTML = `<div class="library-hit-list">${seedMatches.map((entry) => `<div class="library-hit-item"><b>${esc(libraryName(entry.label, entry.category))}</b><span> · ${esc(entry.category)}${entry.note ? ` · ${esc(entry.note)}` : ""}</span></div>`).join("")}</div>`;
  } else {
    seedHit.classList.add("hidden");
    $("#seed-library-hit-content").textContent = "";
  }
  refreshRelatedTable();
  const libraryHitCount = (data.library_matches || []).length;
  $("#sync-matched").disabled = libraryHitCount === 0;
  // 同步目标默认跟随「命中的标签组」；地址库还没加载时先拉一次再定默认值。
  if (state.library.length) {
    fillSyncGroupSelect();
    applyDefaultSyncGroup();
  } else {
    loadLibrary()
      .then(() => { fillSyncGroupSelect(); applyDefaultSyncGroup(true); })
      .catch(() => {});
  }
  updateBookHint();
  $("#warnings").innerHTML = [...data.warnings, ...data.limitations].map((text) => `<li>${esc(text)}</li>`).join("");
  document.querySelectorAll("[data-use-address]").forEach((button) => button.addEventListener("click", () => {
    $("#seed").value = button.dataset.useAddress;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let activeJob = null;
let jobStopping = false;

const JOB_KIND_LABELS = {
  transfer_shallow: "一级关联转账",
  stake_shallow: "一级子网转移",
  transfer_deep: "二级及更深转账",
  transfer_pages: "转账历史翻页",
  stake_deep: "二级及更深子网转移",
};

function describeRound(counts, labels) {
  const dict = labels || JOB_KIND_LABELS;
  const parts = Object.entries(counts || {})
    .filter(([, count]) => count > 0)
    .map(([key, count]) => `${dict[key] || key} ${count} 个`);
  return parts.join("、");
}

function showJobProgress(state) {
  const box = $("#job-progress");
  const text = $("#job-text");
  const planLine = $("#job-plan");
  if (!state) {
    box.classList.add("hidden");
    planLine.textContent = "";
    jobStopping = false;
    $("#cancel-job").disabled = false;
    $("#cancel-job").textContent = "停止续查";
    return;
  }
  box.classList.remove("hidden");
  const parts = [];
  if (state.status === "running") {
    parts.push(`正在执行第 ${state.rounds_done + 1}/${state.max_rounds} 轮`);
  } else if (state.status === "waiting") {
    parts.push(`已完成第 ${state.rounds_done}/${state.max_rounds} 轮`);
  } else if (state.status === "finalizing") {
    parts.push("查询已结束，正在合成结果（最多约 12 秒）");
  }
  parts.push(`累计请求 ${state.requests_total} 次`);
  if (state.remaining_requests > 0) {
    parts.push(`剩余 ${state.remaining_requests} 个请求（约 ${state.estimated_rounds_left} 轮）`);
  } else {
    parts.push("待查队列已清空");
  }
  parts.push(`已发现 ${state.queued_addresses} 个地址`);
  if (state.status === "waiting" && state.next_round_in > 0) {
    parts.push(`下一轮约 ${state.next_round_in} 秒后`);
  }
  if (state.error) parts.push(state.error);
  if (state.cancel_requested) {
    parts.push("已收到停止指令：当前请求一结束立即出结果（最长约 20 秒）");
  }
  text.textContent = parts.join(" · ");

  const lines = [];
  const last = state.last_round;
  if (last && last.requests > 0) {
    lines.push(`上一轮实际查询：${describeRound(last.counts, state.kind_labels) || "无"}（${last.requests} 次请求，新增 ${last.new_addresses} 个地址 / ${last.new_records} 条记录）`);
  }
  const plan = state.next_round_plan;
  if (plan && plan.requests > 0) {
    lines.push(`下一轮计划查询：${describeRound(plan.counts, state.kind_labels) || "无"}（${plan.requests} 次请求）`);
  }
  planLine.textContent = lines.join("　|　");
}

async function cancelActiveJob() {
  if (!activeJob || jobStopping) return;
  jobStopping = true;
  const button = $("#cancel-job");
  button.disabled = true;
  button.textContent = "正在停止…";
  $("#job-text").textContent = "正在停止：当前请求结束后立即出结果，请稍候…";
  // 停止请求会在后台合成部分结果，可能比轮询慢；这里不阻塞轮询。
  api(`/api/relations/jobs/${activeJob}/cancel`, { method: "POST" }).catch((error) => {
    jobStopping = false;
    button.disabled = false;
    button.textContent = "停止续查";
    toast(error.message, true);
  });
}

async function runRelationJob(payload) {
  const created = await api("/api/relations/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const jobId = created.job_id;
  activeJob = jobId;
  showJobProgress(created);
  let last = created;
  while (activeJob === jobId) {
    if (last.status === "complete") {
      activeJob = null;
      showJobProgress(null);
      renderResult(last.result);
      const libraryHitCount = (last.result.library_matches?.length || 0) + (last.result.seed_library_matches?.length || 0);
      const relatedCount = last.result.related.length;
      if (!relatedCount) {
        toast(`分轮查询完成（${last.rounds_done} 轮、${last.requests_total} 次请求），但没有关联地址：${emptyResultSummary(last.result)}。`, true);
      } else {
        toast(`分轮查询完成：共 ${last.rounds_done} 轮、${last.requests_total} 次请求，${relatedCount} 个关联地址，${libraryHitCount} 个地址库命中。`);
      }
      return;
    }
    if (last.status === "cancelled") {
      activeJob = null;
      showJobProgress(null);
      if (last.result) {
        renderResult(last.result);
        const remaining = last.remaining_requests || 0;
        const summary = `已停止续查：完成 ${last.rounds_done} 轮、${last.requests_total} 次请求，当前得到 ${last.result.related.length} 个关联地址`;
        toast(
          remaining > 0
            ? `${summary}；仍有 ${last.pending_transfer} 个转账明细 / ${last.pending_stake} 个子网请求（共 ${remaining} 个）未查询。`
            : `${summary}；停止时待查队列已清空。`,
          remaining > 0 || !last.result.related.length
        );
      } else {
        toast("已停止续查。", true);
      }
      return;
    }
    if (last.status === "failed") {
      activeJob = null;
      showJobProgress(null);
      toast(last.error || "分轮查询失败。", true);
      return;
    }
    // 停止或合成结果期间加快轮询，让状态尽快切换成结果。
    await sleep(jobStopping || last.status === "finalizing" ? 800 : 3000);
    if (activeJob !== jobId) return;
    try {
      last = await api(`/api/relations/jobs/${jobId}`);
    } catch (error) {
      activeJob = null;
      showJobProgress(null);
      throw error;
    }
    showJobProgress(last);
  }
}

function scanPayload(seed) {
  const direction = $("#direction").value;
  return {
    seed,
    depth: Number($("#depth").value),
    min_edge_tao: $("#min-edge").value,
    min_total_tao: $("#min-total").value,
    include_incoming: direction !== "outgoing",
    include_outgoing: direction !== "incoming",
    data_source: $("#taostats-source").checked ? "taostats" : "local",
    filter_exchanges: $("#filter-exchanges").checked,
    filter_validators: $("#filter-validators").checked,
    filter_systems: $("#filter-systems").checked,
  };
}

async function runRelations(keepRemovals = false) {
  const seed = $("#seed").value.trim();
  if (!seed) { toast("请先输入起始地址。", true); return; }
  if ($("#taostats-source").checked && state.taostatsConfigured === false) {
    focusTaostatsKey("还没保存 Taostats API key：先粘贴 key 并点「保存 API key」；或取消勾选「使用 Taostats 历史索引」只查本地索引。");
    return;
  }
  const button = $("#run-scan");
  button.disabled = true;
  button.textContent = "扫描中…";
  if (!keepRemovals) {
    state.removedAddresses.clear();
    state.exchangePruned.clear();
    state.page = 1;
  }
  try {
    const requestedSource = $("#taostats-source").checked ? "taostats" : "local";
    const payload = scanPayload(seed);
    if (state.removedAddresses.size) {
      payload.removed_addresses = [...state.removedAddresses];
    }
    if (requestedSource === "taostats" && $("#multi-round").checked) {
      const rounds = Math.min(100, Math.max(1, Number($("#max-rounds").value) || 5));
      $("#max-rounds").value = rounds;
      payload.max_rounds = rounds;
      button.textContent = "分轮查询中…";
      await runRelationJob(payload);
      return;
    }
    const data = await api("/api/relations", { method: "POST", body: JSON.stringify(payload) });
    if (requestedSource === "taostats" && data.data_source !== "taostats") {
      toast("当前后台尚未加载 Taostats 接入版本。请在项目 PowerShell 窗口执行 .\\stop.ps1，再执行 .\\start.ps1。", true);
      return;
    }
    renderResult(data);
    const libraryHitCount = data.library_matches.length + (data.seed_library_matches || []).length;
    if (!data.related.length) {
      toast(`查询完成，但没有关联地址：${emptyResultSummary(data)}。原因见结果区顶部提示。`, true);
    } else {
      toast(`查询完成：${data.related.length} 个关联地址，${libraryHitCount} 个地址库命中。${data.pagination_complete === false ? "部分历史尚未读取完，请查看提示。" : ""}`, data.pagination_complete === false);
    }
  } catch (error) {
    if (/API key|Taostats/i.test(error.message || "")) {
      focusTaostatsKey(error.message);
    } else {
      toast(error.message, true);
    }
  } finally {
    activeJob = null;
    showJobProgress(null);
    button.disabled = false;
    button.textContent = "扫描关联";
  }
}

/* 批量补录：0.3.0 之前的结果没有落库，只能按地址重跑一遍再归档。
   地址之间留出间隔，避免 Taostats 每分钟 5 次的上限被连续打满。 */
const backfill = { running: false, stop: false };

function parseBackfillSeeds(text) {
  const seen = new Set();
  const seeds = [];
  (text || "").split(/[\s,;，；、]+/).forEach((raw) => {
    const value = raw.trim();
    if (!value || seen.has(value)) return;
    if (!/^[1-9A-HJ-NP-Za-km-z]{47,48}$/.test(value)) return;
    seen.add(value);
    seeds.push(value);
  });
  return seeds;
}

function renderBackfillProgress(text, error = false) {
  const box = $("#backfill-progress");
  if (!text) { box.classList.add("hidden"); box.textContent = ""; return; }
  box.classList.remove("hidden");
  box.classList.toggle("is-error", error);
  box.textContent = text;
}

async function runBackfill() {
  if (backfill.running) return;
  const seeds = parseBackfillSeeds($("#backfill-seeds").value);
  if (!seeds.length) {
    toast("请先粘贴至少一个有效起始地址（每行一个，48 位 SS58 地址）。", true);
    return;
  }
  const payloadBase = scanPayload("");
  const interval = payloadBase.data_source === "taostats" ? 12000 : 2500;
  backfill.running = true;
  backfill.stop = false;
  $("#start-backfill").disabled = true;
  $("#stop-backfill").disabled = false;
  let ok = 0;
  let failed = 0;
  let related = 0;
  const failures = [];
  try {
    for (let index = 0; index < seeds.length; index += 1) {
      if (backfill.stop) break;
      const seed = seeds[index];
      renderBackfillProgress(
        `正在补录 ${index + 1}/${seeds.length}：${seed.slice(0, 8)}… · 成功 ${ok} / 失败 ${failed}`
      );
      try {
        const data = await api("/api/relations", {
          method: "POST",
          body: JSON.stringify({ ...payloadBase, seed }),
        });
        ok += 1;
        related += data.related.length;
      } catch (error) {
        failed += 1;
        failures.push(`${seed.slice(0, 8)}… ${error.message}`);
      }
      renderBackfillProgress(
        `已完成 ${index + 1}/${seeds.length} · 成功 ${ok} / 失败 ${failed} · 累计 ${related} 个关联地址`
      );
      if (index < seeds.length - 1 && !backfill.stop) await sleep(interval);
    }
    await loadHistory(0);
    const stopped = backfill.stop && ok + failed < seeds.length;
    const summary = `${stopped ? "已停止补录" : "补录完成"}：成功 ${ok} 个、失败 ${failed} 个，累计 ${related} 个关联地址，结果已写入查询记录。`;
    renderBackfillProgress(failures.length ? `${summary}\n失败明细：${failures.slice(0, 5).join("；")}` : summary, failed > 0);
    toast(summary, failed > 0 && !ok);
  } finally {
    backfill.running = false;
    $("#start-backfill").disabled = false;
    $("#stop-backfill").disabled = true;
  }
}

async function scanBlocks(advance) {
  const buttons = [$("#scan-latest"), $("#scan-advance")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const result = await api(`/api/scan?blocks=${encodeURIComponent($("#scan-blocks").value)}&advance=${advance}`, { method: "POST" });
    toast(`扫描完成：${result.scanned_blocks} 块，新增 ${result.transfer_count} 条转账。`);
    await loadOverview();
  } catch (error) {
    toast(`扫描失败：${error.message}`, true);
    await loadOverview();
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function taostatsAccountUrl(address) {
  return `https://taostats.io/account/${encodeURIComponent(address)}`;
}

function addressLink(address, text) {
  return `<a class="addr-link" href="${esc(taostatsAccountUrl(address))}" target="_blank" rel="noopener" title="在 Taostats 查看该地址详情（余额、质押、交易）">${esc(text || address)}</a>`;
}

function libraryRowsTable(rows) {
  const rowsHtml = rows.map((row) => {
    const exchangeControl = row.category === "EXCHANGE"
      ? `<div class="exchange-confirm-control"><span class="tag ${row.exchange_confirmed ? "" : "warn"}">${row.exchange_confirmed ? "已人工确认" : row.exchange_rejected ? "已人工否定" : "待人工确认"}</span>${row.exchange_confirmed ? `<button class="ghost note-save" data-library-exchange-decision="reject" data-row-id="${row.id}" title="判错了？改成否定，该地址不再进入交易所过滤" type="button">改为否定</button><button class="ghost note-save" data-library-exchange-decision="reset" data-row-id="${row.id}" title="清除人工结论，回到待确认" type="button">重置</button>` : row.exchange_rejected ? `<button class="secondary note-save" data-library-exchange-decision="confirm" data-row-id="${row.id}" title="判错了？改成确认为交易所" type="button">改为交易所</button><button class="ghost note-save" data-library-exchange-decision="reset" data-row-id="${row.id}" title="清除人工结论，回到待确认" type="button">重置</button>` : `<button class="secondary note-save" data-library-exchange-decision="confirm" data-row-id="${row.id}" type="button">确认交易所</button><button class="ghost note-save" data-library-exchange-decision="reject" data-row-id="${row.id}" type="button">否定交易所</button>`}</div>`
      : "";
    const bookCell = row.book ? `<span class="tag">${esc(row.book)}</span>` : "-";
    const categories = (row.categories && row.categories.length ? row.categories : [row.category]).filter(Boolean);
    // 「交易所（系统）/验证者（系统）」标签本身已写明系统分类，避免再并列一个「系统」标签。
    const hasSystemRole = categories.includes("EXCHANGE") || categories.includes("VALIDATOR");
    const displayCategories = categories.filter((name) => !(hasSystemRole && (name === "系统" || name === "SYSTEM")));
    const chips = displayCategories.map((name) =>
      `<span class="lib-category" data-category="${esc(name)}">${esc(categoryLabel(name))}</span>`
    ).join("");
    // 标签是「组」的属性：单条地址只维护备注，改名在组标题上做。
    const labelCell = `<span class="label-text${row.label_custom ? "" : " is-default"}" title="${row.label_custom ? "组名（整组共用）" : `未命名，默认显示地址后四位（${esc(row.label)}）`}">${esc(row.label || "-")}</span>`;
    // 分类是「组」的属性：单条地址不单独改分类，改分类在组标题上做。
    const editorRow = "";
    return `<tr>
      <td class="selection-cell"><input type="checkbox" data-library-select value="${row.id}" aria-label="选择第 ${row.id} 条地址"></td>
      <td class="mono address library-address-cell"><div class="address-main">${addressLink(row.address, row.address)}<button class="ghost copy-address" data-copy-address="${esc(row.address)}" title="复制完整地址" type="button">复制</button></div></td>
      <td class="library-label-cell">${labelCell}</td>
      <td><div class="category-chips">${chips}</div>${exchangeControl}</td>
      <td><div class="library-note-editor"><textarea data-library-note-input="${row.id}" maxlength="2000" placeholder="添加备注">${esc(row.note || "")}</textarea><button class="ghost note-save" data-save-library-note="${row.id}" type="button">保存</button></div></td>
      <td>${bookCell}</td><td>${esc(row.source || "-")}</td>
      <td><button class="danger" data-delete-address="${row.id}" type="button">删除</button></td>
    </tr>${editorRow}`;
  }).join("");
  // 包一层 .table-wrap：列多时（地址 + 复制按钮）可横向滚动，避免名称/来源被挤成竖排。
  return `<div class="table-wrap"><table><thead><tr><th class="selection-cell"></th><th>地址</th><th>名称</th><th>分类</th><th>备注</th><th>地址库</th><th>来源</th><th></th></tr></thead><tbody>${rowsHtml}</tbody></table></div>`;
}

function libraryTable(rows) {
  if (!rows.length) {
    return state.bookFilter
      ? `<div class="placeholder">地址库「${esc(state.bookFilter)}」里没有地址。</div>`
      : '<div class="placeholder">地址库为空。可通过上方单个添加或导入 TXT/CSV/JSON。</div>';
  }
  const groups = new Map();
  rows.forEach((row) => {
    // 默认标签是地址后四位，逐条都不同；未人工命名的地址仍按分类归组，
    // 避免地址库清单被拆成上百个单条分组。
    const customLabel = Boolean(row.label_custom);
    const label = (customLabel ? row.label : `${categoryLabel(row.category)} · 未命名`).trim();
    // 标签组按「组名」归并：同一组的地址分类可以不同（分类是地址属性，不是组属性）。
    const key = row.book
      ? `book\u0000${row.book}`
      : customLabel
        ? `label\u0000${label}`
        : `category\u0000${row.category}`;
    if (!groups.has(key)) {
      groups.set(key, {
        label: row.book || label,
        book: row.book || "",
        categories: new Set(),
        // 组的主分类（第一条记录的主分类）：整组改分类时默认只勾它，
        // 避免“把 其他 换成 群友”时忘了取消而变成多分类。
        primaryCategory: row.category,
        // 只有「自建库」和人工命名的标签组可以整组改名；未命名分类组不动。
        canRename: Boolean(row.book || customLabel),
        rows: [],
      });
    }
    groups.get(key).categories.add(row.category);
    groups.get(key).rows.push(row);
  });
  return `<div class="library-groups">${[...groups.values()].map((group, index) => {
    const groupId = `library-group-${index}`;
    const expanded = state.libraryGroupsOpen.has(groupId);
    const groupCategories = [...group.categories];
    const categoryTag = group.book
      ? '<span class="lib-category book-tag">自建库</span>'
      : groupCategories.length === 1
        ? `<span class="lib-category" data-category="${esc(groupCategories[0])}">${esc(categoryLabel(groupCategories[0]))}</span>`
        : '<span class="lib-category">多分类</span>';
    const kind = group.book ? "book" : "label";
    const renaming = Boolean(state.groupRename)
      && state.groupRename.kind === kind
      && state.groupRename.name === group.label;
    let renameControl = "";
    let categoryControl = "";
    if (renaming) {
      renameControl = `<div class="group-rename-editor"><input data-group-rename-input maxlength="160" value="${esc(group.label)}" autocomplete="off" aria-label="新的组名"><button class="secondary note-save" data-save-group-rename data-group-kind="${kind}" data-group-name="${esc(group.label)}" type="button">保存</button><button class="ghost note-save" data-cancel-group-rename type="button">取消</button></div>`;
    } else if (group.canRename) {
      renameControl = `<button class="ghost note-save group-rename" data-rename-group data-group-kind="${kind}" data-group-name="${esc(group.label)}" title="整组改名：这一组的所有地址一起更新" type="button">改名</button>`;
    }
    // 整组改分类：分类是「组」的属性，单条地址不再单独改分类。
    const editingCategory = Boolean(state.groupCategory)
      && state.groupCategory.kind === kind
      && state.groupCategory.name === group.label;
    // 只有真正的「组」（自建库 / 人工命名的标签组）能整组改分类；
    // 未命名地址是按分类兜底归到一起的，分类本身就是它的分组依据。
    let categoryEditor = "";
    if (!group.canRename) categoryControl = "";
    else if (editingCategory) {
      // 编辑器单独占一行，避免把组标题挤变形。
      categoryEditor = `<div class="group-category-editor"><div class="group-category-title">整组改分类：勾一个就是整组替换成该分类，勾多个表示同时属于多个分类；交易所/验证者地址会跳过</div><div class="category-editor-choices">${state.categories.map((item) =>
        `<label><input type="checkbox" data-group-category-choice value="${esc(item.name)}"${item.name === group.primaryCategory ? " checked" : ""}>${esc(categoryLabel(item.name))}</label>`
      ).join("")}</div><div class="category-editor-actions"><button class="secondary note-save" data-save-group-categories data-group-kind="${kind}" data-group-name="${esc(group.label)}" type="button">保存分类</button><button class="ghost note-save" data-cancel-group-categories type="button">取消</button></div></div>`;
    } else {
      categoryControl = `<button class="ghost note-save group-category" data-edit-group-categories data-group-kind="${kind}" data-group-name="${esc(group.label)}" data-group-book="${esc(group.book)}" title="整组改分类：这一组的所有地址一起更新" type="button">改分类</button>`;
    }
    // 组内手动添加地址：展开明细后就能用，不用先导出再导入。
    const addingHere = Boolean(state.groupAdd)
      && state.groupAdd.kind === kind
      && state.groupAdd.name === group.label;
    const addAttrs = `data-group-kind="${kind}" data-group-name="${esc(group.label)}" data-group-book="${esc(group.book)}" data-group-category="${esc(group.primaryCategory || "")}" data-group-can-rename="${group.canRename ? "1" : "0"}"`;
    const addBar = `<div class="group-add-bar"><button class="ghost" data-group-add ${addAttrs} type="button">＋ 手动添加地址</button><small>加到「${esc(group.label)}」；重复地址会提示，可勾选“移过来”把它从别的组转到本组</small></div>`;
    const addEditor = addingHere
      ? `<div class="group-add-editor"><textarea data-group-add-input rows="3" placeholder="每行一个 SS58 地址，也支持逗号 / 空格分隔（一次最多 200 个）" aria-label="要添加的地址"></textarea><input data-group-add-note maxlength="2000" autocomplete="off" placeholder="备注（选填，只写给这次新加的地址）" aria-label="新地址的备注"><label class="group-add-move"><input type="checkbox" data-group-add-move> 已在别的组的地址也移到本组（改它的组名/分类，备注保留）</label><div class="group-add-actions"><button class="secondary note-save" data-save-group-add ${addAttrs} type="button">添加到本组</button><button class="ghost note-save" data-cancel-group-add type="button">取消</button></div><small>地址格式不对会逐个提示；加入后可点「撤回上次导入」一次性撤销这次添加。${isExchangeGroupLabel(group.label) ? "本组是人工交易所结论组，加进来的地址会同步标为" + esc(group.label) + "。" : ""}</small></div>`
      : "";
    return `<section class="library-group"><div class="library-group-head"><button class="library-group-toggle" data-library-toggle="${groupId}" aria-expanded="${expanded}" type="button"><span><b>${esc(group.label)}</b>${categoryTag}</span><span>${group.rows.length} 个地址 <span class="group-chevron">⌄</span></span></button><div class="library-group-actions"><label class="group-select" title="全选「${esc(group.label)}」这一组的 ${group.rows.length} 个地址"><input type="checkbox" data-group-select="${groupId}" aria-label="全选「${esc(group.label)}」组地址">全选本组</label><span class="group-selected" data-group-selected="${groupId}"></span>${categoryControl}${renameControl}</div></div>${categoryEditor}<div class="library-group-details${expanded ? "" : " hidden"}" data-library-details="${groupId}">${addBar}${addEditor}${libraryRowsTable(group.rows)}</div></section>`;
  }).join("")}</div>`;
}

function updateLibrarySelection() {
  const checkboxes = [...document.querySelectorAll("[data-library-select]")];
  const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
  const button = $("#delete-selected");
  button.disabled = selected === 0;
  button.textContent = `删除所选 (${selected})`;

  const selectAll = $("#select-all-addresses");
  if (selectAll) {
    selectAll.checked = checkboxes.length > 0 && selected === checkboxes.length;
    selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
  }
  // 每个标签组自己的「全选本组」：全选 / 部分选中（半选）/ 未选。
  document.querySelectorAll("[data-library-details]").forEach((container) => {
    const groupId = container.dataset.libraryDetails;
    const boxes = [...container.querySelectorAll("[data-library-select]")];
    const checked = boxes.filter((checkbox) => checkbox.checked).length;
    const toggle = document.querySelector(`[data-group-select="${groupId}"]`);
    if (toggle) {
      toggle.checked = boxes.length > 0 && checked === boxes.length;
      toggle.indeterminate = checked > 0 && checked < boxes.length;
    }
    const counter = document.querySelector(`[data-group-selected="${groupId}"]`);
    if (counter) counter.textContent = checked ? `已选 ${checked}` : "";
  });
}

function renderLibraryList() {
  const rows = state.bookFilter
    ? state.library.filter((row) => (row.book || "") === state.bookFilter)
    : state.library;
  const total = state.library.length;
  $("#library-count").textContent = state.bookFilter
    ? `${rows.length} / ${total} 条`
    : `${total} 条`;
  $("#library-table").innerHTML = libraryTable(rows);
  renderMergePanel();
  updateLibrarySelection();
}

// -- 合并群组 --------------------------------------------------------------
// 扫描同一个种子时，同一批地址可能被分到几个库里（交叉命中），其实是一伙人。
// 合并后统一挂到一个库下，分类/备注/交易所结论都保留，可撤回。
function mergeableGroups() {
  const map = new Map();
  state.library.forEach((row) => {
    // 未命名的分类兜底组没有稳定组名，不参与合并，避免误并。
    if (!row.book && !row.label_custom) return;
    const kind = row.book ? "book" : "label";
    const name = row.book || row.label;
    const key = `${kind}\u0000${name}`;
    if (!map.has(key)) map.set(key, { kind, name, count: 0, categories: new Set() });
    const group = map.get(key);
    group.count += 1;
    group.categories.add(row.category);
  });
  return [...map.values()].sort((a, b) => b.count - a.count);
}

function mergeKey(kind, name) {
  // 不能拿 \u0000 当分隔符：它写进 HTML 属性会被浏览器替换掉，勾选状态就对不上了。
  return `${kind}:${name}`;
}

function mergePickedGroups() {
  return mergeableGroups().filter((group) => state.merge.picked.has(mergeKey(group.kind, group.name)));
}

function renderMergePanel() {
  const panel = $("#merge-panel");
  if (!panel) return;
  if (!state.merge.open) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  panel.classList.remove("hidden");
  const groups = mergeableGroups();
  // 已经不存在（被删/被并）的勾选要清掉，避免提交时找不到组。
  state.merge.picked = new Set(
    [...state.merge.picked].filter((key) => groups.some((item) => mergeKey(item.kind, item.name) === key))
  );
  const picked = mergePickedGroups();
  const total = picked.reduce((sum, item) => sum + item.count, 0);
  const inheritName = state.merge.inheritName || "";
  panel.innerHTML = `<div class="merge-head"><div><b>合并群组</b><small>勾选两个以上的地址库 / 标签组，合并成一个库；分类、备注、交易所人工结论都保留，合并后可「撤回上次导入」还原。</small></div><button class="ghost" data-merge-cancel type="button">收起</button></div>
    <div class="merge-groups">${groups.map((group) => `<label class="merge-group"><input type="checkbox" data-merge-pick="${esc(mergeKey(group.kind, group.name))}"${state.merge.picked.has(mergeKey(group.kind, group.name)) ? " checked" : ""}><span class="merge-group-name">${esc(group.name)}</span><span class="merge-group-kind">${group.kind === "book" ? "自建库" : "标签组"}</span><span class="merge-group-count">${group.count} 个地址</span></label>`).join("") || '<div class="placeholder">还没有可合并的地址库或标签组。</div>'}</div>
    <div class="merge-name-row">
      <label class="merge-mode"><input type="radio" name="merge-mode" value="new"${state.merge.mode === "new" ? " checked" : ""}> 起个新名字</label>
      <input data-merge-name maxlength="60" autocomplete="off" placeholder="合并后的组名" value="${esc(state.merge.target || "")}"${state.merge.mode === "inherit" ? " disabled" : ""}>
      <label class="merge-mode"><input type="radio" name="merge-mode" value="inherit"${state.merge.mode === "inherit" ? " checked" : ""}> 沿用已有组名</label>
      <select data-merge-inherit aria-label="沿用哪个组名"${state.merge.mode === "inherit" && picked.length ? "" : " disabled"}>${picked.map((group) => `<option value="${esc(group.name)}"${group.name === inheritName ? " selected" : ""}>${esc(group.name)}</option>`).join("") || '<option value="">先勾选组</option>'}</select>
    </div>
    <div class="merge-actions"><button class="secondary note-save" data-merge-apply type="button"${picked.length < 2 ? " disabled" : ""}>合并${picked.length >= 2 ? `（${picked.length} 组 / ${total} 个地址）` : ""}</button><small data-merge-summary>${picked.length < 2 ? "至少勾选两个组" : `已选 ${picked.length} 组，共 ${total} 个地址`}</small></div>`;
}

function refreshMergePanelState() {
  // 勾选只刷新计数/按钮/下拉选项，不重建整块：输入框内容和滚动位置都不该被勾选打断。
  const panel = $("#merge-panel");
  if (!panel) return;
  const picked = mergePickedGroups();
  const total = picked.reduce((sum, item) => sum + item.count, 0);
  const apply = panel.querySelector("[data-merge-apply]");
  if (apply) {
    apply.disabled = picked.length < 2;
    apply.textContent = picked.length >= 2 ? `合并（${picked.length} 组 / ${total} 个地址）` : "合并";
  }
  const summary = panel.querySelector("[data-merge-summary]");
  if (summary) {
    summary.textContent = picked.length < 2 ? "至少勾选两个组" : `已选 ${picked.length} 组，共 ${total} 个地址`;
  }
  const select = panel.querySelector("[data-merge-inherit]");
  if (select) {
    const current = select.value;
    select.disabled = picked.length === 0;
    select.innerHTML = picked.map((item) => `<option value="${esc(item.name)}">${esc(item.name)}</option>`).join("")
      || '<option value="">先勾选组</option>';
    if (picked.some((item) => item.name === current)) select.value = current;
  }
}

function toggleMergePanel() {
  state.merge.open = !state.merge.open;
  if (state.merge.open) {
    state.merge.picked = new Set();
    state.merge.mode = "new";
    state.merge.target = "";
    state.merge.inheritName = "";
  }
  renderMergePanel();
  if (state.merge.open) $("[data-merge-name]")?.focus();
}

async function applyMerge(button) {
  const picked = mergePickedGroups();
  if (picked.length < 2) {
    toast("至少勾选两个组才能合并。", true);
    return;
  }
  const mode = state.merge.mode === "inherit" ? "inherit" : "new";
  const target = mode === "inherit"
    ? String($("[data-merge-inherit]")?.value || "").trim()
    : String($("[data-merge-name]")?.value || "").trim();
  if (!target) {
    toast("给合并后的组起个名字，或改成“沿用已有组名”。", true);
    return;
  }
  const total = picked.reduce((sum, item) => sum + item.count, 0);
  const names = picked.map((item) => `「${item.name}」`).join("、");
  if (!window.confirm(`合并 ${picked.length} 个组到「${target}」？\n\n来源：${names}\n合计 ${total} 个地址，合并后统一归到「${target}」这个库下。\n分类、备注、交易所人工结论都保留；合并后可用「撤回上次导入」还原。`)) return;
  button.disabled = true;
  try {
    const result = await api("/api/library/merge-groups", {
      method: "POST",
      body: JSON.stringify({
        sources: picked.map((item) => ({ kind: item.kind, name: item.name })),
        target,
      }),
    });
    state.merge = { open: false, picked: new Set(), mode: "new" };
    await loadLibrary();
    toast(`已把 ${result.groups} 个组合并成「${result.name}」，共 ${result.merged} 个地址。`);
  } catch (error) {
    toast(`合并失败：${error.message}`, true);
    button.disabled = false;
  }
}

function bookCard(book) {
  const categories = Object.entries(book.categories || {})
    .map(([key, count]) => `${categoryLabel(key)} ${count}`)
    .join(" / ");
  const meta = [
    book.seed ? `来源查询 ${shortAddr(book.seed)}` : "手工创建",
    categories,
    book.created_at ? historyTime(book.created_at) : "",
  ].filter(Boolean).join(" · ");
  return `<article class="book-card">
    <div class="book-card-head"><b title="${esc(book.name)}">${esc(book.name)}</b><span class="tag">${number(book.count)} 个地址</span></div>
    <small>${esc(meta)}</small>
    <div class="book-card-actions">
      <button class="ghost note-save" data-book-view="${esc(book.name)}" type="button">只看此库</button>
      <button class="ghost note-save" data-book-export="txt" data-book-name="${esc(book.name)}" type="button">导出 TXT</button>
      <button class="ghost note-save" data-book-export="csv" data-book-name="${esc(book.name)}" type="button">导出 CSV</button>
      <button class="danger note-save" data-book-dissolve="${esc(book.name)}" type="button">解散</button>
    </div>
  </article>`;
}

function renderBooks() {
  const list = $("#book-list");
  if (!list) return;
  $("#book-count").textContent = `${state.books.length} 个`;
  list.innerHTML = state.books.length
    ? state.books.map(bookCard).join("")
    : '<div class="placeholder">还没有自建地址库。完成一次关联查询后，在结果区填写库名即可创建。</div>';
}

function renderBookFilter() {
  const select = $("#book-filter");
  if (!select) return;
  select.innerHTML = '<option value="">全部地址库</option>' + state.books
    .map((book) => `<option value="${esc(book.name)}">${esc(book.name)}（${number(book.count)}）</option>`)
    .join("");
  if (state.books.some((book) => book.name === state.bookFilter)) select.value = state.bookFilter;
  else {
    state.bookFilter = "";
    select.value = "";
  }
}

function selectBook(name) {
  state.bookFilter = name || "";
  renderBookFilter();
  renderLibraryList();
  if (state.bookFilter) toast(`只看地址库「${state.bookFilter}」。`);
}

async function exportBook(name, format) {
  try {
    const response = await fetch(`/api/library/books/${encodeURIComponent(name)}/export?format=${encodeURIComponent(format)}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `导出失败 (${response.status})`);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${name}.${format}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    toast(`地址库「${name}」已导出为 ${format.toUpperCase()}。`);
  } catch (error) {
    toast(`地址库导出失败：${error.message}`, true);
  }
}

async function dissolveBook(name) {
  const book = state.books.find((item) => item.name === name);
  const count = book ? book.count : 0;
  if (!window.confirm(`确定解散地址库「${name}」？\n\n库里的 ${count} 个地址会一起从已知地址库删除，清单里不再显示。\n误删可以点「撤回上次导入」连库带地址恢复。`)) return;
  try {
    const result = await api(`/api/library/books/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (state.bookFilter === name) state.bookFilter = "";
    await loadLibrary();
    toast(`已解散地址库「${name}」，删除 ${result.deleted} 个地址（可用「撤回上次导入」恢复）。`);
  } catch (error) {
    toast(`解散失败：${error.message}`, true);
  }
}

// -- 地址库分类：大V / 群友 / 其他 + 自建 ---------------------------------
const NEW_CATEGORY = "__new__";

function categoryLabel(value) {
  return ({ EXCHANGE: "交易所（系统）", VALIDATOR: "验证者（系统）", SYSTEM: "系统" })[value] || value || "未分类";
}

function categoryOptionsMarkup(selected) {
  const options = state.categories.map((item) => {
    const suffix = item.count ? ` · ${number(item.count)}` : "";
    const isSelected = item.name === selected ? " selected" : "";
    return `<option value="${esc(item.name)}"${isSelected}>${esc(item.name)}${suffix}</option>`;
  });
  return options.join("") + `<option value="${NEW_CATEGORY}">＋ 新建分类…</option>`;
}

function fillCategorySelects() {
  const names = state.categories.map((item) => item.name);
  document.querySelectorAll("[data-category-select]").forEach((select) => {
    const keep = names.includes(select.value) ? select.value : names[0] || "";
    select.innerHTML = categoryOptionsMarkup(keep);
  });
}

function renderCategories() {
  const list = $("#category-list");
  if (!list) return;
  $("#category-count").textContent = `${state.categories.length} 个`;
  list.innerHTML = state.categories
    .map((item) => `<div class="category-item">
      <div><b>${esc(item.name)}</b><small>${number(item.count)} 个地址</small></div>
      ${item.builtin
        ? '<span class="tag">预置</span>'
        : `<button class="danger note-save" data-category-delete="${esc(item.name)}" type="button">删除</button>`}
    </div>`)
    .join("");
}

async function loadCategories() {
  try {
    const data = await api("/api/library/categories");
    state.categories = data.items || [];
    fillCategorySelects();
    renderCategories();
  } catch (_) {
    // 分类列表拉不到不影响主流程，下拉保留原有选项。
  }
}

async function createCategory(name) {
  const value = String(name || "").trim();
  if (!value) return "";
  try {
    const result = await api("/api/library/categories", {
      method: "POST",
      body: JSON.stringify({ name: value }),
    });
    await loadCategories();
    return result.name;
  } catch (error) {
    toast(`新增分类失败：${error.message}`, true);
    return "";
  }
}

async function promptNewCategory(select) {
  const name = window.prompt("新分类名称（最多 12 个字），例如：项目方、做市商", "");
  const created = await createCategory(name);
  if (created && select) {
    select.value = created;
    toast(`已新增分类「${created}」。`);
    if (select.id === "book-category") updateBookHint();
  } else {
    fillCategorySelects();
  }
}

async function deleteCategory(name) {
  const item = state.categories.find((entry) => entry.name === name);
  const count = item && item.count ? `（当前 ${item.count} 个地址）` : "";
  if (!window.confirm(`删除分类「${name}」${count}？\n\n该分类下的地址会回到“其他”，地址本身不会被删除。\n注意：删除分类只改分类归属，不影响地址库里的地址。`)) return;
  try {
    const result = await api(`/api/library/categories/${encodeURIComponent(name)}`, { method: "DELETE" });
    await loadCategories();
    await loadLibrary();
    toast(`已删除分类「${name}」，${result.moved_to_default} 个地址回到“其他”。`);
  } catch (error) {
    toast(`删除分类失败：${error.message}`, true);
  }
}

async function addCategoryFromPanel() {
  const input = $("#new-category-name");
  const name = input.value.trim();
  if (!name) { toast("请先填写分类名称。", true); input.focus(); return; }
  const created = await createCategory(name);
  if (created) {
    input.value = "";
    toast(`已新增分类「${created}」。`);
  }
}

async function loadLibrary() {
  try {
    if (!state.categories.length) await loadCategories();
    const [data, bookData, batch, seedInfo] = await Promise.all([
      api("/api/library"),
      api("/api/library/books").catch(() => ({ items: [] })),
      api("/api/library/last-import").catch(() => ({ item: null })),
      api("/api/library/seed-books").catch(() => ({ groups: 0, addresses: 0 })),
    ]);
    state.library = data.items;
    state.books = bookData.items || [];
    state.lastImport = batch.item;
    state.seedInfo = { groups: seedInfo.groups || 0, addresses: seedInfo.addresses || 0 };
    renderBooks();
    renderBookFilter();
    renderLibraryList();
    fillSyncGroupSelect();
    updateLibrarySelection();
    updateUndoButton();
    updateSeedButton();
  } catch (error) { toast(error.message, true); }
}

// -- 删除前的二次确认：把「要删的是什么」写清楚，避免误点 ------------------
function libraryGroupOf(row) {
  if (row.book) return row.book;
  if (row.label_custom) return row.label;
  return categoryLabel(row.category);
}

// 批量删除时按组汇总（自建库 / 标签组 / 分类兜底组），让人看清删的是哪几组。
function selectionSummary(ids) {
  const wanted = new Set(ids.map((id) => String(id)));
  const groups = new Map();
  state.library.forEach((row) => {
    if (!wanted.has(String(row.id))) return;
    const key = libraryGroupOf(row);
    groups.set(key, (groups.get(key) || 0) + 1);
  });
  const ordered = [...groups.entries()].sort((a, b) => b[1] - a[1]);
  const shown = ordered.slice(0, 6);
  const lines = shown.map(([name, count]) => `· ${name} ${count} 条`);
  const restCount = ordered.length - shown.length;
  if (restCount > 0) {
    const restRows = ordered.slice(6).reduce((sum, [, count]) => sum + count, 0);
    lines.push(`· 其余 ${restCount} 个组共 ${restRows} 条`);
  }
  return { groups: ordered.length, lines };
}

function deleteAddressConfirmText(row) {
  const lines = ["确定删除这条地址库记录？", "", `地址：${shortAddr(row.address)}`];
  if (row.label) {
    lines.push(`名称：${row.label}${row.book ? `（地址库「${row.book}」）` : ""}`);
  } else if (row.book) {
    lines.push(`地址库：${row.book}`);
  }
  const categories = (row.categories && row.categories.length ? row.categories : [row.category]).filter(Boolean);
  if (categories.length) lines.push(`分类：${categories.map(categoryLabel).join("、")}`);
  const note = String(row.note || "").trim();
  if (note) lines.push(`备注：${note.length > 40 ? `${note.slice(0, 40)}…` : note}`);
  lines.push("", "单条删除不可撤回（只有「解散地址库」能撤回）。");
  return lines.join("\n");
}

async function deleteLibraryAddress(rowId) {
  const row = state.library.find((item) => String(item.id) === String(rowId));
  const text = row ? deleteAddressConfirmText(row) : "确定删除这条地址库记录？此操作不可撤回。";
  if (!window.confirm(text)) return;
  try {
    await api(`/api/library/${rowId}`, { method: "DELETE" });
    await loadLibrary();
    toast("地址已删除。");
  } catch (error) { toast(error.message, true); }
}

async function saveLibraryNote(rowId, button) {
  const row = button.closest("tr");
  const input = row?.querySelector(`[data-library-note-input="${rowId}"]`);
  if (!input) return;
  button.disabled = true;
  try {
    await api(`/api/library/${rowId}`, {
      method: "PATCH",
      body: JSON.stringify({ note: input.value }),
    });
    const item = state.library.find((entry) => String(entry.id) === String(rowId));
    if (item) item.note = input.value;
    button.textContent = "已保存";
    window.setTimeout(() => { button.textContent = "保存"; }, 1400);
  } catch (error) { toast(`备注保存失败：${error.message}`, true); }
  finally { button.disabled = false; }
}

function startGroupRename(kind, name) {
  state.groupRename = { kind, name };
  renderLibraryList();
  const input = document.querySelector("[data-group-rename-input]");
  if (input) {
    input.focus();
    input.select();
  }
}

async function saveGroupRename(button) {
  const kind = button.dataset.groupKind;
  const oldName = button.dataset.groupName;
  const input = document.querySelector("[data-group-rename-input]");
  const value = (input?.value || "").trim();
  if (!value) { toast("组名不能为空；不想改名就点「取消」。", true); return; }
  if (value === oldName) { state.groupRename = null; renderLibraryList(); return; }
  button.disabled = true;
  try {
    if (kind === "book") {
      await api(`/api/library/books/${encodeURIComponent(oldName)}/rename`, {
        method: "POST",
        body: JSON.stringify({ name: value }),
      });
    } else {
      await api("/api/library/rename-label", {
        method: "POST",
        body: JSON.stringify({ old_label: oldName, new_label: value }),
      });
    }
    state.groupRename = null;
    await loadLibrary();
    toast(kind === "book"
      ? `地址库已改名为「${value}」。`
      : `标签组已改名为「${value}」，整组地址一起更新。`);
  } catch (error) {
    toast(`改名失败：${error.message}`, true);
    button.disabled = false;
  }
}

function updateUndoButton() {
  const button = $("#undo-last-import");
  if (!button) return;
  const item = state.lastImport;
  button.disabled = !item;
  button.title = item
    ? `撤回：${item.summary}（删除新导入 ${item.inserted} 条，还原 ${item.updated} 条）`
    : "没有可撤回的批量导入记录";
}

async function undoLastImport(button) {
  button.disabled = true;
  let finished = false;
  try {
    const info = await api("/api/library/last-import");
    const item = info.item;
    if (!item) { toast("没有可撤回的批量导入记录。", true); return; }
    const parts = [`「${item.summary}」`];
    if (item.inserted) parts.push(`删除新导入 ${item.inserted} 条`);
    if (item.updated) parts.push(`还原被改写 ${item.updated} 条`);
    const createdBooks = (item.books && item.books.length ? item.books : (item.book ? [item.book] : []));
    if (createdBooks.length) parts.push(`移除地址库「${createdBooks.join("、")}」`);
    if (!window.confirm(`撤回上一次导入 ${parts.join("，")}？\n\n撤回只做一次，删除的地址无法恢复。`)) return;
    const result = await api("/api/library/undo-last-import", { method: "POST" });
    await loadLibrary();
    finished = true;
    toast(`已撤回：删除 ${result.deleted} 条，还原 ${result.restored} 条${result.book_removed ? `，并移除地址库「${createdBooks.join("、")}」` : ""}。`);
  } catch (error) {
    toast(`撤回失败：${error.message}`, true);
  } finally {
    if (!finished) button.disabled = false;
  }
}

function updateSeedButton() {
  const button = $("#reinstall-seed-books");
  if (!button) return;
  button.title = state.seedInfo.groups
    ? `重新导入外发包预置地址库（${state.seedInfo.groups} 组 / ${state.seedInfo.addresses} 个地址）：只补回地址库里缺失的地址，已存在的跳过，不覆盖人工改动。`
    : "外发包里没有预置地址库文件（app/seed_books）";
}

async function reinstallSeedBooks(button) {
  button.disabled = true;
  let finished = false;
  try {
    const info = await api("/api/library/seed-books").catch(() => ({ groups: 0, addresses: 0 }));
    if (!info.groups) { toast("外发包里没有预置地址库文件（app/seed_books）。", true); return; }
    const okay = window.confirm(
      `重新导入外发包预置地址库（${info.groups} 组 / ${info.addresses} 个地址）？\n\n` +
      `· 只补回地址库里缺失的地址，已存在的跳过，不会覆盖你改过的分类 / 标签 / 备注。\n` +
      `· 之前解散过的预置组会被重新建出来。\n` +
      `· 导错了可点「撤回上次导入」撤销。`
    );
    if (!okay) return;
    const result = await api("/api/library/seed-books/reinstall", { method: "POST" });
    if (state.bookFilter) state.bookFilter = "";
    await loadLibrary();
    finished = true;
    button.disabled = false;
    if (!result.inserted) {
      toast("预置地址库已是最新，没有缺失的地址。");
      return;
    }
    const detail = (result.groups || [])
      .filter((group) => group.inserted > 0)
      .map((group) => `${group.name} +${group.inserted}`)
      .join("、");
    toast(`已重新导入 ${result.inserted} 个地址${detail ? `（${detail}）` : ""}${result.created_books?.length ? `，重建地址库 ${result.created_books.join("、")}` : ""}。`);
  } catch (error) {
    toast(`重新导入失败：${error.message}`, true);
  } finally {
    if (!finished) button.disabled = false;
  }
}

async function saveGroupCategories(button) {
  const kind = button.dataset.groupKind;
  const name = button.dataset.groupName;
  const selected = [...(button.closest(".group-category-editor")?.querySelectorAll("[data-group-category-choice]:checked") || [])]
    .map((checkbox) => checkbox.value);
  if (!selected.length) { toast("请至少勾选一个分类。", true); return; }
  if (!window.confirm(`把「${name}」这一组的地址分类统一改为「${selected.map(categoryLabel).join("、")}」${selected.length > 1 ? "（这组地址会同时属于这几个分类）" : ""}？\n\n整组一起改，交易所/验证者地址会跳过；改错了可点「撤回上次导入」还原。`)) return;
  button.disabled = true;
  try {
    const result = await api("/api/library/group-categories", {
      method: "POST",
      body: JSON.stringify({
        categories: selected,
        book: kind === "book" ? name : "",
        label: kind === "label" ? name : "",
      }),
    });
    state.groupCategory = null;
    await loadLibrary();
    toast(`「${name}」整组分类已改为 ${selected.map(categoryLabel).join("、")}（${result.updated} 个地址${result.skipped ? `，跳过 ${result.skipped} 个交易所/验证者地址` : ""}）。`);
  } catch (error) {
    toast(`分类保存失败：${error.message}`, true);
    button.disabled = false;
  }
}

// -- 组内手动添加地址 ------------------------------------------------------
function isExchangeGroupLabel(name) {
  return name === "人工确认交易所" || name === "人工否定交易所";
}

async function saveGroupAdd(button) {
  const editor = button.closest(".group-add-editor");
  const textarea = editor?.querySelector("[data-group-add-input]");
  const noteInput = editor?.querySelector("[data-group-add-note]");
  const moveInput = editor?.querySelector("[data-group-add-move]");
  const addresses = String(textarea?.value || "").trim();
  if (!addresses) {
    toast("先填入要添加的地址（每行一个）。", true);
    textarea?.focus();
    return;
  }
  const kind = button.dataset.groupKind;
  const name = button.dataset.groupName;
  const bookName = button.dataset.groupBook || "";
  const category = button.dataset.groupCategory || "";
  const isNamedGroup = button.dataset.groupCanRename === "1";
  const moveExisting = Boolean(moveInput?.checked);
  const payload = {
    addresses,
    note: String(noteInput?.value || "").trim(),
    category,
    move_existing: moveExisting,
  };
  if (bookName) {
    payload.book = bookName;
    payload.label = bookName;
  } else if (kind === "label" && isNamedGroup) {
    payload.label = name;
  }
  button.disabled = true;
  try {
    const result = await api("/api/library/group-addresses", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.groupAdd = null;
    await loadLibrary();
    const parts = [];
    if (result.inserted) parts.push(`已加到「${name}」${result.inserted} 个地址`);
    if (result.moved) parts.push(`从别的组转入 ${result.moved} 个`);
    if (result.invalid_count) {
      const sample = result.invalid.slice(0, 3).join("、");
      parts.push(`${result.invalid_count} 个格式不对（${sample}${result.invalid_count > 3 ? "…" : ""}）`);
    }
    const groups = [...new Set((result.conflicts || []).map((item) => item.group))];
    if (!parts.length && !result.conflict_count) {
      parts.push(`这些地址本来就在「${name}」`);
    }
    if (result.conflict_count) {
      const where = groups.slice(0, 3).join("」「");
      parts.push(
        `${result.conflict_count} 个已在「${where}」组，已跳过——勾选“已在别的组的地址也移到本组”再点一次就能转过来`
      );
    }
    toast(`${parts.join("；")}。`, Boolean(result.conflict_count && !result.inserted && !result.moved));
  } catch (error) {
    toast(`添加失败：${error.message}`, true);
    button.disabled = false;
  }
}

async function decideLibraryExchange(button) {
  button.disabled = true;
  try {
    await api(`/api/library/${button.dataset.rowId}/confirm-exchange`, {
      method: "POST",
      body: JSON.stringify({ decision: button.dataset.libraryExchangeDecision }),
    });
    await loadLibrary();
    const decision = button.dataset.libraryExchangeDecision;
    toast(
      decision === "confirm"
        ? "已确认，扫描时允许过滤该交易所地址。"
        : decision === "reject"
          ? "已否定，该地址不会被交易所过滤；判错可点「改为交易所」。"
          : "已重置为待确认，可重新判断。",
      false
    );
  } catch (error) {
    toast(`交易所人工结论更新失败：${error.message}`, true);
    button.disabled = false;
  }
}

async function exportLibrary(format) {
  try {
    const response = await fetch(`/api/library/export?format=${encodeURIComponent(format)}`);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `导出失败 (${response.status})`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `bittensor-address-library-${new Date().toISOString().slice(0, 10)}.${format}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast(`地址库已导出为 ${format.toUpperCase()}，可在其他设备通过“导入文件”恢复。`);
  } catch (error) {
    toast(`地址库导出失败：${error.message}`, true);
  }
}

async function saveRelationNote(button) {
  const rowElement = button.closest("tr");
  const address = rowElement?.dataset.relatedAddress;
  const input = rowElement?.querySelector("[data-relation-note-input]");
  if (!address || !input) return;
  button.disabled = true;
  try {
    const result = await api("/api/notes", {
      method: "POST",
      body: JSON.stringify({ address, note: input.value }),
    });
    const row = state.lastResult?.related?.find((entry) => entry.address === address);
    if (row) {
      row.library_note = input.value;
      if (result.item) {
        row.match = true;
        row.library_category = result.item.category;
        row.library_label = result.item.label;
        row.library_source = result.item.source;
      }
    }
    button.textContent = "已保存";
    window.setTimeout(() => { button.textContent = "保存备注"; }, 1400);
  } catch (error) { toast(`备注保存失败：${error.message}`, true); }
  finally { button.disabled = false; }
}

async function copyAddress(address, button) {
  if (!address) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(address);
    } else {
      const input = document.createElement("textarea");
      input.value = address;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      if (!document.execCommand("copy")) throw new Error("clipboard unavailable");
      input.remove();
    }
    const original = button.textContent;
    button.textContent = "已复制";
    window.setTimeout(() => { button.textContent = original || "复制"; }, 1200);
  } catch (_) {
    toast("浏览器未允许自动复制，请手动选择地址复制。", true);
  }
}

function pruneDownstreamOf(address) {
  // 只把「路径经过该地址」的关联地址在本次会话里隐藏：真正是否移除由刷新后的
  // 后端终点规则决定（还有别的通路的地址刷新后仍会出现，避免误删）。
  const rows = state.lastResult?.related || [];
  let count = 0;
  rows.forEach((row) => {
    if (row.address === address) return;
    const hops = row.path_transfers || [];
    const onPath = hops.some((hop) => (
      hop.path_from === address
      || hop.path_to === address
      || hop.from_address === address
      || hop.to_address === address
    ));
    if (!onPath) return;
    if (!state.exchangePruned.has(row.address)) {
      state.exchangePruned.add(row.address);
      count += 1;
    }
  });
  return count;
}

async function decideExchange(address, decision, button) {
  if (!address) return;
  button.disabled = true;
  try {
    const result = await api("/api/library/confirm-exchange-address", {
      method: "POST",
      body: JSON.stringify({ address, label: "", decision }),
    });
    const item = result.item;
    const row = state.lastResult?.related?.find((entry) => entry.address === address);
    if (row && item) {
      row.match = true;
      row.library_label = item.label;
      row.library_category = item.category;
      row.library_note = item.note;
      row.library_source = item.source;
      row.exchange_confirmed = item.exchange_confirmed;
      row.exchange_rejected = item.exchange_rejected;
      if (decision === "confirm") {
        row.address_role = item.label || "交易所地址";
        row.role_reason = "已由用户人工确认并加入交易所过滤库。";
      } else if (decision === "reject") {
        row.address_role = "已人工否定交易所";
        row.role_reason = "已由用户人工否定，不会进入交易所过滤集合。";
      } else {
        row.address_role = "疑似交易所（待确认）";
        row.role_reason = "交易所候选尚未人工确认，交易所过滤不可用。";
      }
    }
    const pruned = decision === "confirm" ? pruneDownstreamOf(address) : 0;
    renderResult(state.lastResult);
    if (decision === "confirm") {
      toast(pruned
        ? `已标为交易所：其下游 ${pruned} 个关联地址已从列表隐藏，点「刷新结果」重查后不会再出现。`
        : "已标为交易所：该地址作为关联终点，刷新后不再向下扩展。", false);
    } else if (decision === "reject") {
      toast("已人工否定该地址；后续扫描不会再将其列为交易所候选。判错可点「改为交易所」。", false);
    } else {
      toast("已重置为待确认：该地址回到“疑似交易所（待确认）”，可重新判断。", false);
    }
  } catch (error) {
    toast(`确认失败：${error.message}`, true);
    button.disabled = false;
  }
}

async function addAddress() {
  const address = $("#lib-address").value.trim();
  if (!address) { toast("请输入地址。", true); return; }
  try {
    await api("/api/library", { method: "POST", body: JSON.stringify({ address, label: $("#lib-label").value.trim(), category: $("#lib-category").value, note: $("#lib-note").value.trim() }) });
    $("#lib-address").value = "";
    $("#lib-label").value = "";
    $("#lib-note").value = "";
    await loadLibrary();
    toast("地址已加入已知地址库。");
  } catch (error) { toast(error.message, true); }
}

async function uploadLibrary(file) {
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await fetch("/api/library/import", { method: "POST", body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "导入失败");
    await loadLibrary();
    toast(`导入完成：新增 ${data.inserted}，更新 ${data.updated}，跳过 ${data.skipped}。`);
  } catch (error) { toast(error.message, true); }
  finally { $("#library-file").value = ""; }
}

async function syncExchangeLibrary() {
  const button = $("#sync-exchanges");
  button.disabled = true;
  button.textContent = "同步中…";
  try {
    const data = await api("/api/library/import-exchanges", { method: "POST" });
    await loadLibrary();
    const cacheText = data.from_cache ? "（使用 5 分钟内缓存）" : "";
    toast(`交易所地址同步完成：新增 ${data.inserted}，更新 ${data.updated}，跳过 ${data.skipped}，共识别 ${data.provider_count} 个${cacheText}。`);
  } catch (error) {
    toast(`交易所地址同步失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = "同步官方交易所地址";
  }
}

// -- 查询记录（历史结果） -------------------------------------------------
const HISTORY_PAGE_SIZE = 20;

function historyTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function historyStatus(value) {
  return ({
    complete: ["已完成", ""],
    cancelled: ["已停止", "warn"],
    failed: ["失败", "bad"],
  })[value] || [value || "-", ""];
}

function historyTable(items) {
  if (!items.length) {
    return '<div class="placeholder">还没有查询记录。完成一次关联查询（分轮或单轮）后会自动保存在这里。</div>';
  }
  return `<table><thead><tr><th>时间</th><th>发起地址</th><th>状态</th><th>关联地址</th><th>地址库命中</th><th>轮次 / 请求</th><th>操作</th></tr></thead><tbody>${items.map((item) => {
    const [statusText, statusTone] = historyStatus(item.status);
    const kindText = item.kind === "job" ? "分轮" : "单轮";
    const hit = item.library_hit_count
      ? `<span class="tag warn">${number(item.library_hit_count)}</span>`
      : "-";
    return `<tr><td class="mono history-time">${esc(historyTime(item.created_at))}</td><td class="mono address"><div class="address-main"><code title="${esc(item.seed)}">${esc(shortAddr(item.seed))}</code><button class="ghost copy-address" data-copy-address="${esc(item.seed)}" title="复制完整地址" type="button">复制</button></div></td><td><span class="tag ${statusTone}">${esc(statusText)}</span><span class="history-kind">${esc(kindText)}</span></td><td class="amount">${number(item.related_count)}</td><td class="amount">${hit}</td><td class="mono">${number(item.rounds)} / ${number(item.requests)}</td><td><div class="history-row-actions"><button class="secondary note-save" data-history-view="${item.id}" type="button">查看结果</button><button class="ghost note-save" data-history-export="txt" data-history-id="${item.id}" type="button">导出地址</button><button class="ghost note-save" data-history-export="json" data-history-id="${item.id}" type="button">导出 JSON</button><button class="ghost note-save" data-history-sync="${item.id}" type="button">同步地址库</button><button class="danger note-save" data-history-delete="${item.id}" type="button">删除</button></div></td></tr>`;
  }).join("")}</tbody></table>`;
}

async function loadHistory(offset = state.history.offset) {
  try {
    const data = await api(`/api/relations/history?limit=${HISTORY_PAGE_SIZE}&offset=${offset}`);
    state.history = { offset: data.offset, total: data.total, items: data.items };
    $("#history-table").innerHTML = historyTable(data.items);
    $("#history-count").textContent = `${number(data.total)} 条`;
    const page = Math.floor(data.offset / HISTORY_PAGE_SIZE) + 1;
    const pages = Math.max(1, Math.ceil(data.total / HISTORY_PAGE_SIZE));
    $("#history-page").textContent = `第 ${page} / ${pages} 页`;
    $("#history-prev").disabled = data.offset <= 0;
    $("#history-next").disabled = data.offset + HISTORY_PAGE_SIZE >= data.total;
  } catch (error) {
    $("#history-table").innerHTML = `<div class="placeholder">查询记录加载失败：${esc(error.message)}</div>`;
    toast(`查询记录加载失败：${error.message}`, true);
  }
}

async function viewHistoryRecord(id) {
  try {
    const data = await api(`/api/relations/history/${id}`);
    if (!data.result) {
      toast("这条记录没有可展示的结果。", true);
      return;
    }
    navigate("scan");
    state.removedAddresses.clear();
    state.exchangePruned.clear();
    state.page = 1;
    renderResult(data.result);
    toast(`已载入历史记录 #${id}（${historyTime(data.item.created_at)}）：${(data.result.related || []).length} 个关联地址。`);
    const panel = $("#result");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(`历史记录读取失败：${error.message}`, true);
  }
}

async function exportHistoryRecord(id, format) {
  try {
    const depthSelect = $("#history-export-depth");
    const depth = depthSelect && depthSelect.value !== "all" ? depthSelect.value : "";
    const depthQuery = depth ? `&depth=${encodeURIComponent(depth)}` : "";
    const response = await fetch(`/api/relations/history/${id}/export?format=${encodeURIComponent(format)}${depthQuery}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `导出失败 (${response.status})`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const matched = disposition.match(/filename="([^"]+)"/);
    const filename = matched ? matched[1] : `bittensor-history-${id}.${format}`;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    toast(depth
      ? `历史记录 #${id} 的 ${depth} 级关联地址已导出为 ${format.toUpperCase()}。`
      : `历史记录 #${id} 已导出为 ${format.toUpperCase()}（全部层级）。`);
  } catch (error) {
    toast(`历史记录导出失败：${error.message}`, true);
  }
}

async function deleteHistoryRecord(id) {
  if (!window.confirm("确定删除这条查询记录？删除后无法恢复（地址库中的地址不受影响）。")) return;
  try {
    await api(`/api/relations/history/${id}`, { method: "DELETE" });
    await loadHistory(Math.max(0, state.history.offset));
    toast("查询记录已删除。");
  } catch (error) {
    toast(`删除失败：${error.message}`, true);
  }
}

async function clearHistory() {
  if (!state.history.total) { toast("当前没有查询记录。"); return; }
  if (!window.confirm(`确定清空全部 ${state.history.total} 条查询记录？此操作不可撤销，地址库不受影响。`)) return;
  try {
    const result = await api("/api/relations/history/clear", { method: "POST" });
    await loadHistory(0);
    toast(`已清空 ${result.deleted} 条查询记录。`);
  } catch (error) {
    toast(`清空失败：${error.message}`, true);
  }
}

// -- 关联地址同步进地址库 -------------------------------------------------
async function syncAddresses({ seed, related, mode = "all", category = "其他", label = "", book = "" }) {
  const addresses = (related || []).map((row) => row.address).filter(Boolean);
  return api("/api/relations/sync-library", {
    method: "POST",
    body: JSON.stringify({
      seed: seed || "",
      addresses,
      include_seed: true,
      category,
      label,
      book,
      only_library_matched: mode === "matched",
    }),
  });
}

/* 同步目标：自建库名 + 人工命名的标签组，去重后作为下拉选项。 */
function libraryGroupChoices() {
  const books = [];
  const labels = [];
  (state.library || []).forEach((row) => {
    if (row.book) {
      if (!books.includes(row.book)) books.push(row.book);
    } else if (row.label_custom && !labels.includes(row.label)) {
      labels.push(row.label);
    }
  });
  books.sort((a, b) => a.localeCompare(b, "zh"));
  labels.sort((a, b) => a.localeCompare(b, "zh"));
  return [
    ...books.map((name) => ({ value: `book:${name}`, text: `${name} · 自建地址库` })),
    ...labels.map((name) => ({ value: `label:${name}`, text: `${name} · 标签组` })),
  ];
}

function fillSyncGroupSelect() {
  const choices = libraryGroupChoices();
  ["#sync-label-group", "#history-sync-group"].forEach((selector) => {
    const select = $(selector);
    if (!select) return;
    const previous = select.value;
    select.innerHTML = '<option value="__new__">＋ 新建标签组（默认用起始地址后四位）</option>'
      + choices.map((item) => `<option value="${esc(item.value)}">${esc(item.text)}</option>`).join("");
    if (previous && [...select.options].some((option) => option.value === previous)) {
      select.value = previous;
    } else if (previous === "__new__") {
      select.value = "__new__";
    }
  });
  updateSyncNewLabelRow();
}

function updateSyncNewLabelRow() {
  const select = $("#sync-label-group");
  const row = $("#sync-new-label-row");
  if (!select || !row) return;
  row.classList.toggle("hidden", select.value !== "__new__");
}

function groupValueForAddress(address) {
  const row = (state.library || []).find((item) => item.address === address);
  if (!row) return "";
  if (row.book) return `book:${row.book}`;
  if (row.label_custom) return `label:${row.label}`;
  return "";
}

function defaultSyncGroupValue() {
  const data = state.lastResult;
  if (!data) return "__new__";
  const addresses = [data.seed, ...(data.library_matches || []).map((item) => item.address)];
  for (const address of addresses) {
    const value = groupValueForAddress(address);
    if (value) return value;
  }
  return "__new__";
}

function updateSyncHint() {
  const hint = $("#sync-hint");
  const select = $("#sync-label-group");
  if (!hint || !select || !state.lastResult) return;
  const data = state.lastResult;
  const libraryHitCount = (data.library_matches || []).length;
  const target = select.value;
  const targetText = target === "__new__"
    ? "将新建一个标签组"
    : `将合并进「${target.replace(/^(book|label):/, "")}」，不另起新组`;
  hint.textContent = `本次 ${(data.related || []).length} 个关联地址 + 发起地址；命中已知地址库 ${libraryHitCount} 个，${targetText}。`;
}

function applyDefaultSyncGroup(force = false) {
  const select = $("#sync-label-group");
  if (!select) return;
  const seed = state.lastResult?.seed || "";
  const seedChanged = state.syncTargetSeed !== seed;
  if (force || seedChanged || !state.syncTargetTouched) {
    if (seedChanged) state.syncTargetTouched = false;
    state.syncTargetSeed = seed;
    const value = defaultSyncGroupValue();
    select.value = [...select.options].some((option) => option.value === value) ? value : "__new__";
    if (select.value === "__new__") $("#sync-label").value = "";
  }
  updateSyncNewLabelRow();
  updateSyncHint();
  syncCategoryForTarget();
}

function syncCategoryForTarget() {
  const select = $("#sync-label-group");
  const categorySelect = $("#sync-category");
  if (!select || !categorySelect) return;
  const target = select.value;
  if (target === "__new__") return;
  const name = target.replace(/^(book|label):/, "");
  const rows = (state.library || []).filter((row) => (
    target.startsWith("book:")
      ? row.book === name
      : !row.book && row.label_custom && row.label === name
  ));
  const categories = [...new Set(rows.map((row) => row.category))];
  // 交易所 / 验证者 / 系统 是过滤用的系统分类，同步时不能选，保持用户自选分类。
  const systemCategories = ["EXCHANGE", "VALIDATOR", "系统"];
  if (categories.length !== 1 || systemCategories.includes(categories[0])) return;
  if ([...categorySelect.options].some((option) => option.value === categories[0])) {
    categorySelect.value = categories[0];
  }
}

function syncTargetPayload(target, newLabel = "") {
  if (!target || target === "__new__") {
    return { label: String(newLabel || "").trim(), book: "" };
  }
  if (target.startsWith("book:")) {
    const name = target.slice(5);
    return { label: name, book: name };
  }
  return { label: target.slice(6), book: "" };
}

function syncSummary(result) {
  const parts = [`新增 ${result.inserted} 个`, `已存在 ${result.existing} 个`];
  if (result.recategorized) parts.push(`归入「${result.category}」${result.recategorized} 个`);
  if (result.matched) parts.push(`命中地址库 ${result.matched} 个`);
  if (result.invalid) parts.push(`跳过无效 ${result.invalid} 个`);
  return parts.join("，");
}

/* 新建地址库：把当前结果的可导入地址打上库名存进地址库，
   规则与导出 TXT 一致——已确认交易所和验证者地址剔除。 */
function seedIsPublicAddress(data) {
  return (data.seed_library_matches || []).some((entry) => {
    const category = String(entry.category || "").toUpperCase();
    if (category === "VALIDATOR") return true;
    return category === "EXCHANGE" && Boolean(entry.exchange_confirmed);
  });
}

function updateBookHint() {
  const hint = $("#book-hint");
  if (!hint) return;
  const data = state.lastResult;
  if (!data) { hint.textContent = "先完成一次关联查询。"; return; }
  const { addresses, skippedExchange, skippedValidator } = selectExportableAddresses(data.related);
  const includeSeed = $("#book-include-seed").checked;
  const seedSkipped = includeSeed && seedIsPublicAddress(data);
  const count = addresses.length + (includeSeed && !seedSkipped ? 1 : 0);
  const parts = [`本次可导入 ${count} 个地址`];
  if (skippedExchange || skippedValidator) {
    parts.push(`已排除交易所 ${skippedExchange} 个、验证者 ${skippedValidator} 个`);
  }
  parts.push(includeSeed ? "含发起地址" : "不含发起地址");
  if (seedSkipped) parts.push("发起地址本身是交易所或验证者，会被跳过");
  hint.textContent = `${parts.join("；")}。`;
}

async function createBookFromResult() {
  if (!state.lastResult) { toast("先完成一次关联查询。", true); return; }
  const name = $("#book-name").value.trim();
  if (!name) { toast("请先填写地址库名称。", true); $("#book-name").focus(); return; }
  const button = $("#create-book");
  button.disabled = true;
  button.textContent = "导入中…";
  try {
    const result = await api("/api/library/books", {
      method: "POST",
      body: JSON.stringify({
        name,
        seed: state.lastResult.seed || "",
        addresses: (state.lastResult.related || []).map((row) => row.address).filter(Boolean),
        include_seed: $("#book-include-seed").checked,
        category: $("#book-category").value,
      }),
    });
    const parts = [`新增 ${result.inserted} 个`];
    if (result.attached) parts.push(`补入已有记录 ${result.attached} 个`);
    if (result.already_in_book) parts.push(`本库已有 ${result.already_in_book} 个`);
    if (result.other_book) parts.push(`属于其他库 ${result.other_book} 个（保留原库）`);
    if (result.skipped_exchange) parts.push(`排除交易所 ${result.skipped_exchange} 个`);
    if (result.skipped_validator) parts.push(`排除验证者 ${result.skipped_validator} 个`);
    if (result.skipped_system) parts.push(`排除系统地址 ${result.skipped_system} 个`);
    toast(`地址库「${result.name}」已创建：${parts.join("，")}。可到“已知地址库”查看。`);
    $("#book-name").value = "";
    state.books = [];
    await loadLibrary().catch(() => {});
  } catch (error) {
    toast(`新建地址库失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = "新建地址库并导入";
  }
}

async function syncCurrentResult(mode) {
  if (!state.lastResult) { toast("先完成一次关联查询。", true); return; }
  if (!state.library.length) {
    await loadLibrary().catch(() => {});
    fillSyncGroupSelect();
    state.syncTargetTouched = false;
    applyDefaultSyncGroup(true);
  }
  const button = mode === "matched" ? $("#sync-matched") : $("#sync-all");
  button.disabled = true;
  try {
    const target = $("#sync-label-group").value;
    const { label, book } = syncTargetPayload(target, $("#sync-label").value);
    const result = await syncAddresses({
      seed: state.lastResult.seed,
      related: state.lastResult.related,
      mode,
      category: $("#sync-category").value,
      label,
      book,
    });
    const groupName = result.book || result.label || label;
    toast(`已合并进「${groupName}」：${syncSummary(result)}。`);
    await loadLibrary();
  } catch (error) {
    toast(`同步失败：${error.message}`, true);
  } finally {
    button.disabled = mode === "matched" && !(state.lastResult?.library_matches || []).length;
  }
}

async function syncHistoryRecord(id) {
  try {
    const data = await api(`/api/relations/history/${id}`);
    if (!data.result) { toast("这条记录没有可同步的地址。", true); return; }
    const category = $("#history-sync-category")?.value || "其他";
    const { label, book } = syncTargetPayload($("#history-sync-group")?.value || "__new__", "");
    const result = await syncAddresses({
      seed: data.result.seed,
      related: data.result.related,
      category,
      label,
      book,
    });
    const groupName = result.book || result.label || label;
    toast(`历史记录 #${id} 已合并进「${groupName}」：${syncSummary(result)}。`);
    await loadHistory(state.history.offset);
    await loadLibrary();
  } catch (error) {
    toast(`同步失败：${error.message}`, true);
  }
}

document.querySelectorAll("[data-nav]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.nav)));
const savedTheme = (() => {
  try {
    const saved = localStorage.getItem("wallet-link-theme");
    if (["system", "warm", "darkgray"].includes(saved)) return saved;
    if (!saved && localStorage.getItem("wallet-link-eye-mode") === "on") return "warm";
    return "system";
  }
  catch (_) {
    return ({ eye: "warm", darkgray: "darkgray" })[document.documentElement.dataset.theme] || "system";
  }
})();
setTheme(savedTheme, false);
$("#theme-select").addEventListener("change", (event) => setTheme(event.target.value));
$("#save-taostats-key").addEventListener("click", saveTaostatsKey);
$("#clear-taostats-key").addEventListener("click", clearTaostatsKey);
$("#toggle-api-key").addEventListener("click", () => setApiKeyBody(!state.apiKeyBodyOpen));
$("#taostats-api-key").addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveTaostatsKey();
});
$("#run-scan").addEventListener("click", () => runRelations(false));
$("#refresh-result").addEventListener("click", () => {
  if (!state.lastResult) {
    toast("还没有查询结果；先输入起始地址点「扫描关联」。", true);
    return;
  }
  runRelations(true);
});
$("#cancel-job").addEventListener("click", cancelActiveJob);
$("#taostats-source").addEventListener("change", () => {
  $("#multi-round").disabled = !$("#taostats-source").checked;
});
$("#multi-round").disabled = !$("#taostats-source").checked;
function updateRoundHint() {
  const rounds = Math.min(100, Math.max(1, Number($("#max-rounds").value) || 5));
  $("#round-hint").textContent = `≈ ${rounds * 5} 次请求`;
}
$("#max-rounds").addEventListener("input", updateRoundHint);
$("#max-rounds").addEventListener("change", () => {
  $("#max-rounds").value = Math.min(100, Math.max(1, Number($("#max-rounds").value) || 5));
  updateRoundHint();
});
updateRoundHint();
$("#related-depth-filter").addEventListener("change", (event) => {
  state.depthFilter = event.target.value;
  refreshRelatedTable();
});
// 金额筛选：改方式或阈值都只重排当前表格（局部刷新，不重新查询）。
$("#related-amount-mode").addEventListener("change", (event) => {
  state.amountFilter.mode = event.target.value;
  // 从「不限」切到具体口径时，把焦点给阈值输入框，省一次点击。
  if (state.amountFilter.mode !== "off" && amountMinValue() <= 0) $("#related-amount-min")?.focus();
  saveAmountFilter();
  syncAmountFilterControls();
  state.page = 1;
  refreshRelatedTable();
});
let amountInputTimer = 0;
$("#related-amount-min").addEventListener("input", (event) => {
  state.amountFilter.min = event.target.value;
  if (state.amountFilter.mode === "off" && amountMinValue() > 0) {
    // 只填数字、没选口径时默认按“流入或流出”筛，避免填了却没反应。
    state.amountFilter.mode = "either";
    $("#related-amount-mode").value = "either";
  }
  saveAmountFilter();
  syncAmountFilterControls();
  window.clearTimeout(amountInputTimer);
  amountInputTimer = window.setTimeout(() => {
    state.page = 1;
    refreshRelatedTable();
  }, 250);
});
$("#related-amount-reset").addEventListener("click", () => {
  state.amountFilter = { mode: "off", min: "" };
  saveAmountFilter();
  syncAmountFilterControls();
  state.page = 1;
  refreshRelatedTable();
  toast("已清除金额筛选，显示全部关联地址。");
});
$("#scan-latest").addEventListener("click", () => scanBlocks(false));
$("#scan-advance").addEventListener("click", () => scanBlocks(true));
$("#add-address").addEventListener("click", addAddress);
$("#choose-file").addEventListener("click", () => $("#library-file").click());
$("#library-file").addEventListener("change", (event) => { if (event.target.files[0]) uploadLibrary(event.target.files[0]); });
$("#sync-exchanges").addEventListener("click", syncExchangeLibrary);
$("#export-library-csv").addEventListener("click", () => exportLibrary("csv"));
$("#export-library-json").addEventListener("click", () => exportLibrary("json"));
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-save-relation-note]");
  if (button) saveRelationNote(button);
  const exchangeButton = event.target.closest("[data-exchange-decision]");
  if (exchangeButton) decideExchange(exchangeButton.dataset.address, exchangeButton.dataset.exchangeDecision, exchangeButton);
  const copyButton = event.target.closest("[data-copy-address]");
  if (copyButton) copyAddress(copyButton.dataset.copyAddress, copyButton);
  const pathToggle = event.target.closest("[data-path-toggle]");
  if (pathToggle) togglePathDetail(pathToggle.dataset.pathToggle);
  const pageJump = event.target.closest("[data-page-jump]");
  if (pageJump) gotoRelatedPage(pageJump.dataset.pageJump);
  const removeRelationButton = event.target.closest("[data-remove-relation]");
  if (removeRelationButton) removeRelation(removeRelationButton.dataset.removeRelation);
  const historyView = event.target.closest("[data-history-view]");
  if (historyView) viewHistoryRecord(historyView.dataset.historyView);
  const historyExport = event.target.closest("[data-history-export]");
  if (historyExport) exportHistoryRecord(historyExport.dataset.historyId, historyExport.dataset.historyExport);
  const historySync = event.target.closest("[data-history-sync]");
  if (historySync) syncHistoryRecord(historySync.dataset.historySync);
  const historyDelete = event.target.closest("[data-history-delete]");
  if (historyDelete) deleteHistoryRecord(historyDelete.dataset.historyDelete);
  const bookView = event.target.closest("[data-book-view]");
  if (bookView) selectBook(bookView.dataset.bookView);
  const bookExport = event.target.closest("[data-book-export]");
  if (bookExport) exportBook(bookExport.dataset.bookName, bookExport.dataset.bookExport);
  const bookDissolve = event.target.closest("[data-book-dissolve]");
  if (bookDissolve) dissolveBook(bookDissolve.dataset.bookDissolve);
  const libraryToggle = event.target.closest("[data-library-toggle]");
  if (libraryToggle) {
    const groupId = libraryToggle.dataset.libraryToggle;
    const expanded = libraryToggle.getAttribute("aria-expanded") === "true";
    libraryToggle.setAttribute("aria-expanded", String(!expanded));
    if (expanded) state.libraryGroupsOpen.delete(groupId);
    else state.libraryGroupsOpen.add(groupId);
    const details = document.querySelector(`[data-library-details="${groupId}"]`);
    if (details) details.classList.toggle("hidden", expanded);
  }
  const editGroupCategoriesButton = event.target.closest("[data-edit-group-categories]");
  if (editGroupCategoriesButton) {
    state.groupCategory = {
      kind: editGroupCategoriesButton.dataset.groupKind,
      name: editGroupCategoriesButton.dataset.groupName,
    };
    state.groupRename = null;
    renderLibraryList();
  }
  const renameGroupButton = event.target.closest("[data-rename-group]");
  if (renameGroupButton) {
    startGroupRename(
      renameGroupButton.dataset.groupKind,
      renameGroupButton.dataset.groupName,
    );
  }
  const saveGroupRenameButton = event.target.closest("[data-save-group-rename]");
  if (saveGroupRenameButton) saveGroupRename(saveGroupRenameButton);
  const cancelGroupRenameButton = event.target.closest("[data-cancel-group-rename]");
  if (cancelGroupRenameButton) {
    state.groupRename = null;
    renderLibraryList();
  }
  const cancelGroupCategoriesButton = event.target.closest("[data-cancel-group-categories]");
  if (cancelGroupCategoriesButton) {
    state.groupCategory = null;
    renderLibraryList();
  }
  const saveGroupCategoriesButton = event.target.closest("[data-save-group-categories]");
  if (saveGroupCategoriesButton) saveGroupCategories(saveGroupCategoriesButton);
  const groupAddButton = event.target.closest("[data-group-add]");
  if (groupAddButton) {
    state.groupAdd = {
      kind: groupAddButton.dataset.groupKind,
      name: groupAddButton.dataset.groupName,
    };
    renderLibraryList();
    const input = document.querySelector("[data-group-add-input]");
    if (input) input.focus();
  }
  const cancelGroupAddButton = event.target.closest("[data-cancel-group-add]");
  if (cancelGroupAddButton) {
    state.groupAdd = null;
    renderLibraryList();
  }
  const saveGroupAddButton = event.target.closest("[data-save-group-add]");
  if (saveGroupAddButton) saveGroupAdd(saveGroupAddButton);
  const mergeCancelButton = event.target.closest("[data-merge-cancel]");
  if (mergeCancelButton) {
    state.merge.open = false;
    renderMergePanel();
  }
  const mergeApplyButton = event.target.closest("[data-merge-apply]");
  if (mergeApplyButton) applyMerge(mergeApplyButton);
  const deleteAddressButton = event.target.closest("[data-delete-address]");
  if (deleteAddressButton) deleteLibraryAddress(deleteAddressButton.dataset.deleteAddress);
  const libraryNoteButton = event.target.closest("[data-save-library-note]");
  if (libraryNoteButton) saveLibraryNote(libraryNoteButton.dataset.saveLibraryNote, libraryNoteButton);
  const libraryExchangeButton = event.target.closest("[data-library-exchange-decision]");
  if (libraryExchangeButton) decideLibraryExchange(libraryExchangeButton);
  const categoryDelete = event.target.closest("[data-category-delete]");
  if (categoryDelete) deleteCategory(categoryDelete.dataset.categoryDelete);
});
document.addEventListener("change", (event) => {
  if (event.target.matches("[data-library-select]")) updateLibrarySelection();
  if (event.target.matches("[data-group-select]")) {
    // 组内全选：只影响这一组的地址（收起状态下也能选中，删除时一并生效）。
    const groupId = event.target.dataset.groupSelect;
    const container = document.querySelector(`[data-library-details="${groupId}"]`);
    if (container) {
      container.querySelectorAll("[data-library-select]").forEach((checkbox) => {
        checkbox.checked = event.target.checked;
      });
    }
    updateLibrarySelection();
  }
  if (event.target.matches("[data-merge-pick]")) {
    // 勾选变化只刷新汇总与“沿用组名”候选，不重建面板（避免输入框失焦）。
    const key = event.target.dataset.mergePick;
    if (event.target.checked) state.merge.picked.add(key);
    else state.merge.picked.delete(key);
    refreshMergePanelState();
  }
  if (event.target.matches('input[name="merge-mode"]')) {
    state.merge.mode = event.target.value;
    const nameInput = document.querySelector("[data-merge-name]");
    const inheritSelect = document.querySelector("[data-merge-inherit]");
    if (nameInput) nameInput.disabled = state.merge.mode === "inherit";
    if (inheritSelect) inheritSelect.disabled = state.merge.mode !== "inherit" || mergePickedGroups().length === 0;
    refreshMergePanelState();
  }
  if (event.target.matches("[data-merge-inherit]")) {
    state.merge.inheritName = event.target.value;
  }
  if (event.target.id === "related-page-size") setRelatedPageSize(event.target.value);
  if (event.target.id === "related-page-input") gotoRelatedPage(event.target.value);
});
document.addEventListener("input", (event) => {
  // 合并面板重渲染会重建 DOM，名字先记在 state 里，勾选组时不会丢。
  if (event.target.matches("[data-merge-name]")) {
    state.merge.target = event.target.value;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.target.matches("[data-group-rename-input]")) {
    if (event.key === "Enter") {
      event.preventDefault();
      const save = document.querySelector("[data-save-group-rename]");
      if (save) saveGroupRename(save);
    } else if (event.key === "Escape") {
      event.preventDefault();
      state.groupRename = null;
      renderLibraryList();
    }
    return;
  }
  if (event.target.id !== "related-page-input") return;
  if (event.key !== "Enter") return;
  event.preventDefault();
  gotoRelatedPage(event.target.value);
});
const selectAllAddresses = $("#select-all-addresses");
if (selectAllAddresses) selectAllAddresses.addEventListener("change", () => {
  document.querySelectorAll("[data-library-select]").forEach((checkbox) => {
    checkbox.checked = selectAllAddresses.checked;
  });
  updateLibrarySelection();
});
$("#sync-all").addEventListener("click", () => syncCurrentResult("all"));
$("#sync-label-group").addEventListener("change", () => {
  state.syncTargetTouched = true;
  updateSyncNewLabelRow();
  updateSyncHint();
  syncCategoryForTarget();
});
const undoImportButton = $("#undo-last-import");
if (undoImportButton) undoImportButton.addEventListener("click", () => undoLastImport(undoImportButton));
const reinstallSeedButton = $("#reinstall-seed-books");
if (reinstallSeedButton) reinstallSeedButton.addEventListener("click", () => reinstallSeedBooks(reinstallSeedButton));
const mergeGroupsButton = $("#merge-groups");
if (mergeGroupsButton) mergeGroupsButton.addEventListener("click", toggleMergePanel);
$("#sync-matched").addEventListener("click", () => syncCurrentResult("matched"));
$("#create-book").addEventListener("click", createBookFromResult);
$("#book-include-seed").addEventListener("change", updateBookHint);
$("#book-category").addEventListener("change", () => {
  if ($("#book-category").value === NEW_CATEGORY) promptNewCategory($("#book-category"));
  else updateBookHint();
});
document.querySelectorAll("[data-category-select]").forEach((select) => {
  if (select.id === "book-category") return;
  select.addEventListener("change", () => {
    if (select.value === NEW_CATEGORY) promptNewCategory(select);
  });
});
$("#add-category").addEventListener("click", addCategoryFromPanel);
$("#new-category-name").addEventListener("keydown", (event) => {
  if (event.key === "Enter") addCategoryFromPanel();
});
$("#book-name").addEventListener("keydown", (event) => {
  if (event.key === "Enter") createBookFromResult();
});
$("#book-filter").addEventListener("change", (event) => selectBook(event.target.value));
$("#refresh-history").addEventListener("click", () => loadHistory(state.history.offset));
$("#clear-history").addEventListener("click", clearHistory);
$("#history-prev").addEventListener("click", () => loadHistory(Math.max(0, state.history.offset - HISTORY_PAGE_SIZE)));
$("#history-next").addEventListener("click", () => loadHistory(state.history.offset + HISTORY_PAGE_SIZE));
$("#start-backfill").addEventListener("click", runBackfill);
$("#stop-backfill").addEventListener("click", () => {
  if (!backfill.running) return;
  backfill.stop = true;
  $("#stop-backfill").disabled = true;
  renderBackfillProgress("正在停止：当前地址查询结束后就不再继续。");
});
$("#delete-selected").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll("[data-library-select]:checked")]
    .map((checkbox) => Number(checkbox.value))
    .filter((id) => Number.isInteger(id) && id > 0);
  if (!ids.length) return;
  const summary = selectionSummary(ids);
  const text = [
    `确定删除选中的 ${ids.length} 条地址？`,
    "",
    ...(summary.groups ? [`涉及 ${summary.groups} 个组：`, ...summary.lines, ""] : []),
    "批量删除不可撤回（只有「解散地址库」能撤回），建议先导出 TXT/CSV 备份。",
  ].join("\n");
  if (!window.confirm(text)) return;
  try {
    const result = await api("/api/library", {
      method: "DELETE",
      body: JSON.stringify({ ids }),
    });
    await loadLibrary();
    toast(`已批量删除 ${result.deleted} 条记录。`);
  } catch (error) { toast(error.message, true); }
});
// 导出层级："all" 或 "1"/"2"/"3"（与结果表“层级筛选”口径一致，按地址首次被发现的层级算）。
function exportDepth() {
  const value = $("#export-depth") ? $("#export-depth").value : "all";
  return value === "all" ? null : Number(value);
}

function depthTag(depth) {
  return depth ? `-d${depth}` : "";
}

$("#export-json").addEventListener("click", () => {
  if (!state.lastResult) { toast("先完成一次关联扫描。", true); return; }
  const depth = exportDepth();
  const rows = (state.lastResult.related || []).filter(
    (row) => depth === null || String(row.depth) === String(depth)
  );
  if (depth !== null && !rows.length) {
    toast(`没有 ${depth} 级关联地址可导出；可把导出层级改回「全部层级」。`, true);
    return;
  }
  const exportData = {
    seed: state.lastResult.seed,
    max_depth: state.lastResult.max_depth,
    data_source: state.lastResult.data_source,
    coverage: state.lastResult.coverage,
    data_cutoff_block: state.lastResult.data_cutoff_block,
    seed_balance_tao: state.lastResult.seed_balance_tao,
    policy: state.lastResult.policy,
    export_depth: depth === null ? "all" : depth,
    related: rows.map((row) => ({
      address: row.address,
      depth: row.depth,
      incoming_tao: row.incoming_tao,
      outgoing_tao: row.outgoing_tao,
      total_tao: row.total_tao,
      balance_tao: row.balance_tao,
      match: row.match,
      library_label: row.library_label,
      library_category: row.library_category,
      stake_transfer_count: row.stake_transfer_count || 0,
    })),
    warnings: state.lastResult.warnings,
    limitations: state.lastResult.limitations,
  };
  const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: "application/json;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `bittensor-link-${String(state.lastResult.seed || "").slice(0, 8)}${depthTag(depth)}-${Date.now()}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
  toast(depth === null
    ? "已导出全部层级关联地址 JSON。"
    : `已导出 ${rows.length} 个 ${depth} 级关联地址 JSON。`);
});
// 纯函数：从关联结果里挑出可导出的地址。地址库中已确认的交易所地址和
// 验证者地址不导出，避免把公共地址当成待跟进钱包继续扩散。
function selectExportableAddresses(rows) {
  const keep = [];
  const seen = new Set();
  let skippedExchange = 0;
  let skippedValidator = 0;
  for (const row of rows || []) {
    const category = String(row.library_category || "").toUpperCase();
    if (category === "EXCHANGE" && row.exchange_confirmed && !row.exchange_rejected) {
      skippedExchange += 1;
      continue;
    }
    if (category === "VALIDATOR") {
      skippedValidator += 1;
      continue;
    }
    if (!row.address || seen.has(row.address)) continue;
    seen.add(row.address);
    keep.push(row.address);
  }
  return { addresses: keep, skippedExchange, skippedValidator };
}

$("#export-addresses").addEventListener("click", () => {
  if (!state.lastResult) { toast("先完成一次关联扫描。", true); return; }
  const depth = exportDepth();
  const depthRows = (state.lastResult.related || []).filter(
    (row) => depth === null || String(row.depth) === String(depth)
  );
  // 地址 TXT 跟随当前金额筛选：筛掉轻微关联后导出的清单就是干净的待跟进列表。
  const rows = depthRows.filter(rowPassesAmountFilter);
  const amountSkipped = depthRows.length - rows.length;
  const amountNote = amountSkipped > 0 ? `，金额筛选隐藏 ${amountSkipped} 个轻微关联` : "";
  const { addresses, skippedExchange, skippedValidator } = selectExportableAddresses(rows);
  if (!addresses.length) {
    const depthNote = depth === null ? "" : `（${depth} 级关联里）`;
    toast(`没有可导出的地址${depthNote}；已排除已确认交易所 ${skippedExchange} 个、验证者 ${skippedValidator} 个${amountNote}。可把导出层级改回「全部层级」或调低金额阈值。`, true);
    return;
  }
  const blob = new Blob([addresses.join("\n") + "\n"], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `bittensor-addresses-${String(state.lastResult.seed || "").slice(0, 8)}${depthTag(depth)}-${Date.now()}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
  const depthText = depth === null ? "全部层级" : `${depth} 级关联`;
  toast(`已导出 ${depthText} ${addresses.length} 个地址（每行一个）；已排除已确认交易所 ${skippedExchange} 个、验证者 ${skippedValidator} 个${amountNote}。`);
});
$("#seed").addEventListener("keydown", (event) => { if (event.key === "Enter") runRelations(); });
$("#license-activate").addEventListener("click", activateLicense);
$("#copy-machine").addEventListener("click", async () => {
  const code = $("#license-machine").textContent.trim();
  if (!code || code === "读取中…") return;
  try {
    await navigator.clipboard.writeText(code);
    toast("机器码已复制，发给管理员即可换取授权码。");
  } catch (_) {
    toast(`机器码：${code}`, true);
  }
});

(async function bootstrap() {
  loadAmountFilter();
  syncAmountFilterControls();
  const status = await loadLicenseStatus();
  applyLicenseStatus(status);
  if (!status.licensed) return; // 未激活时只留激活页，不请求业务接口
  const initialView = String(location.hash || "").replace("#", "");
  if (["history", "library", "method", "release"].includes(initialView)) {
    navigate(initialView, false);
  }
  loadOverview();
  loadVersionInfo();
  loadCategories();
})();
