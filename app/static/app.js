"use strict";

const $ = (selector) => document.querySelector(selector);
const state = {
  lastResult: null,
  library: [],
  books: [],
  categories: [],
  bookFilter: "",
  depthFilter: "all",
  taostatsConfigured: null,
  taostatsKeyStorage: "",
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

function showTaostatsKeyStatus(configured, storage) {
  const status = $("#taostats-status");
  const detail = $("#taostats-key-status");
  if (typeof configured !== "boolean") {
    status.textContent = "后台版本较旧；请重启服务后设置 API key";
    detail.textContent = "设置 API key 前请先更新并重启服务。";
    $("#clear-taostats-key").disabled = true;
    state.taostatsConfigured = null;
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
    toast(
      data.storage === "dpapi"
        ? "API key 已保存（本机加密存储），重启服务后不用重填。"
        : data.storage === "plain"
          ? "API key 已保存到本机 data\\settings.json（明文），重启后不用重填。"
          : "API key 已保存到当前服务内存，重启后需要重填。",
      false
    );
  } catch (error) {
    toast(`API key 保存失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function clearTaostatsKey() {
  const button = $("#clear-taostats-key");
  button.disabled = true;
  try {
    const data = await api("/api/settings/taostats", { method: "DELETE" });
    $("#taostats-api-key").value = "";
    showTaostatsKeyStatus(data.configured, data.storage);
    toast("API key 已从本机清除，Taostats 查询需要重新填写。", false);
  } catch (error) {
    toast(`清除 API key 失败：${error.message}`, true);
    button.disabled = false;
  }
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
    const exchangeActions = row.address_role === "疑似交易所（待确认）"
      ? `<div class="exchange-decision"><button class="secondary note-save" data-exchange-decision="confirm" data-address="${esc(row.address)}" type="button">确认交易所</button><button class="ghost note-save" data-exchange-decision="reject" data-address="${esc(row.address)}" type="button">否定</button></div>`
      : "";
    return `<tr data-related-address="${esc(row.address)}"><td class="address related-address-cell"><div class="address-main"><code title="${esc(row.address)}">${esc(shortAddr(row.address))}</code><button class="ghost copy-address" data-copy-address="${esc(row.address)}" title="复制完整地址" type="button">复制</button>${role}</div></td><td class="amount">${esc(formatTao(row.balance_tao))}</td><td>${row.depth}</td><td class="amount">${esc(formatTao(row.incoming_tao))}</td><td class="amount">${esc(formatTao(row.outgoing_tao))}</td><td class="amount">${esc(formatTao(row.total_tao))}</td><td>${library}${exchangeActions}</td><td><div class="note-editor"><textarea data-relation-note-input placeholder="备注此地址" maxlength="2000">${esc(row.library_note || "")}</textarea><button class="ghost note-save" data-save-relation-note type="button">保存备注</button></div></td></tr>`;
  }).join("")}</tbody></table>`;
}

function refreshRelatedTable() {
  const allRows = state.lastResult?.related || [];
  const rows = state.depthFilter === "all"
    ? allRows
    : allRows.filter((row) => String(row.depth) === state.depthFilter);
  $("#count-related").textContent = number(rows.length);
  $("#related").innerHTML = relatedTable(rows);
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
  $("#sync-hint").textContent = `本次 ${(data.related || []).length} 个关联地址 + 发起地址；其中命中已知地址库 ${libraryHitCount} 个。`;
  $("#sync-matched").disabled = libraryHitCount === 0;
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
  $("#job-text").textContent = "正在停止：会把已查到的关联整理成结果，请稍候…";
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
    await sleep(3000);
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
  };
}

async function runRelations() {
  const seed = $("#seed").value.trim();
  if (!seed) { toast("请先输入起始地址。", true); return; }
  if ($("#taostats-source").checked && state.taostatsConfigured === false) {
    focusTaostatsKey("还没保存 Taostats API key：先粘贴 key 并点「保存 API key」；或取消勾选「使用 Taostats 历史索引」只查本地索引。");
    return;
  }
  const button = $("#run-scan");
  button.disabled = true;
  button.textContent = "扫描中…";
  try {
    const requestedSource = $("#taostats-source").checked ? "taostats" : "local";
    const payload = scanPayload(seed);
    if (requestedSource === "taostats" && $("#multi-round").checked) {
      const rounds = Math.min(100, Math.max(1, Number($("#max-rounds").value) || 12));
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

function libraryRowsTable(rows) {
  return `<table><thead><tr><th class="selection-cell"></th><th>地址</th><th>名称</th><th>分类</th><th>备注</th><th>地址库</th><th>来源</th><th></th></tr></thead><tbody>${rows.map((row) => { const exchangeControl = row.category === "EXCHANGE" ? `<div class="exchange-confirm-control"><span class="tag ${row.exchange_confirmed ? "" : "warn"}">${row.exchange_confirmed ? "已人工确认" : row.exchange_rejected ? "已人工否定" : "待人工确认"}</span>${row.exchange_confirmed ? `<button class="ghost note-save" data-library-exchange-decision="reset" data-row-id="${row.id}" type="button">撤销确认</button>` : row.exchange_rejected ? `<button class="secondary note-save" data-library-exchange-decision="confirm" data-row-id="${row.id}" type="button">重新确认</button><button class="ghost note-save" data-library-exchange-decision="reset" data-row-id="${row.id}" type="button">撤销否定</button>` : `<button class="secondary note-save" data-library-exchange-decision="confirm" data-row-id="${row.id}" type="button">确认交易所</button><button class="ghost note-save" data-library-exchange-decision="reject" data-row-id="${row.id}" type="button">否定交易所</button>`}</div>` : ""; const bookCell = row.book ? `<span class="tag">${esc(row.book)}</span>` : "-"; return `<tr><td class="selection-cell"><input type="checkbox" data-library-select value="${row.id}" aria-label="选择第 ${row.id} 条地址"></td><td class="mono address">${esc(row.address)}</td><td>${esc(row.label || "-")}</td><td><span class="lib-category" data-category="${esc(row.category)}">${esc(row.category_label || categoryLabel(row.category))}</span>${exchangeControl}</td><td><div class="library-note-editor"><textarea data-library-note-input="${row.id}" maxlength="2000" placeholder="添加备注">${esc(row.note || "")}</textarea><button class="ghost note-save" data-save-library-note="${row.id}" type="button">保存</button></div></td><td>${bookCell}</td><td>${esc(row.source || "-")}</td><td><button class="danger" data-delete-address="${row.id}" type="button">删除</button></td></tr>`; }).join("")}</tbody></table>`;
}

function libraryTable(rows) {
  if (!rows.length) {
    return state.bookFilter
      ? `<div class="placeholder">地址库「${esc(state.bookFilter)}」里没有地址。</div>`
      : '<div class="placeholder">地址库为空。可通过上方单个添加或导入 TXT/CSV/JSON。</div>';
  }
  const groups = new Map();
  rows.forEach((row) => {
    const label = (row.label || `${categoryLabel(row.category)} · 未命名`).trim();
    // 自建地址库优先按库名归组，其余仍按「分类 + 名称」。
    const key = row.book ? `book\u0000${row.book}` : `${row.category}\u0000${label}`;
    if (!groups.has(key)) {
      groups.set(key, {
        label: row.book || label,
        category: row.book ? "" : row.category,
        book: row.book || "",
        rows: [],
      });
    }
    groups.get(key).rows.push(row);
  });
  return `<div class="library-groups">${[...groups.values()].map((group, index) => {
    const groupId = `library-group-${index}`;
    const categoryTag = group.category
      ? `<span class="lib-category" data-category="${esc(group.category)}">${esc(categoryLabel(group.category))}</span>`
      : '<span class="lib-category book-tag">自建库</span>';
    return `<section class="library-group"><button class="library-group-toggle" data-library-toggle="${groupId}" aria-expanded="false" type="button"><span><b>${esc(group.label)}</b>${categoryTag}</span><span>${group.rows.length} 个地址 <span class="group-chevron">⌄</span></span></button><div class="library-group-details hidden" data-library-details="${groupId}">${libraryRowsTable(group.rows)}</div></section>`;
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
  if (!window.confirm(`确定解散地址库「${name}」？\n\n只会去掉库名标记，库里的地址仍然保留在已知地址库中，不会删除。`)) return;
  try {
    const result = await api(`/api/library/books/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (state.bookFilter === name) state.bookFilter = "";
    await loadLibrary();
    toast(`已解散地址库「${name}」，${result.addresses_kept} 个地址仍保留在已知地址库。`);
  } catch (error) {
    toast(`解散失败：${error.message}`, true);
  }
}

// -- 地址库分类：大V / 群友 / 其他 + 自建 ---------------------------------
const NEW_CATEGORY = "__new__";

function categoryLabel(value) {
  return ({ EXCHANGE: "交易所（系统）", VALIDATOR: "验证者（系统）" })[value] || value || "未分类";
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
  if (!window.confirm(`删除分类「${name}」？\n\n该分类下的地址会回到“其他”，地址本身不会被删除。`)) return;
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
    const [data, bookData] = await Promise.all([
      api("/api/library"),
      api("/api/library/books").catch(() => ({ items: [] })),
    ]);
    state.library = data.items;
    state.books = bookData.items || [];
    renderBooks();
    renderBookFilter();
    renderLibraryList();
    const selectAll = $("#select-all-addresses");
    if (selectAll) selectAll.addEventListener("change", () => {
      document.querySelectorAll("[data-library-select]").forEach((checkbox) => {
        checkbox.checked = selectAll.checked;
      });
      updateLibrarySelection();
    });
    document.querySelectorAll("[data-library-toggle]").forEach((button) => button.addEventListener("click", () => {
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!expanded));
      const details = document.querySelector(`[data-library-details="${button.dataset.libraryToggle}"]`);
      if (details) details.classList.toggle("hidden", expanded);
    }));
    document.querySelectorAll("[data-library-select]").forEach((checkbox) => {
      checkbox.addEventListener("change", updateLibrarySelection);
    });
    updateLibrarySelection();
    document.querySelectorAll("[data-delete-address]").forEach((button) => button.addEventListener("click", async () => {
      if (!window.confirm("确定删除这条地址库记录？")) return;
      try {
        await api(`/api/library/${button.dataset.deleteAddress}`, { method: "DELETE" });
        await loadLibrary();
        toast("地址已删除。");
      } catch (error) { toast(error.message, true); }
    }));
    document.querySelectorAll("[data-save-library-note]").forEach((button) => button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const input = row?.querySelector(`[data-library-note-input="${button.dataset.saveLibraryNote}"]`);
      if (!input) return;
      button.disabled = true;
      try {
        await api(`/api/library/${button.dataset.saveLibraryNote}`, {
          method: "PATCH",
          body: JSON.stringify({ note: input.value }),
        });
        const item = state.library.find((entry) => String(entry.id) === String(button.dataset.saveLibraryNote));
        if (item) item.note = input.value;
        button.textContent = "已保存";
        window.setTimeout(() => { button.textContent = "保存"; }, 1400);
      } catch (error) { toast(`备注保存失败：${error.message}`, true); }
      finally { button.disabled = false; }
    }));
    document.querySelectorAll("[data-library-exchange-decision]").forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api(`/api/library/${button.dataset.rowId}/confirm-exchange`, {
          method: "POST",
          body: JSON.stringify({ decision: button.dataset.libraryExchangeDecision }),
        });
        await loadLibrary();
        const decision = button.dataset.libraryExchangeDecision;
        toast(decision === "confirm" ? "已确认，扫描时允许过滤该交易所地址。" : decision === "reject" ? "已否定，该地址不会被交易所过滤。" : "已重置交易所人工结论。", false);
      } catch (error) {
        toast(`交易所人工结论更新失败：${error.message}`, true);
        button.disabled = false;
      }
    }));
  } catch (error) { toast(error.message, true); }
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

