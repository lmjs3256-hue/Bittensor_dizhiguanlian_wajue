"use strict";

const $ = (selector) => document.querySelector(selector);
const state = { lastResult: null, library: [], depthFilter: "all" };

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

function formatWalletValue(total, status = "unavailable") {
  if (total === null || total === undefined || total === "") {
    return status === "partial" ? "部分获取" : "未获取";
  }
  const value = formatTao(total, true);
  return status === "partial" ? `${value}（部分获取）` : value;
}

function walletTooltip(row) {
  const available = formatTao(row.available_tao, true);
  const staked = formatTao(row.staked_tao, true);
  return `可用 ${available} · 已质押折合 ${staked}`;
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

function showTaostatsKeyStatus(configured) {
  const status = $("#taostats-status");
  const detail = $("#taostats-key-status");
  if (typeof configured !== "boolean") {
    status.textContent = "后台版本较旧；请重启服务后设置 API key";
    detail.textContent = "设置 API key 前请先更新并重启服务。";
    $("#clear-taostats-key").disabled = true;
    return;
  }
  status.textContent = configured ? "API key 已配置" : "尚未设置 API key";
  detail.textContent = configured
    ? "当前服务已保存 key（不回显）；服务重启后需要重新输入。"
    : "尚未保存 key。";
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

function navigate(view) {
  document.querySelectorAll(".nav").forEach((button) => {
    button.classList.toggle("active", button.dataset.nav === view);
  });
  ["scan", "library", "method", "release"].forEach((name) => {
    $(`#view-${name}`).classList.toggle("hidden", name !== view);
  });
  if (view === "library") loadLibrary();
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
    showTaostatsKeyStatus(data.taostats_configured);
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
    showTaostatsKeyStatus(data.configured);
    toast("API key 已保存到当前服务内存。", false);
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
    showTaostatsKeyStatus(data.configured);
    toast("当前服务中的 API key 已清除。", false);
  } catch (error) {
    toast(`清除 API key 失败：${error.message}`, true);
    button.disabled = false;
  }
}

function relatedTable(rows) {
  if (!rows.length) return '<div class="placeholder">没有达到过滤条件的关联地址。</div>';
  return `<table><thead><tr><th>关联钱包地址</th><th>钱包总价值 TAO</th><th>层级</th><th>流入 TAO</th><th>流出 TAO</th><th>累计转账 TAO</th><th>地址库</th><th>备注</th></tr></thead><tbody>${rows.map((row) => {
    const role = row.address_role
      ? `<span class="tag role-tag" title="${esc(row.role_reason || "")}">${esc(row.address_role)}</span>`
      : "";
    const library = row.match
      ? `<span class="tag warn">${esc(libraryName(row.library_label, row.library_category))}</span>`
      : "-";
    const exchangeActions = row.address_role === "疑似交易所（待确认）"
      ? `<div class="exchange-decision"><button class="secondary note-save" data-exchange-decision="confirm" data-address="${esc(row.address)}" type="button">确认交易所</button><button class="ghost note-save" data-exchange-decision="reject" data-address="${esc(row.address)}" type="button">否定</button></div>`
      : "";
    return `<tr data-related-address="${esc(row.address)}"><td class="address related-address-cell"><div class="address-main"><code title="${esc(row.address)}">${esc(shortAddr(row.address))}</code><button class="ghost copy-address" data-copy-address="${esc(row.address)}" title="复制完整地址" type="button">复制</button>${role}</div></td><td class="amount" title="${esc(walletTooltip(row))}">${esc(formatWalletValue(row.balance_tao, row.balance_status))}</td><td>${row.depth}</td><td class="amount">${esc(formatTao(row.incoming_tao))}</td><td class="amount">${esc(formatTao(row.outgoing_tao))}</td><td class="amount">${esc(formatTao(row.total_tao))}</td><td>${library}${exchangeActions}</td><td><div class="note-editor"><textarea data-relation-note-input placeholder="备注此地址" maxlength="2000">${esc(row.library_note || "")}</textarea><button class="ghost note-save" data-save-relation-note type="button">保存备注</button></div></td></tr>`;
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

function renderResult(data) {
  state.lastResult = data;
  $("#empty").classList.add("hidden");
  $("#result").classList.remove("hidden");
  const sourcesLabel = (data.data_sources_used || []).map(sourceLabel).join(" + ") || "无数据来源";
  const apiUsage = data.data_source === "taostats"
    ? ` · API 请求 ${data.api_requests} 次 · 返回转账 ${data.taostats_transfer_count} 条 · 缓存命中 ${data.api_cache_hits} 次`
    : "";
  $("#result-subtitle").textContent = `${data.seed} · 最多 ${data.max_depth} 级 · 来源 ${sourcesLabel}${apiUsage} · 数据截止 ${data.data_cutoff_block ? number(data.data_cutoff_block) : "未采集"} 区块`;
  $("#seed-result-address").textContent = data.seed || "-";
  $("#seed-result-balance").textContent = formatWalletValue(data.seed_balance_tao, data.seed_balance_status);
  $("#seed-result-available").textContent = `可用 ${formatTao(data.seed_available_tao, true)}`;
  $("#seed-result-staked").textContent = `已质押折合 ${formatTao(data.seed_staked_tao, true)}`;
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
  $("#warnings").innerHTML = [...data.warnings, ...data.limitations].map((text) => `<li>${esc(text)}</li>`).join("");
  document.querySelectorAll("[data-use-address]").forEach((button) => button.addEventListener("click", () => {
    $("#seed").value = button.dataset.useAddress;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
}

async function runRelations() {
  const seed = $("#seed").value.trim();
  if (!seed) { toast("请先输入起始地址。", true); return; }
  const button = $("#run-scan");
  button.disabled = true;
  button.textContent = "扫描中…";
  try {
    const direction = $("#direction").value;
    const requestedSource = $("#taostats-source").checked ? "taostats" : "local";
    const data = await api("/api/relations", {
      method: "POST",
      body: JSON.stringify({
        seed,
        depth: Number($("#depth").value),
        min_edge_tao: $("#min-edge").value,
        min_total_tao: $("#min-total").value,
        include_incoming: direction !== "outgoing",
        include_outgoing: direction !== "incoming",
        data_source: requestedSource,
        filter_exchanges: $("#filter-exchanges").checked,
        filter_validators: $("#filter-validators").checked,
      }),
    });
    if (requestedSource === "taostats" && data.data_source !== "taostats") {
      toast("当前后台尚未加载 Taostats 接入版本。请在项目 PowerShell 窗口执行 .\\stop.ps1，再执行 .\\start.ps1。", true);
      return;
    }
    renderResult(data);
    const libraryHitCount = data.library_matches.length + (data.seed_library_matches || []).length;
    toast(`查询完成：${data.related.length} 个关联地址，${libraryHitCount} 个地址库命中。${data.pagination_complete === false ? "部分历史尚未读取完，请查看提示。" : ""}`, data.pagination_complete === false);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "扫描关联";
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
  return `<table><thead><tr><th class="selection-cell"></th><th>地址</th><th>名称</th><th>分类</th><th>备注</th><th>来源</th><th></th></tr></thead><tbody>${rows.map((row) => { const exchangeControl = row.category === "EXCHANGE" ? `<div class="exchange-confirm-control"><span class="tag ${row.exchange_confirmed ? "" : "warn"}">${row.exchange_confirmed ? "已人工确认" : row.exchange_rejected ? "已人工否定" : "待人工确认"}</span>${row.exchange_confirmed ? `<button class="ghost note-save" data-library-exchange-decision="reset" data-row-id="${row.id}" type="button">撤销确认</button>` : row.exchange_rejected ? `<button class="secondary note-save" data-library-exchange-decision="confirm" data-row-id="${row.id}" type="button">重新确认</button><button class="ghost note-save" data-library-exchange-decision="reset" data-row-id="${row.id}" type="button">撤销否定</button>` : `<button class="secondary note-save" data-library-exchange-decision="confirm" data-row-id="${row.id}" type="button">确认交易所</button><button class="ghost note-save" data-library-exchange-decision="reject" data-row-id="${row.id}" type="button">否定交易所</button>`}</div>` : ""; return `<tr><td class="selection-cell"><input type="checkbox" data-library-select value="${row.id}" aria-label="选择第 ${row.id} 条地址"></td><td class="mono address">${esc(row.address)}</td><td>${esc(row.label || "-")}</td><td><span class="lib-category ${esc(row.category)}">${esc(row.category)}</span>${exchangeControl}</td><td><div class="library-note-editor"><textarea data-library-note-input="${row.id}" maxlength="2000" placeholder="添加备注">${esc(row.note || "")}</textarea><button class="ghost note-save" data-save-library-note="${row.id}" type="button">保存</button></div></td><td>${esc(row.source || "-")}</td><td><button class="danger" data-delete-address="${row.id}" type="button">删除</button></td></tr>`; }).join("")}</tbody></table>`;
}

function libraryTable(rows) {
  if (!rows.length) return '<div class="placeholder">地址库为空。可通过上方单个添加或导入 TXT/CSV/JSON。</div>';
  const groups = new Map();
  rows.forEach((row) => {
    const label = (row.label || `${row.category} · 未命名`).trim();
    const key = `${row.category}\u0000${label}`;
    if (!groups.has(key)) groups.set(key, { label, category: row.category, rows: [] });
    groups.get(key).rows.push(row);
  });
  return `<div class="library-groups">${[...groups.values()].map((group, index) => {
    const groupId = `library-group-${index}`;
    return `<section class="library-group"><button class="library-group-toggle" data-library-toggle="${groupId}" aria-expanded="false" type="button"><span><b>${esc(group.label)}</b><span class="lib-category ${esc(group.category)}">${esc(group.category)}</span></span><span>${group.rows.length} 个地址 <span class="group-chevron">⌄</span></span></button><div class="library-group-details hidden" data-library-details="${groupId}">${libraryRowsTable(group.rows)}</div></section>`;
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

async function loadLibrary() {
  try {
    const data = await api("/api/library");
    state.library = data.items;
    $("#library-count").textContent = `${data.items.length} 条`;
    $("#library-table").innerHTML = libraryTable(data.items);
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
    seed_available_tao: state.lastResult.seed_available_tao,
    seed_staked_tao: state.lastResult.seed_staked_tao,
    policy: state.lastResult.policy,
    related: (state.lastResult.related || []).map((row) => ({
      address: row.address,
      depth: row.depth,
      incoming_tao: row.incoming_tao,
      outgoing_tao: row.outgoing_tao,
      total_tao: row.total_tao,
      balance_tao: row.balance_tao,
      available_tao: row.available_tao,
      staked_tao: row.staked_tao,
      balance_status: row.balance_status,
      match: row.match,
      library_label: row.library_label,
      library_category: row.library_category,
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
$("#seed").addEventListener("keydown", (event) => { if (event.key === "Enter") runRelations(); });
loadOverview();
loadVersionInfo();