async function decideExchange(address, decision, button) {
  if (!address) return;
  button.disabled = true;
  try {
    const result = await api("/api/library/confirm-exchange-address", {
      method: "POST",
      body: JSON.stringify({
        address,
        label: decision === "confirm" ? "人工确认交易所" : "人工否定交易所",
        decision,
      }),
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
      row.address_role = decision === "confirm" ? item.label : "已人工否定交易所";
      row.role_reason = decision === "confirm"
        ? "已由用户人工确认并加入交易所过滤库。"
        : "已由用户人工否定，不会进入交易所过滤集合。";
    }
    renderResult(state.lastResult);
    toast(decision === "confirm"
      ? "已人工确认该地址为交易所地址；下次扫描可按交易所过滤。"
      : "已人工否定该地址；后续扫描不会再将其列为交易所候选。", false);
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
    const response = await fetch(`/api/relations/history/${id}/export?format=${encodeURIComponent(format)}`);
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
    toast(`历史记录 #${id} 已导出为 ${format.toUpperCase()}。`);
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
async function syncAddresses({ seed, related, mode = "all", category = "其他", label = "" }) {
  const addresses = (related || []).map((row) => row.address).filter(Boolean);
  return api("/api/relations/sync-library", {
    method: "POST",
    body: JSON.stringify({
      seed: seed || "",
      addresses,
      include_seed: true,
      category,
      label,
      only_library_matched: mode === "matched",
    }),
  });
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
  const button = mode === "matched" ? $("#sync-matched") : $("#sync-all");
  button.disabled = true;
  try {
    const result = await syncAddresses({
      seed: state.lastResult.seed,
      related: state.lastResult.related,
      mode,
      category: $("#sync-category").value,
      label: $("#sync-label").value.trim(),
    });
    toast(`地址库同步完成：${syncSummary(result)}。`);
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
    const label = $("#history-sync-label")?.value.trim() || "";
    const result = await syncAddresses({
      seed: data.result.seed,
      related: data.result.related,
      category,
      label,
    });
    toast(`历史记录 #${id} 已按「${category}」同步到地址库：${syncSummary(result)}。`);
    await loadHistory(state.history.offset);
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
$("#taostats-api-key").addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveTaostatsKey();
});
$("#run-scan").addEventListener("click", runRelations);
$("#cancel-job").addEventListener("click", cancelActiveJob);
$("#taostats-source").addEventListener("change", () => {
  $("#multi-round").disabled = !$("#taostats-source").checked;
});
$("#multi-round").disabled = !$("#taostats-source").checked;
function updateRoundHint() {
  const rounds = Math.min(100, Math.max(1, Number($("#max-rounds").value) || 12));
  $("#round-hint").textContent = `≈ ${rounds * 5} 次请求`;
}
$("#max-rounds").addEventListener("input", updateRoundHint);
$("#max-rounds").addEventListener("change", () => {
  $("#max-rounds").value = Math.min(100, Math.max(1, Number($("#max-rounds").value) || 12));
  updateRoundHint();
});
updateRoundHint();
$("#related-depth-filter").addEventListener("change", (event) => {
  state.depthFilter = event.target.value;
  refreshRelatedTable();
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
  const categoryDelete = event.target.closest("[data-category-delete]");
  if (categoryDelete) deleteCategory(categoryDelete.dataset.categoryDelete);
});
$("#sync-all").addEventListener("click", () => syncCurrentResult("all"));
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
  if (!window.confirm(`确定批量删除选中的 ${ids.length} 条地址库记录？此操作不可撤销。`)) return;
  try {
    const result = await api("/api/library", {
      method: "DELETE",
      body: JSON.stringify({ ids }),
    });
    await loadLibrary();
    toast(`已批量删除 ${result.deleted} 条记录。`);
  } catch (error) { toast(error.message, true); }
});
$("#export-json").addEventListener("click", () => {
  if (!state.lastResult) { toast("先完成一次关联扫描。", true); return; }
  const exportData = {
    seed: state.lastResult.seed,
    max_depth: state.lastResult.max_depth,
    data_source: state.lastResult.data_source,
    coverage: state.lastResult.coverage,
    data_cutoff_block: state.lastResult.data_cutoff_block,
    seed_balance_tao: state.lastResult.seed_balance_tao,
    policy: state.lastResult.policy,
    related: (state.lastResult.related || []).map((row) => ({
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
  link.download = `bittensor-link-${Date.now()}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
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
  const { addresses, skippedExchange, skippedValidator } = selectExportableAddresses(state.lastResult.related);
  if (!addresses.length) {
    toast(`没有可导出的地址（已排除 ${skippedExchange} 个交易所、${skippedValidator} 个验证者）。`, true);
    return;
  }
  const blob = new Blob([addresses.join("\n") + "\n"], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `bittensor-addresses-${String(state.lastResult.seed || "").slice(0, 8)}-${Date.now()}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
  toast(`已导出 ${addresses.length} 个地址（每行一个）；已排除已确认交易所 ${skippedExchange} 个、验证者 ${skippedValidator} 个。`);
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
