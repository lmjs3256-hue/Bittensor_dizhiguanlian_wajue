
const APP_BASE = "/event-monitor";
const APP_EMBED = new URLSearchParams(location.search).get("embed") === "1";
if (APP_EMBED) document.documentElement.classList.add("embedded");

function applyWalletTheme() {
  try {
    const mode = localStorage.getItem("wallet-link-theme");
    if (mode === "warm" || (!mode && localStorage.getItem("wallet-link-eye-mode") === "on")) {
      document.documentElement.dataset.theme = "eye";
    } else if (mode === "darkgray") {
      document.documentElement.dataset.theme = "darkgray";
    } else {
      delete document.documentElement.dataset.theme;
    }
  } catch (_) {
    delete document.documentElement.dataset.theme;
  }
}
applyWalletTheme();
window.addEventListener("storage", (event) => {
  if (event.key === "wallet-link-theme" || event.key === "wallet-link-eye-mode") {
    applyWalletTheme();
  }
});

function routePathname(pathname = location.pathname) {
  if (pathname === APP_BASE) return "/";
  if (pathname.startsWith(`${APP_BASE}/`)) return pathname.slice(APP_BASE.length);
  return pathname;
}

function withEmbedQuery(url) {
  if (APP_EMBED) url.searchParams.set("embed", "1");
  return url;
}

function eventRoute(path) {
  const url = withEmbedQuery(new URL(path, location.origin));
  const route = routePathname(url.pathname);
  if (route === "/") url.pathname = APP_BASE;
  else if (!url.pathname.startsWith(`${APP_BASE}/`)) url.pathname = `${APP_BASE}${route}`;
  return url.pathname + url.search + url.hash;
}

function rewriteEventLink(link) {
  const raw = link.getAttribute("href");
  if (!raw || raw.startsWith("#") || raw.startsWith("javascript:")) return;
  const url = new URL(raw, location.href);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/event-api/")) return;
  if (url.pathname.startsWith("/api/")) {
    url.pathname = `/event-api${url.pathname}`;
  } else {
    const route = routePathname(url.pathname);
    const internal = route === "/"
      || ["/profiles", "/priorities", "/addresses", "/records", "/logs", "/features", "/guide", "/changelog", "/settings"].includes(route)
      || /^\/subnets\/\d+$/.test(route)
      || /^\/events\/[0-9a-f]{64}$/.test(route);
    if (!internal) return;
    url.pathname = route === "/" ? APP_BASE : `${APP_BASE}${route}`;
  }
  withEmbedQuery(url);
  const next = url.pathname + url.search + url.hash;
  if (next !== raw) link.setAttribute("href", next);
}

"use strict";

const UI_KEYS = {
  panelCleared: "subnetPulse.panelCleared",
  eventFontSize: "subnetPulse.eventFontSize",
  floatingMode: "subnetPulse.floatingMode.v2",
  inlineBounds: "subnetPulse.inlineFloatingBounds",
};
const FONT_MIN = 11;
const FONT_MAX = 18;
let uiChannel = null;
try {
  if ("BroadcastChannel" in window) uiChannel = new BroadcastChannel("subnet-pulse-ui");
} catch {
  uiChannel = null;
}

const state = {
  events: [],
  eventsTotal: 0,
  eventTypes: [],
  currentPage: 1,
  pageSize: 12,
  subnetFilter: null,
  eventFontSize: readStoredEventFontSize(),
  panelCleared: readStoredPanelCleared(),
  selectedId: null,
  paused: false,
  pending: [],
  route: parseRoute(),
  product: null,
  profiles: [],
  watchedAddresses: [],
  simulationSignals: [],
  stream: null,
  streamState: "connecting",
  streamConnectedAt: null,
  status: null,
  statusReceivedAt: null,
  statusErrorAt: null,
  lastObservedHeight: null,
  lastHeightChangedAt: null,
  toastTimer: null,
  logTimer: null,
};

const el = {
  eventList: document.querySelector("#event-list"),
  empty: document.querySelector("#empty-state"),
  count: document.querySelector("#event-count"),
  pendingCount: document.querySelector("#pending-count"),
  pendingUpdates: document.querySelector("#pending-updates"),
  chainHeight: document.querySelector("#chain-height"),
  finalizedHeight: document.querySelector("#finalized-height"),
  chainLag: document.querySelector("#chain-lag"),
  liveStatus: document.querySelector("#live-status"),
  detail: document.querySelector("#detail-panel"),
  pageTitle: document.querySelector("#page-title"),
  pageSubtitle: document.querySelector("#page-subtitle"),
  feedKicker: document.querySelector("#feed-kicker"),
  feedTitle: document.querySelector("#feed-title"),
  listeningIndicator: document.querySelector("#listening-indicator"),
  listeningLabel: document.querySelector("#listening-label"),
  listeningDetail: document.querySelector("#listening-detail"),
  pause: document.querySelector("#pause-toggle"),
  search: document.querySelector("#search-input"),
  subnetFilter: document.querySelector("#subnet-filter"),
  subnetQuery: document.querySelector("#subnet-query-button"),
  typeFilter: document.querySelector("#type-filter"),
  statusFilter: document.querySelector("#status-filter"),
  sourceFilter: document.querySelector("#source-filter"),
  floatingButton: document.querySelector("#floating-button"),
  pagination: document.querySelector("#pagination"),
  pageFirst: document.querySelector("#page-first"),
  pagePrev: document.querySelector("#page-prev"),
  pageInput: document.querySelector("#page-input"),
  pageTotal: document.querySelector("#page-total"),
  pageNext: document.querySelector("#page-next"),
  pageLast: document.querySelector("#page-last"),
  pageRange: document.querySelector("#page-range"),
  alertBanner: document.querySelector("#alert-banner"),
  alertTitle: document.querySelector("#alert-title"),
  alertCopy: document.querySelector("#alert-copy"),
  monitorHealth: document.querySelector("#monitor-health"),
  chainHealth: document.querySelector("#chain-health"),
  chainHealthCopy: document.querySelector("#chain-health-copy"),
  chainHeartbeat: document.querySelector("#chain-heartbeat"),
  chainReset: document.querySelector("#chain-reset-button"),
  githubHealth: document.querySelector("#github-health"),
  githubHealthCopy: document.querySelector("#github-health-copy"),
  githubHeartbeat: document.querySelector("#github-heartbeat"),
  githubApiSettings: document.querySelector("#github-api-settings"),
  streamHealth: document.querySelector("#stream-health"),
  streamHealthCopy: document.querySelector("#stream-health-copy"),
  streamHeartbeat: document.querySelector("#stream-heartbeat"),
  eventHealth: document.querySelector("#event-health"),
  eventHealthCopy: document.querySelector("#event-health-copy"),
  eventHeartbeat: document.querySelector("#event-heartbeat"),
  emptyTitle: document.querySelector("#empty-title"),
  emptyCopy: document.querySelector("#empty-copy"),
  emptyHeartbeat: document.querySelector("#empty-heartbeat"),
  refresh: document.querySelector("#refresh-button"),
  simulationButton: document.querySelector("#simulation-button"),
  clearPanelButton: document.querySelector("#clear-panel-button"),
  floatingDialog: document.querySelector("#floating-dialog"),
  floatingForm: document.querySelector("#floating-form"),
  floatingClose: document.querySelector("#floating-close"),
  eventFontSizeInput: document.querySelector("#event-font-size"),
  eventFontSizeOutput: document.querySelector("#event-font-size-output"),
  inlineFloating: document.querySelector("#inline-floating"),
  inlineFloatingHead: document.querySelector("#inline-floating-head"),
  inlineFloatingFrame: document.querySelector("#inline-floating-frame"),
  inlineFloatingClose: document.querySelector("#inline-floating-close"),
  simulationDialog: document.querySelector("#simulation-dialog"),
  simulationForm: document.querySelector("#simulation-form"),
  simulationClose: document.querySelector("#simulation-close"),
  simulationSignal: document.querySelector("#simulation-signal"),
  simulationAddressField: document.querySelector("#simulation-address-field"),
  simulationAddress: document.querySelector("#simulation-address"),
  simulationNetuidField: document.querySelector("#simulation-netuid-field"),
  simulationCompletion: document.querySelector("#simulation-completion"),
  simulationDelay: document.querySelector(".completion-delay"),
  simulationSummary: document.querySelector("#simulation-summary"),
  simulationMeaning: document.querySelector("#simulation-meaning"),
  simulationError: document.querySelector("#simulation-error"),
  deleteSimulations: document.querySelector("#delete-simulations"),
  repoButton: document.querySelector("#repo-button"),
  repoDialog: document.querySelector("#repo-dialog"),
  repoForm: document.querySelector("#repo-form"),
  repoClose: document.querySelector("#repo-close"),
  repoError: document.querySelector("#repo-error"),
  toast: document.querySelector("#toast"),
  infoView: document.querySelector("#info-view"),
  detailBackdrop: document.querySelector("#detail-backdrop"),
  infoMenuSummary: document.querySelector(".info-menu summary"),
  profileDialog: document.querySelector("#profile-dialog"),
  profileForm: document.querySelector("#profile-form"),
  profileClose: document.querySelector("#profile-close"),
  profileError: document.querySelector("#profile-error"),
  profileDialogTitle: document.querySelector("#profile-dialog-title"),
  profileSourceSummary: document.querySelector("#profile-source-summary"),
  metrics: document.querySelector(".metrics"),
  workspace: document.querySelector(".workspace"),
};

function parseRoute() {
  const path = routePathname();
  const event = path.match(/^\/events\/([0-9a-f]{64})$/);
  if (event) return { kind: "event", eventId: event[1] };
  const subnet = path.match(/^\/subnets\/(\d+)$/);
  if (subnet) return { kind: "subnet", netuid: Number(subnet[1]) };
  const info = path.match(/^\/(profiles|priorities|addresses|records|logs|features|guide|changelog|settings)$/);
  if (info) return { kind: info[1] };
  return { kind: "home" };
}

function clampEventFontSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size)) return 13;
  return Math.max(FONT_MIN, Math.min(FONT_MAX, Math.round(size)));
}

function readStoredEventFontSize() {
  try {
    return clampEventFontSize(localStorage.getItem(UI_KEYS.eventFontSize) || 13);
  } catch {
    return 13;
  }
}

function readStoredPanelCleared() {
  try {
    return localStorage.getItem(UI_KEYS.panelCleared) === "true";
  } catch {
    return false;
  }
}

function readStoredFloatingMode() {
  try {
    const value = localStorage.getItem(UI_KEYS.floatingMode);
    return ["inline", "window", "pip"].includes(value) ? value : "pip";
  } catch {
    return "pip";
  }
}

function broadcastUi(type, value) {
  try {
    uiChannel?.postMessage({ type, value });
  } catch {
    // Same-origin storage events remain available when BroadcastChannel is blocked.
  }
}

function applyEventFontSize(value, options = {}) {
  const size = clampEventFontSize(value);
  state.eventFontSize = size;
  document.documentElement.style.setProperty("--event-font-size", `${size}px`);
  if (el.eventFontSizeInput) el.eventFontSizeInput.value = String(size);
  if (el.eventFontSizeOutput) el.eventFontSizeOutput.textContent = `${size} px`;
  if (options.persist !== false) {
    try {
      localStorage.setItem(UI_KEYS.eventFontSize, String(size));
    } catch {
      // The current tab still uses the selected size.
    }
  }
  if (options.broadcast !== false) broadcastUi("event-font-size", size);
  return size;
}

function setPanelCleared(cleared, options = {}) {
  state.panelCleared = Boolean(cleared);
  if (options.persist !== false) {
    try {
      localStorage.setItem(UI_KEYS.panelCleared, state.panelCleared ? "true" : "false");
    } catch {
      // The in-memory state still applies to this tab.
    }
  }
  if (options.broadcast !== false) broadcastUi("panel-cleared", state.panelCleared);
  if (options.render !== false) render();
}

function applyExternalUiState(type, value) {
  if (type === "event-font-size") {
    applyEventFontSize(value, { persist: false, broadcast: false });
  } else if (type === "panel-cleared") {
    state.panelCleared = Boolean(value);
    render();
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

async function loadEvents(page = state.currentPage) {
  state.currentPage = Math.max(1, Number(page) || 1);
  const params = new URLSearchParams({
    network: "finney",
    limit: String(state.pageSize),
    offset: String((state.currentPage - 1) * state.pageSize),
  });
  if (state.route.kind === "subnet") {
    params.set("netuid", String(state.route.netuid));
    params.set("order", "asc");
  } else if (state.subnetFilter != null) {
    params.set("netuid", String(state.subnetFilter));
  }
  for (const [key, value] of [
    ["q", el.search.value.trim()],
    ["event_type", el.typeFilter.value],
    ["chain_status", el.statusFilter.value],
    ["source", el.sourceFilter.value],
  ]) {
    if (value) params.set(key, value);
  }
  const data = await request(`/event-api/events?${params}`);
  state.events = data.events || [];
  state.eventsTotal = Number(data.total || 0);
  state.eventTypes = data.event_types || [];
  const pages = totalPages();
  if (state.currentPage > pages && pages > 0) {
    return loadEvents(pages);
  }
  populateTypes();
  render();
}

async function loadStatus() {
  try {
    const data = await request("/event-api/status");
    state.status = data;
    state.statusReceivedAt = Date.now();
    state.statusErrorAt = null;
    renderStatus(data);
  } catch (error) {
    state.statusErrorAt = Date.now();
    el.liveStatus.className = "live-status offline";
    el.liveStatus.innerHTML = "<span></span>本地状态接口无响应";
    el.chainHealth.className = "health-item state-off";
    el.chainHealthCopy.textContent = "无法读取链上心跳，请检查本地服务或网络";
    renderHeartbeatCountdown();
  }
}

async function loadProduct() {
  state.product = await request("/event-api/product");
}

function renderStatus(data) {
  const chain = data.workers?.chain;
  const details = chain?.details || {};
  const github = data.workers?.github;
  const githubDetails = github?.details || {};
  const now = Date.now();
  el.chainHeight.textContent = details.best_height ? Number(details.best_height).toLocaleString("zh-CN") : "—";
  el.finalizedHeight.textContent = details.finalized_height ? Number(details.finalized_height).toLocaleString("zh-CN") : "—";
  el.chainLag.textContent = details.lag_blocks == null ? "等待链上采集" : `落后 ${details.lag_blocks} 个区块`;

  const observedHeight = Number(details.best_height);
  if (Number.isFinite(observedHeight) && observedHeight > 0) {
    if (state.lastObservedHeight !== observedHeight) {
      state.lastObservedHeight = observedHeight;
      state.lastHeightChangedAt = now;
    }
  } else {
    state.lastObservedHeight = null;
  }

  const chainAge = ageSeconds(chain?.updated_at);
  const chainFresh = chainAge != null && chainAge <= 30;
  const chainNormal = chain?.state === "connected" && chainFresh;
  el.chainHealth.className = `health-item ${chainNormal ? "state-on" : "state-off"}`;
  el.chainHealthCopy.textContent = chain?.state === "disabled"
    ? "链上采集已关闭"
    : chainNormal
      ? `区块高度 ${formatNumber(details.best_height)} · 最终确认 ${formatNumber(details.finalized_height)}`
      : chainFresh
        ? `采集器${chainStateText(chain?.state)}：${details.error || "等待下一步重试"}`
        : "超过 30 秒未收到链上心跳，可能网络中断";

  const githubState = github?.state || "waiting";
  const githubText = githubState === "connected"
    ? `本轮 ${formatNumber(githubDetails.repositories)} 个仓库 · 队列 ${formatNumber(githubDetails.repository_queue_total)}`
    : githubState === "running"
      ? `正在轮询 ${formatNumber(githubDetails.repositories)} 个仓库 · ${formatNumber(githubDetails.workers || 1)} 并发`
      : githubState === "partial"
        ? `部分成功 ${formatNumber(githubDetails.successes)} / ${formatNumber(githubDetails.repositories)} 个，稍后重试失败仓库`
        : githubState === "limited"
        ? `API 限流 · 队列 ${formatNumber(githubDetails.repository_queue_total)} 个，不影响链上`
        : githubState === "disabled"
        ? "GitHub 采集已关闭"
        : githubState === "waiting"
          ? "等待首次轮询"
          : `轮询异常${githubDetails.failures ? ` · ${githubDetails.failures} 个仓库失败` : ""}`;
  el.githubHealth.className = `health-item ${
    githubState === "connected"
      ? "state-on"
      : ["partial", "limited"].includes(githubState)
        ? "state-warn"
        : ["waiting", "disabled", "running"].includes(githubState)
          ? "state-idle"
          : "state-off"
  }`;
  el.githubHealthCopy.textContent = githubText;
  const apiLimit = githubDetails.api_rate_limit || {};
  const configured = Boolean(data.runtime?.github_token_configured);
  const pollSeconds = Number(data.runtime?.github_poll_seconds || 300);
  const batchSize = Number(data.runtime?.github_batch_size || 1);
  const intervalText = pollSeconds < 60
    ? `${formatNumber(pollSeconds)} 秒/轮`
    : `${formatNumber(Math.round(pollSeconds / 60))} 分钟/轮`;
  const quotaText = apiLimit.remaining == null || apiLimit.limit == null
    ? "配额等待首轮"
    : `剩余 ${formatNumber(apiLimit.remaining)}/${formatNumber(apiLimit.limit)}`;
  if (el.githubApiSettings) {
    el.githubApiSettings.textContent = `${configured ? "认证 API" : "匿名 API"} · ${intervalText} · 每轮 ${formatNumber(batchSize)} 仓库 · ${quotaText}`;
    el.githubApiSettings.title = apiLimit.reset_at
      ? `GitHub API ${quotaText}；配额预计 ${formatFullTime(apiLimit.reset_at)} 重置。点击打开系统设置。`
      : `GitHub API ${quotaText}。点击打开系统设置。`;
  }

  const alerts = data.alerts || [];
  renderAlerts({ chain, details, github, githubDetails, alerts, chainNormal });
  renderStreamHealth();
  renderHeartbeatCountdown();
  renderEmptyState();
}

function renderAlerts({ chain, details, github, githubDetails, alerts, chainNormal }) {
  const chainAlerts = alerts.filter((alert) => String(alert.component).startsWith("chain"));
  const githubAlerts = alerts.filter((alert) => String(alert.component).startsWith("github"));
  const githubRateAlert = githubAlerts.find((alert) => /rate limit|速率限制/i.test(alert.message || ""));
  const otherAlerts = alerts.filter((alert) => (
    !String(alert.component).startsWith("github") &&
    !String(alert.component).startsWith("chain")
  ));
  let title = "";
  let copy = "";
  let kind = "";

  if (!chainNormal && chain?.state !== "disabled") {
    title = "Finney 链上连接异常";
    copy = chainAlerts[0]?.message || details.error || "链上心跳已停止，系统正在自动重连。";
    kind = "critical";
  } else if (github?.state === "partial") {
    title = "GitHub 部分仓库暂时不可用";
    copy = `本轮成功 ${formatNumber(githubDetails.successes)} 个、失败 ${formatNumber(githubDetails.failures)} 个。失败仓库已保留游标，下一轮会重试，链上监控不受影响。`;
    kind = "limited";
  } else if (github?.state === "limited" || githubRateAlert) {
    title = "GitHub 限流，链上监控正常";
    copy = "匿名 GitHub API 配额已用尽。将 .env.example 复制为 .env，填写 GITHUB_TOKEN 后重启服务，可提高到每小时 5000 次请求；仓库游标已经保留。";
    if (githubDetails.rate_limit_reset_at) {
      copy += ` 当前配额预计在 ${formatFullTime(githubDetails.rate_limit_reset_at)} 恢复。`;
    }
    kind = "limited";
  } else if (github?.state === "degraded") {
    title = "GitHub 轮询异常，链上监控正常";
    copy = githubAlerts[0]?.message || "部分仓库暂时无法轮询，系统会保留游标并重试。";
    kind = "limited";
  } else if (chainAlerts.length || otherAlerts.length) {
    const alert = chainAlerts[0] || otherAlerts[0];
    title = alert.component.startsWith("chain_enrichment") ? "事件补全异常" : "系统提示";
    copy = alert.message;
    kind = "limited";
  } else if (alerts.length) {
    title = "系统提示";
    copy = `${alerts[0].component}：${alerts[0].message}`;
    kind = "limited";
  }

  el.alertBanner.hidden = !title;
  el.alertBanner.className = `alert-banner ${kind}`;
  el.alertTitle.textContent = title;
  el.alertCopy.textContent = copy;
}

function renderStreamHealth() {
  const connected = state.streamState === "connected";
  const reconnecting = state.streamState === "reconnecting";
  el.liveStatus.className = connected ? "live-status online" : "live-status offline";
  el.liveStatus.innerHTML = connected
    ? "<span></span>实时通道已连接"
    : `<span></span>${reconnecting ? "实时通道重连中" : "正在连接实时通道"}`;
  el.streamHealth.className = `health-item ${connected ? "state-on" : "state-off"}`;
  el.streamHealthCopy.textContent = connected
    ? "SSE 已连接，服务端每 15 秒发送保活信号"
    : reconnecting
      ? "实时通道断开，浏览器正在自动重连"
      : "正在建立实时通道";
  renderListeningIndicator();
}

function renderHeartbeatCountdown() {
  const chain = state.status?.workers?.chain;
  const github = state.status?.workers?.github;
  const chainDetails = chain?.details || {};
  const githubDetails = github?.details || {};
  const chainAge = ageSeconds(chain?.updated_at);
  const githubAge = ageSeconds(github?.updated_at);
  el.chainHeartbeat.textContent = state.statusErrorAt
    ? "刚刚请求失败"
    : formatAge(chainAge, "刚刚更新");
  el.chainHeartbeat.title = chain?.updated_at ? `最后心跳：${formatFullTime(chain.updated_at)}` : "尚未收到心跳";
  el.githubHeartbeat.textContent = formatAge(githubAge, "等待首次检查");
  el.githubHeartbeat.title = github?.updated_at ? `最后检查：${formatFullTime(github.updated_at)}` : "尚未完成首次检查";
  el.streamHeartbeat.textContent = state.streamState === "connected"
    ? `连接 ${formatAge(ageSeconds(state.streamConnectedAt), "刚刚", "刚刚")}`
    : "等待重连";

  const latest = state.events.reduce((current, event) => (
    !current || new Date(event.event_at) > new Date(current.event_at) ? event : current
  ), null);
  if (latest) {
    const latestAge = ageSeconds(latest.event_at);
    el.eventHealth.className = "health-item state-on";
    el.eventHealthCopy.textContent = `最近事件 SN${latest.netuid ?? "—"} · ${latest.event_type}`;
    el.eventHeartbeat.textContent = formatAge(latestAge, "刚刚");
    el.eventHeartbeat.title = formatFullTime(latest.event_at);
  } else {
    const chainAgeNow = ageSeconds(chain?.updated_at);
    const healthy = chain?.state === "connected" && chainAgeNow != null && chainAgeNow <= 30;
    el.eventHealth.className = `health-item ${healthy ? "state-on" : "state-idle"}`;
    el.eventHealthCopy.textContent = healthy
      ? "尚无事件；链上轮询仍在持续更新"
      : "尚无事件；等待采集器恢复";
    el.eventHeartbeat.textContent = healthy ? "监听中" : "无心跳";
  }

  if (state.statusReceivedAt) {
    const apiAge = Math.max(0, Math.floor((Date.now() - state.statusReceivedAt) / 1000));
    el.chainLag.title = `浏览器最近一次成功读取状态：${apiAge} 秒前`;
    el.githubHealth.title = `浏览器最近一次成功读取状态：${apiAge} 秒前`;
  }
  renderListeningIndicator();
  renderEmptyState();
}

function renderListeningIndicator() {
  const chain = state.status?.workers?.chain;
  const details = chain?.details || {};
  const chainAge = ageSeconds(chain?.updated_at);
  const chainLive = chain?.state === "connected" && chainAge != null && chainAge <= 30;
  const streamLive = state.streamState === "connected";
  const live = chainLive && streamLive;
  el.listeningIndicator.classList.toggle("is-live", live);
  el.listeningIndicator.classList.toggle("is-interrupted", !live && state.streamState === "reconnecting");
  if (live) {
    el.listeningIndicator.dataset.state = "live";
    el.listeningLabel.textContent = "正在实时监听";
    el.listeningDetail.textContent = `区块 ${formatNumber(details.best_height)} · ${formatAge(chainAge, "刚刚更新")}`;
  } else if (state.streamState === "reconnecting") {
    el.listeningIndicator.dataset.state = "offline";
    el.listeningLabel.textContent = "网络断开，监听已暂停";
    el.listeningDetail.textContent = "实时通道正在自动重连";
  } else if (chain?.state === "degraded") {
    el.listeningIndicator.dataset.state = "offline";
    el.listeningLabel.textContent = "链上连接异常，监听已暂停";
    el.listeningDetail.textContent = details.error || "等待 RPC 自动重连";
  } else if (chainAge != null && chainAge > 30) {
    el.listeningIndicator.dataset.state = "offline";
    el.listeningLabel.textContent = "链上心跳中断，监听已暂停";
    el.listeningDetail.textContent = `超过 ${chainAge} 秒未收到新区块`;
  } else {
    el.listeningIndicator.dataset.state = "waiting";
    el.listeningLabel.textContent = "正在连接监听";
    el.listeningDetail.textContent = "等待链上心跳与实时通道";
  }
}

function renderEmptyState() {
  if (!el.emptyCopy) return;
  if (state.eventsTotal > 0) {
    return;
  }
  if (state.subnetFilter != null) {
    el.emptyTitle.textContent = `SN${state.subnetFilter} 暂无本地事件`;
    el.emptyCopy.textContent = "该子网目前没有匹配查询条件的事件。可清空子网编号后查看全部事件流。";
    el.emptyHeartbeat.textContent = "查询范围为本地 SQLite 已采集记录";
    return;
  }
  const chain = state.status?.workers?.chain;
  const chainAge = ageSeconds(chain?.updated_at);
  const healthy = chain?.state === "connected" && chainAge != null && chainAge <= 30;
  el.emptyTitle.textContent = healthy ? "轮询正常，等待第一条事件" : "当前没有事件，正在检查采集器";
  el.emptyCopy.textContent = healthy
    ? "没有新事件时，区块高度和心跳仍会持续变化。低于 30 秒未更新才提示可能中断。"
    : "若链上心跳与实时通道持续异常，请打开运行日志查看 RPC 或本地服务的错误。";
  el.emptyHeartbeat.textContent = healthy
    ? `Finney 心跳 ${formatAge(chainAge, "刚刚")} · 区块 ${formatNumber(chain?.details?.best_height)}`
    : "等待链上心跳恢复…";
}

function ageSeconds(value) {
  if (!value) return null;
  const timestamp = typeof value === "number" ? value : new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
}

function formatAge(seconds, fallback, recent = "刚刚更新") {
  if (seconds == null || !Number.isFinite(seconds)) return fallback;
  if (seconds < 3) return recent;
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN") : "—";
}

function chainStateText(value) {
  return value === "connected" ? "心跳已超时" : value === "degraded" ? "连接异常" : "等待连接";
}

function populateTypes() {
  const current = el.typeFilter.value;
  const types = state.eventTypes.slice().sort((a, b) => a.localeCompare(b, "zh-CN"));
  el.typeFilter.replaceChildren(option("", "全部类型"), ...types.map((type) => option(type, type)));
  if (types.includes(current)) el.typeFilter.value = current;
}

function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
}

function totalPages() {
  return Math.max(1, Math.ceil(state.eventsTotal / state.pageSize));
}

function render() {
  if (["profiles", "priorities", "addresses", "records", "logs", "features", "guide", "changelog", "settings"].includes(state.route.kind)) {
    configurePageCopy();
    return;
  }
  if (state.panelCleared) {
    el.count.textContent = "0";
    el.pendingCount.textContent = "0";
    el.pendingUpdates.textContent = state.pending.length ? `+${state.pending.length}` : "";
    el.empty.hidden = false;
    el.eventList.replaceChildren();
    el.pagination.hidden = true;
    renderClearedPanel();
    configurePageCopy();
    return;
  }
  el.count.textContent = state.eventsTotal.toLocaleString("zh-CN");
  el.pendingCount.textContent = state.events.filter((event) => event.enrichment_status !== "complete").length.toLocaleString("zh-CN");
  el.pendingUpdates.textContent = state.pending.length ? `+${state.pending.length}` : "";
  el.empty.hidden = state.eventsTotal > 0;
  const offset = (state.currentPage - 1) * state.pageSize;
  el.eventList.replaceChildren(...state.events.map((event, index) => renderEvent(event, offset + index)));
  renderPagination();
  configurePageCopy();
  renderEmptyState();
}

function renderClearedPanel() {
  el.emptyTitle.textContent = "首页面板显示已清空";
  el.emptyCopy.textContent = "本地数据库记录没有被删除。输入子网编号查询，或调整筛选条件，即可重新显示事件。";
  el.emptyHeartbeat.textContent = "仅清除当前显示 · 原始记录仍保留";
}

function renderPagination() {
  const pages = totalPages();
  const page = Math.min(state.currentPage, pages);
  el.pagination.hidden = state.eventsTotal === 0;
  const start = state.eventsTotal ? (page - 1) * state.pageSize + 1 : 0;
  const end = Math.min(page * state.pageSize, state.eventsTotal);
  el.pageInput.value = String(page);
  el.pageInput.max = String(pages);
  el.pageTotal.textContent = String(pages);
  el.pageRange.textContent = `${formatNumber(start)}–${formatNumber(end)} / ${formatNumber(state.eventsTotal)}`;
  el.pageFirst.disabled = page <= 1;
  el.pagePrev.disabled = page <= 1;
  el.pageNext.disabled = page >= pages;
  el.pageLast.disabled = page >= pages;
}

const LEGACY_PROJECT_OWNER_SUFFIX = /（(SN\d+\s*项目方(?:、SN\d+\s*项目方)*)）/g;
const NOTE_SUFFIX = /((?:【[^】]+】)+)$/;
const TRAILING_NOTE_TAGS = /\s*(?:(?:【[^】]+】)|(?:（SN\d+\s*项目方(?:、SN\d+\s*项目方)*）))+\s*$/;

function compactTag(value) {
  return String(value || "").replace(/\s+/g, "").toLocaleLowerCase("zh-CN");
}

function stripAddressEventTags(summary) {
  return String(summary || "").replace(TRAILING_NOTE_TAGS, "").replace(/\s*·\s*$/, "").trim();
}

function eventPrimaryAddress(event) {
  const direct = String(event.details?.primary_address || "").trim();
  if (direct) return direct;
  const matched = Array.isArray(event.details?.matched_addresses)
    ? event.details.matched_addresses
    : [];
  return String(matched.find((item) => item?.address)?.address || "").trim();
}

function isSystemAutoAddressLabel(label, address) {
  const clean = String(label || "").trim().replace(/^[【\[]+|[】\]]+$/g, "").trim();
  const value = String(address || "").trim();
  if (!clean || clean === "未命名地址" || clean === value || clean === maskAddress(value)) return true;
  return clean.length <= 4 && value.endsWith(clean);
}

function addressEventNoteDetails(event) {
  const matched = Array.isArray(event.details?.matched_addresses)
    ? event.details.matched_addresses
    : [];
  const primaryAddress = eventPrimaryAddress(event);
  const item = matched.find((entry) => String(entry?.address || "") === primaryAddress) || matched[0] || {};
  const libraryName = String(item.library_book || "").trim().replace(/^[【\[]+|[】\]]+$/g, "").trim();
  const label = String(item.label || "").trim().replace(/^[【\[]+|[】\]]+$/g, "").trim();
  const primaryTag = libraryName
    ? `【${libraryName}】`
    : (!isSystemAutoAddressLabel(label, primaryAddress) ? `【${label}】` : "");
  const officialOwner = item.address_role === "project_owner" || item.official_owner === true;
  const officialTag = officialOwner ? "【官方】" : "";
  const manualTags = Array.isArray(item.manual_note_tags)
    ? item.manual_note_tags.filter(Boolean).map((tag) => {
      const clean = String(tag).trim().replace(/^[【\[]+|[】\]]+$/g, "").trim();
      return clean ? `【${clean}】` : "";
    }).filter(Boolean)
    : manualNoteTags(item.manual_note);
  const seen = new Set();
  const tags = [];
  for (const tag of [...manualTags, primaryTag, officialTag]) {
    const key = compactTag(tag);
    if (key && !seen.has(key)) {
      seen.add(key);
      tags.push(tag);
    }
  }
  return {
    base: stripAddressEventTags(event.summary),
    primaryTag,
    officialTag,
    officialOwner,
    manualTags,
    tags,
    address: primaryAddress,
    addressId: Number(item.address_id || 0) || null,
  };
}

function eventSummaryText(event) {
  if (event.source === "address") {
    const note = addressEventNoteDetails(event);
    const tags = note.tags.length ? note.tags : ["【手动备注】"];
    return [note.base, " · ", tags.join("")].filter(Boolean).join("");
  }
  let summary = String(event.summary || "").replace(
    LEGACY_PROJECT_OWNER_SUFFIX,
    (_, group) => group.split("、").map((tag) => `【${compactTag(tag)}】`).join(""),
  );
  const tags = Array.isArray(event.details?.project_owner_tags)
    ? event.details.project_owner_tags.filter(Boolean)
    : [];
  const compactSummary = compactTag(summary);
  const missing = tags.filter((tag) => !compactSummary.includes(compactTag(`【${tag}】`)));
  if (missing.length) summary += missing.map((tag) => `【${compactTag(tag)}】`).join("");
  return summary;
}

function appendEditableManualTag(element, event, tag) {
  const button = infoElement("button", "event-note-tag event-note-tag-editable", tag);
  button.type = "button";
  button.title = "点击编辑该地址的手动备注";
  button.setAttribute("aria-label", `编辑手动备注 ${tag}`);
  button.addEventListener("click", (clickEvent) => {
    clickEvent.preventDefault();
    clickEvent.stopPropagation();
    openManualNoteEditor(event);
  });
  button.addEventListener("keydown", (keyEvent) => keyEvent.stopPropagation());
  element.append(button);
}

function renderEventSummary(element, event) {
  if (event.source === "address") {
    const note = addressEventNoteDetails(event);
    element.append(document.createTextNode(note.base));
    element.append(document.createTextNode(" · "));
    if (note.manualTags.length) {
      for (const tag of note.manualTags) appendEditableManualTag(element, event, tag);
    }
    if (note.primaryTag) {
      element.append(infoElement(
        "span",
        note.officialOwner ? "project-owner-suffix official-owner-tag" : "event-library-tag",
        note.primaryTag,
      ));
    }
    if (note.officialTag) {
      element.append(infoElement("span", "project-owner-suffix official-owner-tag", note.officialTag));
    }
    if (!note.manualTags.length) {
      const placeholder = infoElement("button", "event-note-placeholder", "【手动备注】");
      placeholder.type = "button";
      placeholder.title = "点击添加手动备注，例如 回购、官方、出货";
      placeholder.setAttribute("aria-label", "为该地址添加手动备注");
      placeholder.addEventListener("click", (clickEvent) => {
        clickEvent.preventDefault();
        clickEvent.stopPropagation();
        openManualNoteEditor(event);
      });
      placeholder.addEventListener("keydown", (keyEvent) => keyEvent.stopPropagation());
      element.append(placeholder);
    }
    return;
  }

  const text = eventSummaryText(event);
  const match = text.match(NOTE_SUFFIX);
  if (!match) {
    element.textContent = text;
    return;
  }
  const ownerTags = new Set(
    (Array.isArray(event.details?.project_owner_tags) ? event.details.project_owner_tags : [])
      .map(compactTag),
  );
  element.append(document.createTextNode(text.slice(0, -match[0].length)));
  for (const tagMatch of match[0].matchAll(/【([^】]+)】/g)) {
    const tag = `【${tagMatch[1]}】`;
    const className = ownerTags.has(compactTag(tagMatch[1]))
      ? "project-owner-suffix"
      : "event-note-tag";
    element.append(infoElement("span", className, tag));
  }
}

function eventAddressAction(event) {
  if (event.source !== "address") return "";
  const stored = String(event.details?.address_action || "");
  if (["buy", "sell", "transfer"].includes(stored)) return stored;
  if (String(event.event_name || "").endsWith("StakeAdded")) return "buy";
  if (String(event.event_name || "").endsWith("StakeRemoved")) return "sell";
  return "transfer";
}


function renderAddressCopyAction(event) {
  const wrap = infoElement("div", "event-address-actions");
  const address = eventPrimaryAddress(event);
  if (!address) return wrap;
  const button = infoElement("button", "event-address-copy", "复制");
  button.type = "button";
  button.title = `复制完整地址：${address}`;
  button.setAttribute("aria-label", `复制地址 ${maskAddress(address)}`);
  button.addEventListener("click", async (clickEvent) => {
    clickEvent.preventDefault();
    clickEvent.stopPropagation();
    try {
      await copyTextToClipboard(address);
      showToast(`已复制 ${maskAddress(address)}`);
    } catch (error) {
      showToast(error.message);
    }
  });
  button.addEventListener("keydown", (keyEvent) => keyEvent.stopPropagation());
  wrap.addEventListener("click", (clickEvent) => clickEvent.stopPropagation());
  wrap.append(button);
  return wrap;
}

function renderEvent(event, index = 0) {
  const row = document.createElement("article");
  const tone = ["red", "yellow", "green"][index % 3];
  const direction = eventAddressAction(event);
  const directionClass = direction ? ` address-${direction}` : "";
  const eventTimestamp = new Date(
    event.updated_at || event.last_enriched_at || event.first_seen_at || event.event_at
  ).getTime();
  const recent = Number.isFinite(eventTimestamp)
    && Date.now() - eventTimestamp >= 0
    && Date.now() - eventTimestamp <= 30000;
  const latest = index === 0 && recent && state.route.kind !== "subnet";
  const priorityLevel = Number(event.priority_level || 0);
  const priorityClass = priorityLevel === 2 ? " priority-critical" : priorityLevel === 1 ? " priority-watch" : "";
  row.className = `event-row tone-${tone}${directionClass}${priorityClass}${event.event_id === state.selectedId ? " selected" : ""}${latest ? " is-latest-event" : ""}`;
  if (latest) {
    const remaining = Math.max(0, 30000 - (Date.now() - eventTimestamp));
    setTimeout(() => row.classList.remove("is-latest-event"), remaining);
  }
  row.tabIndex = 0;
  row.dataset.id = event.event_id;
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", `查看事件：${eventSummaryText(event)}`);

  const main = document.createElement("div");
  main.className = "event-main";
  const icon = document.createElement("span");
  icon.className = `event-icon ${event.source}`;
  icon.textContent = event.source === "github"
    ? "GH"
    : event.source === "simulation"
      ? "SIM"
      : event.source === "address" ? "AD" : "CH";
  const copy = document.createElement("div");
  copy.className = "event-copy";
  const title = document.createElement("strong");
  renderEventSummary(title, event);
  const meta = document.createElement("span");
  const priorityLabel = event.priority_level === 2 ? " · 重点" : event.priority_level === 1 ? " · 优先" : "";
  const subnetLabel = event.event_name === "Balances.Transfer"
    ? "全局链上"
    : event.netuid == null ? "SN待定" : `SN${event.netuid}`;
  meta.textContent = `${subnetLabel}${priorityLabel} · ${event.event_type} · ${event.event_name}`;
  copy.append(title, meta);
  main.append(icon, copy);

  const time = document.createElement("div");
  time.className = "event-time";
  time.textContent = formatTime(event.event_at);
  const badge = document.createElement("span");
  badge.className = `badge ${event.chain_status || event.enrichment_status}`;
  badge.textContent = statusText(event);
  row.append(main, time, badge, renderAddressCopyAction(event));
  row.addEventListener("click", () => selectEvent(event.event_id, true));
  row.addEventListener("keydown", (keyboardEvent) => {
    if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
      keyboardEvent.preventDefault();
      selectEvent(event.event_id, true);
    }
  });
  return row;
}

function isSimulatedEvent(event) {
  return event.source === "simulation"
    || event.confidence === "simulated"
    || event.details?.simulated === true;
}

function statusText(event) {
  if (isSimulatedEvent(event)) return "模拟测试";
  if (event.chain_status === "retracted") return "已撤回";
  if (event.chain_status === "observed") return "链上观察";
  if (event.enrichment_status === "pending") return "补全中";
  if (event.chain_status === "finalized") return "已确认";
  return "已记录";
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
}

function configurePageCopy() {
  const infoRoutes = { profiles: "子网档案", priorities: "优先级监控", addresses: "地址库", records: "事件记录导出", logs: "运行日志", features: "功能状态", guide: "使用说明", changelog: "版本更新", settings: "系统设置" };
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const key = state.route.kind === "subnet" || state.route.kind === "event" ? "home" : state.route.kind;
    link.classList.toggle("active", link.dataset.nav === key);
  });
  const isInfo = Boolean(infoRoutes[state.route.kind]);
  el.infoMenuSummary.classList.toggle("active", isInfo);
  el.metrics.hidden = isInfo;
  el.monitorHealth.hidden = isInfo;
  el.workspace.hidden = isInfo;
  el.infoView.hidden = !isInfo;
  el.simulationButton.hidden = state.route.kind !== "home";
  el.clearPanelButton.hidden = state.route.kind !== "home";
  el.floatingButton.hidden = state.route.kind !== "home";
  el.subnetFilter.closest(".subnet-query").hidden = state.route.kind === "subnet";
  if (state.route.kind === "subnet") {
    el.pageTitle.textContent = `SN${state.route.netuid} 的完整事件轨迹。`;
    el.pageSubtitle.textContent = "按发生时间正序排列，保留首次发现、补全和确认过程。";
    el.feedKicker.textContent = `SUBNET ${state.route.netuid}`;
    el.feedTitle.textContent = "子网历史";
  } else if (state.route.kind === "event") {
    el.pageTitle.textContent = "事件证据与处理轨迹。";
    el.pageSubtitle.textContent = "原始记录保持不变，补全、确认和更正进入同一条事件历史。";
    el.feedKicker.textContent = "EVENT STREAM";
    el.feedTitle.textContent = "最新事件";
  } else if (isInfo) {
    el.pageTitle.textContent = infoRoutes[state.route.kind];
    el.pageSubtitle.textContent = state.route.kind === "logs"
      ? "查看采集、推送、补全和外部接口的运行记录。"
      : state.route.kind === "profiles"
        ? "统一查看链上身份、官网、X、GitHub 和人工修订记录。"
        : state.route.kind === "priorities"
          ? "管理优先与重点监控子网，列表中优先展示并优先轮询。"
          : state.route.kind === "addresses"
            ? "维护重点地址，监控普通转账、质押、解质押和子网代币转移。"
          : state.route.kind === "records"
            ? "按子网查看并导出按事件时间正序排列的独立更新记录文档。"
          : state.route.kind === "features"
        ? "当前能力、交易开放状态与后续范围集中说明。"
        : state.route.kind === "guide"
          ? "快速了解面板、事件状态和交易开放信号。"
        : state.route.kind === "settings"
          ? "配置 GitHub API Token；留空时自动使用匿名访问。"
          : "按版本查看已经交付的功能与调整。";
    el.feedKicker.textContent = "SYSTEM INFO";
    el.feedTitle.textContent = infoRoutes[state.route.kind];
    renderInfoView();
  } else {
    el.pageTitle.textContent = "所有子网，一条可信事件流。";
    el.pageSubtitle.textContent = "链上事件先记录、先推送，状态与来源证据随后补全。";
    el.feedKicker.textContent = "EVENT STREAM";
    el.feedTitle.textContent = "最新事件";
  }
}

async function selectEvent(eventId, push = false) {
  state.selectedId = eventId;
  if (push && routePathname() !== `/events/${eventId}`) history.pushState({}, "", eventRoute(`/events/${eventId}`));
  state.route = { kind: "event", eventId };
  render();
  openDetail();
  try {
    renderDetail(await request(`/event-api/events/${eventId}`));
  } catch (error) {
    showToast(error.message);
  }
}

function renderDetail(data) {
  const event = data.event;
  el.detail.replaceChildren();
  const container = document.createElement("div");
  container.className = "detail-content";

  const topline = document.createElement("div");
  topline.className = "detail-topline";
  const netuid = document.createElement("a");
  netuid.className = "detail-netuid";
  netuid.href = eventRoute(event.netuid == null ? "/" : `/subnets/${event.netuid}`);
  netuid.textContent = event.event_name === "Balances.Transfer" ? "全局链上" : event.netuid == null ? "SN待定" : `SN${event.netuid} →`;
  const close = document.createElement("button");
  close.className = "detail-close";
  close.textContent = "×";
  close.setAttribute("aria-label", "关闭详情");
  close.addEventListener("click", returnToEventStream);
  topline.append(netuid, close);

  const time = document.createElement("time");
  time.className = "detail-event-time";
  time.dateTime = event.event_at;
  time.textContent = formatFullTime(event.event_at);
  const summary = document.createElement("h2");
  summary.className = "detail-summary";
  renderEventSummary(summary, event);
  const meta = document.createElement("div");
  meta.className = "detail-meta";
  meta.textContent = `${event.event_type} · ${event.event_name}`;
  const badges = document.createElement("div");
  badges.className = "badge-row";
  const sourceLabel = event.source === "chain"
    ? "链上来源"
    : event.source === "github"
      ? "GitHub"
      : event.source === "address" ? "地址监控" : "模拟测试";
  badges.append(makeBadge(sourceLabel), makeBadge(statusText(event), event.chain_status || event.enrichment_status));
  container.append(topline, time, summary, meta, badges);
  if (event.source === "github") appendGithubDetail(container, event);
  if (isSimulatedEvent(event)) {
    const danger = document.createElement("div");
    danger.className = "detail-danger-zone";
    const note = document.createElement("small");
    note.textContent = "该记录仅用于模拟测试，可从数据库永久删除。";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button danger";
    remove.textContent = "删除此模拟事件";
    remove.addEventListener("click", async () => {
      if (!window.confirm("确定永久删除这条模拟事件？此操作不会影响真实监控记录。")) return;
      remove.disabled = true;
      try {
        await request(`/event-api/events/${event.event_id}`, { method: "DELETE" });
        returnToEventStream();
        await loadEvents(1);
        showToast("模拟事件已从数据库永久删除");
      } catch (error) {
        remove.disabled = false;
        showToast(error.message);
      }
    });
    danger.append(note, remove);
    container.append(danger);
  }
  el.detail.append(container);
}

function appendGithubDetail(container, event) {
  const details = event.details || {};
  const raw = event.raw_data || {};
  const rawDetail = raw.detail || {};
  const files = Array.isArray(rawDetail.files) ? rawDetail.files : [];
  const section = document.createElement("section");
  section.className = "detail-section github-detail";
  section.append(infoElement("h3", "", event.event_name === "GitHub.ReleasePublished" ? "GitHub 发布详情" : "GitHub 提交详情"));

  const progressPoints = Array.isArray(details.progress_points) ? details.progress_points : [];
  if (progressPoints.length) {
    section.append(infoElement("h3", "github-subheading", "本次实际进展"));
    const progressList = document.createElement("ul");
    progressList.className = "github-progress-list";
    progressPoints.forEach((point) => progressList.append(infoElement("li", "", point)));
    section.append(progressList);
  }

  const grid = document.createElement("dl");
  grid.className = "data-grid";
  const rows = [
    ["仓库", details.repository || "—"],
    ["分支", details.branch || "—"],
    ["提交 SHA", details.sha || "—"],
    ["更新类型", details.change_kind || event.event_type || "—"],
    ["更新评级", details.update_rating || "—"],
    ["文件数量", details.files_changed ?? files.length],
    ["代码行数", `+${details.additions ?? 0} / -${details.deletions ?? 0}`],
  ];
  if (event.event_name === "GitHub.ReleasePublished") {
    rows.splice(2, 2,
      ["版本标签", details.tag || "—"],
      ["发布名称", details.name || "—"],
      ["是否预发布", details.prerelease ? "是" : "否"],
    );
  }
  for (const [label, value] of rows) {
    grid.append(infoElement("dt", "", label), infoElement("dd", "", String(value)));
  }
  section.append(grid);

  const messageTitle = infoElement("h3", "github-subheading", event.event_name === "GitHub.ReleasePublished" ? "完整发布说明" : "完整提交说明");
  const message = String(details.message || raw.body || raw.list_item?.commit?.message || "GitHub 未返回文字说明");
  const messagePre = document.createElement("pre");
  messagePre.className = "github-message";
  messagePre.textContent = message;
  section.append(messageTitle, messagePre);

  const isDocument = (name) => /\.(md|mdx|rst|txt|adoc)$/i.test(name) || /(^|\/)docs?\//i.test(name);
  const docFiles = files.filter((file) => isDocument(String(file.filename || "")));
  if (docFiles.length) {
    const docTitle = infoElement("h3", "github-subheading", `文档更新内容 · ${docFiles.length} 个文件`);
    const docList = infoElement("div", "github-file-list github-doc-diff");
    docFiles.forEach((file) => docList.append(renderGithubFile(file)));
    section.append(docTitle, docList);
  }

  if (files.length) {
    const fileTitle = infoElement("h3", "github-subheading", `全部变更文件 · ${files.length} 个`);
    const fileList = infoElement("div", "github-file-list");
    files.forEach((file) => fileList.append(renderGithubFile(file)));
    section.append(fileTitle, fileList);
  } else {
    const names = Array.isArray(details.files_preview) ? details.files_preview : [];
    const empty = infoElement("p", "github-empty", names.length
      ? `GitHub 未返回逐文件差异。已记录文件：${names.join("、")}`
      : "GitHub API 未返回文件级差异，可能因为文件过大、二进制或 Token 权限不足。");
    section.append(empty);
  }

  if (event.source_url) {
    const link = infoElement("a", "button ghost github-source-link", "在 GitHub 查看完整提交");
    link.href = event.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    section.append(link);
  }
  container.append(section);
}

function renderGithubFile(file) {
  const item = document.createElement("details");
  item.className = "github-file-diff";
  const summary = document.createElement("summary");
  const name = infoElement("code", "", String(file.filename || "未知文件"));
  const status = infoElement("span", `github-file-status ${String(file.status || "modified").replace(/[^a-z]/gi, "")}`, String(file.status || "modified"));
  const stats = infoElement("span", "github-file-stats", `+${Number(file.additions || 0)} / -${Number(file.deletions || 0)}`);
  summary.append(name, status, stats);
  const patch = document.createElement("pre");
  patch.textContent = String(file.patch || "GitHub 未返回该文件的文本差异（可能为二进制、超大文件或权限不足）。");
  item.append(summary, patch);
  return item;
}

function makeBadge(text, kind = "") {
  const node = document.createElement("span");
  node.className = `badge ${kind}`;
  node.textContent = text;
  return node;
}

function formatFullTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
    minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
}

function upsertEvent(event) {
  const index = state.events.findIndex((item) => item.event_id === event.event_id);
  if (index === -1) state.events.unshift(event);
  else state.events[index] = event;
  if (state.route.kind === "subnet") {
    state.events = state.events
      .filter((item) => item.netuid === state.route.netuid)
      .sort((a, b) => new Date(a.event_at) - new Date(b.event_at));
  } else {
    state.events.sort((a, b) => new Date(b.event_at) - new Date(a.event_at));
  }
}

function connectStream() {
  if (state.stream) state.stream.close();
  const stream = new EventSource("/event-api/stream");
  state.stream = stream;
  state.streamState = "connecting";
  renderStreamHealth();
  stream.addEventListener("ready", () => {
    state.streamState = "connected";
    state.streamConnectedAt = Date.now();
    el.liveStatus.className = "live-status online";
    el.liveStatus.innerHTML = "<span></span>实时通道已连接";
    renderStreamHealth();
    renderHeartbeatCountdown();
  });
  stream.addEventListener("event", (message) => {
    const payload = JSON.parse(message.data);
    if (payload.change_type === "deleted") {
      loadEvents(state.currentPage).catch((error) => showToast(error.message));
    } else if (state.paused) {
      state.pending.push(payload);
      if (!["profiles", "priorities", "addresses", "records", "logs", "features", "guide", "changelog", "settings"].includes(state.route.kind)) render();
    } else {
      if (state.panelCleared) {
        setPanelCleared(false, { render: false });
        showToast("收到新事件，事件流已自动恢复");
      }
      if (!["profiles", "priorities", "addresses", "records", "logs", "features", "guide", "changelog", "settings"].includes(state.route.kind)) {
        loadEvents(state.currentPage).catch((error) => showToast(error.message));
      }
      if (state.selectedId === payload.event.event_id) selectEvent(payload.event.event_id);
    }
    renderHeartbeatCountdown();
    loadStatus();
  });
  stream.onerror = () => {
    state.streamState = "reconnecting";
    el.liveStatus.className = "live-status offline";
    el.liveStatus.innerHTML = "<span></span>正在重连";
    renderStreamHealth();
    renderHeartbeatCountdown();
  };
}

function flushPending() {
  const hadPending = state.pending.length > 0;
  state.pending = [];
  if (hadPending && state.panelCleared) setPanelCleared(false, { render: false });
  loadEvents(1).catch((error) => showToast(error.message));
}

function isInternalAppPath(path) {
  const route = routePathname(path);
  return route === "/"
    || ["/profiles", "/priorities", "/addresses", "/records", "/logs", "/features", "/guide", "/changelog", "/settings"].includes(route)
    || /^\/subnets\/\d+$/.test(route)
    || /^\/events\/[0-9a-f]{64}$/.test(route);
}

function navigateWithinApp(link, url) {
  withEmbedQuery(url);
  const current = location.pathname + location.search + location.hash;
  const next = url.pathname + url.search + url.hash;
  if (current !== next) history.pushState({}, "", next);
  routeChanged();
  const details = link.closest("details");
  if (details) details.open = false;
}

document.addEventListener("click", (event) => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest("a[href]");
  if (!link || link.target || link.hasAttribute("download")) return;
  const url = new URL(link.href, location.href);
  if (url.origin !== location.origin || !isInternalAppPath(url.pathname)) return;
  event.preventDefault();
  navigateWithinApp(link, url);
});

function routeChanged() {
  state.route = parseRoute();
  state.selectedId = state.route.kind === "event" ? state.route.eventId : null;
  if (state.route.kind !== "event") closeDetail();
  Promise.all([loadEvents(1), loadProduct()]).then(() => {
    if (state.route.kind === "event") selectEvent(state.route.eventId);
    else if (!["profiles", "priorities", "addresses", "records", "logs", "features", "guide", "changelog", "settings"].includes(state.route.kind)) {
      el.detail.innerHTML = `<div class="detail-placeholder"><span class="detail-glyph">⌁</span><p class="eyebrow">EVIDENCE DRAWER</p><h2>选择一条事件</h2><p>查看原始数据、解析结果、确认状态和完整补充记录。</p></div>`;
    }
  }).catch((error) => showToast(error.message));
}

function openDetail() {
  el.detail.classList.add("open");
  el.detail.setAttribute("aria-hidden", "false");
  el.detailBackdrop.hidden = false;
}

function closeDetail() {
  el.detail.classList.remove("open");
  el.detail.setAttribute("aria-hidden", "true");
  el.detailBackdrop.hidden = true;
}

function returnToEventStream() {
  closeDetail();
  state.selectedId = null;
  state.route = { kind: "home" };
  history.pushState({}, "", eventRoute("/"));
  render();
}

function showToast(message) {
  el.toast.textContent = message;
  el.toast.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => el.toast.classList.remove("show"), 3200);
}

function infoElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function renderInfoView() {
  clearInterval(state.logTimer);
  state.logTimer = null;
  if (!state.product) {
    el.infoView.replaceChildren(infoElement("p", "info-intro", "正在读取产品信息…"));
    return;
  }
  el.infoView.replaceChildren();
  if (state.route.kind === "profiles") renderProfilesView();
  if (state.route.kind === "priorities") renderPrioritiesView();
  if (state.route.kind === "addresses") renderAddressesView();
  if (state.route.kind === "records") renderRecordsView();
  if (state.route.kind === "logs") renderLogsView();
  if (state.route.kind === "features") renderFeaturesView();
  if (state.route.kind === "guide") renderGuideView();
  if (state.route.kind === "changelog") renderChangelogView();
  if (state.route.kind === "settings") renderSettingsView();
}

async function renderSettingsView() {
  const intro = infoElement(
    "p", "info-intro",
    "GitHub Token 只保存在本机 .env，不会显示在页面或浏览器源码中。保存后立即生效；清空后系统自动切回匿名模式。",
  );
  const card = infoElement("section", "settings-card");
  const status = infoElement("div", "settings-status", "正在读取 GitHub API 状态…");
  const link = infoElement("a", "button ghost", "获取只读 GitHub Token");
  link.href = "https://github.com/settings/personal-access-tokens/new";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  const form = infoElement("form", "settings-form");
  const tokenLabel = infoElement("label", "settings-token-field");
  tokenLabel.append(infoElement("span", "", "GitHub API Token"));
  const tokenInput = infoElement("input");
  tokenInput.type = "password";
  tokenInput.name = "github_token";
  tokenInput.autocomplete = "off";
  tokenInput.spellcheck = false;
  tokenInput.placeholder = "粘贴 Fine-grained Token";
  tokenLabel.append(tokenInput);
  const help = infoElement("small", "", "推荐权限：Contents 和 Metadata 只读。Token 不会返回到浏览器。");
  const actions = infoElement("div", "settings-actions");
  const save = infoElement("button", "button primary", "保存并启用");
  save.type = "submit";
  const clear = infoElement("button", "button danger", "清除并匿名");
  clear.type = "button";
  actions.append(save, clear, link);
  form.append(tokenLabel, help, actions);
  card.append(status, form);
  el.infoView.append(intro, card);

  const renderStatus = (data) => {
    status.className = `settings-status ${data.configured ? "ok" : "anonymous"}`;
    status.textContent = data.message;
    tokenInput.placeholder = data.configured
      ? "Token 已配置；输入新 Token 可替换"
      : "未配置，当前使用匿名访问";
    clear.disabled = !data.configured;
  };
  const load = async () => renderStatus(await request("/event-api/settings/github"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = tokenInput.value.trim();
    if (!token) {
      showToast("请输入 Token；如需匿名访问请使用“清除并匿名”");
      return;
    }
    save.disabled = true;
    try {
      const data = await request("/event-api/settings/github", {
        method: "PUT",
        body: JSON.stringify({ token }),
      });
      tokenInput.value = "";
      renderStatus(data);
      showToast("GitHub Token 已保存并立即生效");
    } catch (error) { showToast(error.message); }
    finally { save.disabled = false; }
  });
  clear.addEventListener("click", async () => {
    if (!window.confirm("清除 GitHub Token 并切换到匿名模式？")) return;
    clear.disabled = true;
    try {
      const data = await request("/event-api/settings/github", {
        method: "PUT",
        body: JSON.stringify({ token: "" }),
      });
      renderStatus(data);
      showToast("已清除 Token，当前使用匿名 GitHub API");
    } catch (error) { showToast(error.message); }
    finally { clear.disabled = false; }
  });
  try { await load(); } catch (error) { status.textContent = error.message; }
}

async function renderProfilesView() {
  const intro = infoElement("p", "info-intro", "系统默认每周校对一次链上子网编号、名称、官网、X、GitHub 和 Discord。新注册子网先建立空档案，信息未取得时不会继承旧子网数据；注销后旧档案立即归档。也可点击“立即校对”。");
  const toolbar = infoElement("div", "profile-toolbar");
  const search = infoElement("input");
  search.type = "search";
  search.placeholder = "搜索 SN、名称、官网、X 或仓库";
  const trading = infoElement("select");
  [["", "全部交易状态"], ["enabled", "已开启"], ["disabled", "未开启"]].forEach(([value, label]) => {
    const item = infoElement("option", "", label);
    item.value = value;
    trading.append(item);
  });
  const sync = infoElement("button", "button primary", "立即校对");
  sync.type = "button";
  sync.id = "profile-sync-button";
  const archiveToggle = infoElement("label", "archive-toggle check-line");
  const archiveCheckbox = infoElement("input");
  archiveCheckbox.type = "checkbox";
  archiveCheckbox.id = "archive-profile-toggle";
  archiveToggle.append(archiveCheckbox, infoElement("span", "", "显示归档"));
  const exportJson = infoElement("a", "button ghost", "导出 JSON");
  exportJson.href = "/event-api/subnet-profiles/export?format=json";
  const exportCsv = infoElement("a", "button ghost", "导出 CSV");
  exportCsv.href = "/event-api/subnet-profiles/export?format=csv";
  const importButton = infoElement("button", "button ghost", "导入");
  importButton.type = "button";
  const selectionActions = infoElement("div", "selection-actions");
  const selectAll = infoElement("button", "button ghost", "全选");
  selectAll.type = "button";
  const unselectAll = infoElement("button", "button ghost", "取消全选");
  unselectAll.type = "button";
  selectionActions.append(selectAll, unselectAll);
  const fileInput = infoElement("input");
  fileInput.type = "file";
  fileInput.accept = ".json,.csv";
  fileInput.hidden = true;
  toolbar.append(search, trading, selectionActions, archiveToggle, sync, exportJson, exportCsv, importButton, fileInput);
  const syncStatus = infoElement("div", "profile-sync-status", "正在读取自动校对状态…");
  const table = infoElement("div", "profile-table");
  table.append(infoElement("p", "info-intro", "正在读取子网档案…"));
  el.infoView.append(intro, toolbar, syncStatus, table);
  let archivedProfiles = [];
  let latestSyncState = null;

  const applyFilter = () => {
    const needle = search.value.trim().toLowerCase();
    const selected = trading.value;
    const matches = (profile) => {
      const haystack = [
        profile.netuid, profile.name, profile.website_url, profile.x_url,
        profile.github_url, profile.discord_url,
      ].join(" ").toLowerCase();
      if (needle && !haystack.includes(needle)) return false;
      if (selected === "enabled" && profile.trading_enabled !== true) return false;
      if (selected === "disabled" && profile.trading_enabled !== false) return false;
      return true;
    };
    const rows = state.profiles.filter(matches).map(renderProfileRow);
    if (archiveCheckbox.checked) {
      rows.push(...archivedProfiles.filter(matches).map(renderArchivedProfileRow));
    }
    table.replaceChildren(...rows);
    if (!rows.length) table.append(infoElement("p", "info-intro", "没有匹配的子网档案。"));
  };
  const renderSyncState = (statusData) => {
    const syncState = statusData.workers?.profile_sync;
    latestSyncState = syncState;
    const details = syncState?.details || {};
    const intervalDays = Math.round(Number(statusData.runtime?.profile_sync_seconds || 604800) / 86400);
    if (syncState?.state === "running") {
      syncStatus.textContent = "正在校对链上子网编号、名称和关联链接…";
      syncStatus.className = "profile-sync-status running";
    } else if (syncState?.state === "degraded") {
      syncStatus.textContent = `校对失败：${details.error || details.message || "请查看运行日志"}`;
      syncStatus.className = "profile-sync-status error";
    } else if (syncState?.state === "connected") {
      syncStatus.textContent = `上次校对 ${formatFullTime(details.last_synced_at)} · 每 ${intervalDays} 天自动校对`;
      syncStatus.className = "profile-sync-status ok";
    } else {
      syncStatus.textContent = `自动校对周期：每 ${intervalDays} 天；服务启动后会先执行一次。`;
      syncStatus.className = "profile-sync-status";
    }
  };
  const load = async () => {
    const includeArchived = archiveCheckbox.checked ? "?include_archived=1" : "";
    const [data, statusData] = await Promise.all([
      request(`/event-api/subnet-profiles${includeArchived}`),
      request("/event-api/status"),
    ]);
    state.profiles = data.profiles;
    archivedProfiles = data.archived_profiles || [];
    renderSyncState(statusData);
    applyFilter();
  };
  const setMonitoring = async (netuids, monitored) => {
    const result = await request("/event-api/subnet-profiles/selection", {
      method: "POST",
      body: JSON.stringify({ netuids, monitored }),
    });
    showToast(`${monitored ? "已勾选" : "已取消"} ${result.updated} 个子网`);
    await load();
  };
  search.addEventListener("input", applyFilter);
  trading.addEventListener("change", applyFilter);
  archiveCheckbox.addEventListener("change", () => {
    load().catch((error) => showToast(error.message));
  });
  sync.addEventListener("click", async () => {
    sync.disabled = true;
    sync.textContent = "正在校对…";
    try {
      await request("/event-api/subnet-profiles/sync", { method: "POST", body: "{}" });
      showToast("已请求立即校对子网档案");
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const statusData = await request("/event-api/status");
        renderSyncState(statusData);
        if (statusData.workers?.profile_sync?.state !== "running") break;
      }
      await load();
    } catch (error) { showToast(error.message); }
    finally {
      sync.disabled = false;
      sync.textContent = "立即校对";
    }
  });
  importButton.addEventListener("click", () => fileInput.click());
  selectAll.addEventListener("click", () => setMonitoring(
    state.profiles.map((profile) => profile.netuid), true,
  ).catch((error) => showToast(error.message)));
  unselectAll.addEventListener("click", () => setMonitoring(
    state.profiles.map((profile) => profile.netuid), false,
  ).catch((error) => showToast(error.message)));
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = file.name.toLowerCase().endsWith(".csv") ? null : JSON.parse(text);
      const profiles = parsed ? (Array.isArray(parsed) ? parsed : parsed.profiles) : parseProfileCsv(text);
      const result = await request("/event-api/subnet-profiles/import", {
        method: "POST",
        body: JSON.stringify({ profiles, edited_by: "档案导入" }),
      });
      showToast(`已导入 ${result.imported} 条档案`);
      await load();
    } catch (error) { showToast(`导入失败：${error.message}`); }
  });
  try { await load(); } catch (error) { table.replaceChildren(infoElement("p", "info-intro", error.message)); }
}

function archiveReasonText(value) {
  return {
    network_removed: "子网注销事件",
    reconcile_registration_missing: "自动校对未发现当前注册",
    new_registration_replaced_active_profile: "同一编号重新注册，旧档案保留",
  }[value] || value || "子网档案已归档";
}

function renderArchivedProfileRow(profile) {
  const row = infoElement("article", "profile-row archived-profile-row");
  row.append(infoElement("span", "archive-mark", "档"));
  row.append(infoElement("div", "profile-id", `SN${profile.netuid}`));
  const name = infoElement("div", "profile-name");
  name.append(
    infoElement("strong", "", profile.name || "未登记名称"),
    infoElement("small", "", `归档于 ${formatFullTime(profile.archived_at)} · ${archiveReasonText(profile.archive_reason)}`),
  );
  const links = infoElement("div", "profile-links");
  [["website_url", "官网"], ["x_url", "X"], ["github_url", "GitHub"], ["discord_url", "Discord"]]
    .forEach(([field, label]) => {
      const href = profile[field];
      if (!href) return;
      if (/^https?:\/\//i.test(href)) {
        const link = infoElement("a", "", label);
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        links.append(link);
      } else {
        links.append(infoElement("span", "", `${label}: ${href}`));
      }
    });
  if (!links.childElementCount) links.append(infoElement("span", "", "无归档链接"));
  row.append(name, links, infoElement("span", "badge retracted", "已归档"));
  return row;
}

function renderProfileRow(profile) {
  const row = infoElement("article", "profile-row");
  const checkbox = infoElement("input", "profile-checkbox");
  checkbox.type = "checkbox";
  checkbox.checked = profile.monitored;
  checkbox.title = profile.monitored ? "已纳入监控轮询" : "未纳入监控轮询";
  checkbox.addEventListener("change", async () => {
    try {
      const result = await request("/event-api/subnet-profiles/selection", {
        method: "POST",
        body: JSON.stringify({ netuids: [profile.netuid], monitored: checkbox.checked }),
      });
      profile.monitored = checkbox.checked;
      showToast(`${checkbox.checked ? "已勾选" : "已取消"} SN${profile.netuid}，更新 ${result.updated} 条`);
    } catch (error) {
      checkbox.checked = !checkbox.checked;
      showToast(error.message);
    }
  });
  row.append(checkbox, infoElement("div", "profile-id", `SN${profile.netuid}`));
  const name = infoElement("div", "profile-name");
  const sourceText = profile.edited_at
    ? `人工修订 · ${profile.edited_by}`
    : profile.auto_updated_at
      ? `链上身份 · ${formatFullTime(profile.auto_updated_at)}`
      : "等待链上身份同步";
  name.append(
    infoElement("strong", "", profile.name || "链上未登记名称"),
    infoElement("small", "", sourceText),
  );
  const links = infoElement("div", "profile-links");
  const linkSpecs = [
    ["website_url", "官网"], ["x_url", "X"],
    ["github_url", "GitHub"], ["discord_url", "Discord"],
  ];
  linkSpecs.forEach(([field, label]) => {
    const href = profile[field];
    if (href) {
      if (/^https?:\/\//i.test(href)) {
        const link = infoElement("a", "", label);
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = `${label}: ${href}\n来源: ${profile.field_sources?.[field] || "manual"}`;
        links.append(link);
      } else {
        links.append(infoElement("span", "", `${label}: ${href}`));
      }
    }
  });
  if (!links.childElementCount) links.append(infoElement("span", "", "未登记外部链接"));
  const status = infoElement(
    "span", `badge ${profile.trading_enabled ? "finalized" : "observed"}`,
    profile.trading_enabled ? "交易已开放" : "交易未开放",
  );
  const actions = infoElement("div", "profile-actions");
  const edit = infoElement("button", "button ghost", "查看/编辑");
  edit.type = "button";
  edit.addEventListener("click", () => openProfileEditor(profile));
  actions.append(edit);
  row.append(name, links, status, actions);
  return row;
}

function openProfileEditor(profile) {
  el.profileError.textContent = "";
  el.profileDialogTitle.textContent = `SN${profile.netuid} · ${profile.name || "子网档案"}`;
  const sources = Object.entries(profile.field_sources || {})
    .filter(([, source]) => source)
    .map(([field, source]) => `${field}: ${source}`);
  el.profileSourceSummary.textContent = sources.length
    ? `来源记录：${sources.join(" · ")}`
    : "尚未取得可核验的链上档案字段。";
  for (const field of ["netuid", "name", "website_url", "x_url", "github_url", "discord_url", "logo_url", "description", "additional"]) {
    el.profileForm.elements[field].value = profile[field] ?? "";
  }
  el.profileForm.elements.edited_by.value = "";
  el.profileDialog.showModal();
}

function parseProfileCsv(text) {
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];
    if (char === '"' && quoted && next === '"') { field += '"'; index += 1; }
    else if (char === '"') quoted = !quoted;
    else if (char === "," && !quoted) { row.push(field); field = ""; }
    else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && next === "\n") index += 1;
      row.push(field); field = "";
      if (row.some((value) => value !== "")) rows.push(row);
      row = [];
    } else field += char;
  }
  row.push(field);
  if (row.some((value) => value !== "")) rows.push(row);
  if (!rows.length) return [];
  const headers = rows[0].map((value) => value.replace(/^\uFEFF/, ""));
  return rows.slice(1).map((values) => Object.fromEntries(
    headers.map((key, index) => [key, values[index] ?? ""])
  ));
}

async function renderPrioritiesView() {
  const intro = infoElement("p", "info-intro", "优先级自动开启监控。链上按全链区块统一采集，再按勾选状态过滤事件；优先级主要决定 GitHub 仓库在有限配额内的处理顺序。支持输入多个编号，例如 78,99。");
  const runtimeGrid = infoElement("div", "priority-runtime-grid");
  runtimeGrid.append(infoElement("p", "info-intro", "正在读取实际轮询参数…"));
  const controls = infoElement("div", "priority-controls");
  const input = infoElement("input");
  input.placeholder = "输入子网编号，例如 78,99";
  const level = infoElement("select");
  [["1", "优先监控"], ["2", "重点监控"]].forEach(([value, label]) => {
    const item = infoElement("option", "", label);
    item.value = value;
    level.append(item);
  });
  const add = infoElement("button", "button primary", "加入优先级");
  add.type = "button";
  controls.append(input, level, add);
  const list = infoElement("div", "profile-table");
  el.infoView.append(intro, runtimeGrid, controls, list);
  let loadVersion = 0;
  const load = async () => {
    const requestVersion = ++loadVersion;
    const [data, status] = await Promise.all([
      request("/event-api/subnet-priorities"),
      request("/event-api/status"),
    ]);
    if (requestVersion !== loadVersion) return;
    const runtime = status.runtime || {};
    const githubMinutes = Math.max(1, Math.round(Number(runtime.github_poll_seconds || 300) / 60));
    runtimeGrid.replaceChildren(
      priorityRuntimeCard(
        "链上事件",
        `${formatNumber(runtime.chain_poll_seconds || 2)} 秒`,
        "每轮读取最新区块；所有已勾选子网共享这个实时流。优先级不会让链上 RPC 单独加快。",
      ),
      priorityRuntimeCard(
        "GitHub 仓库",
        `${githubMinutes} 分钟 / 每轮 ${formatNumber(runtime.github_batch_size || 3)} 个`,
        runtime.github_token_configured
          ? "已配置 Token；每轮先处理重点、再处理优先、最后处理普通仓库。"
          : "当前为匿名 API；每轮先处理重点、再处理优先、最后处理普通仓库，配额不足时保留游标。",
      ),
      priorityRuntimeCard(
        "交易开放开关",
        `${formatNumber(runtime.trading_poll_seconds || 5)} 秒`,
        "独立比对 SubtokenEnabled；该轮询不受 GitHub 限流影响。",
      ),
      priorityRuntimeCard(
        "队列顺序",
        "重点 ＞ 优先 ＞ 普通",
        "重点监控排在优先监控之前；两者都会自动加入监控集合。普通监控保持注册号顺序。",
      ),
    );
    list.replaceChildren();
    if (!data.priorities.length) {
      list.append(infoElement("p", "info-intro", "尚未设置优先或重点监控子网。"));
      return;
    }
    data.priorities.forEach((profile) => {
      const row = infoElement("article", "profile-row priority-row");
      row.append(infoElement("div", "profile-id", `SN${profile.netuid}`));
      const name = infoElement("div", "profile-name");
      name.append(
        infoElement("strong", "", profile.name || "链上未登记名称"),
        infoElement("small", "", profile.priority_level === 2 ? "重点监控" : "优先监控"),
      );
      const links = infoElement("div", "profile-links");
      [profile.website_url, profile.x_url, profile.github_url]
        .filter((value) => /^https?:\/\//i.test(value || ""))
        .forEach((href) => {
          const link = infoElement("a", "", new URL(href).hostname.replace(/^www\./, ""));
          link.href = href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          links.append(link);
        });
      if (!links.childElementCount) links.append(infoElement("span", "", "无关联链接"));
      const selected = infoElement("span", "badge finalized", "已勾选监控");
      const actions = infoElement("div", "profile-actions");
      const remove = infoElement("button", "button ghost", "移出");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        try {
          await request("/event-api/subnet-priorities", {
            method: "POST",
            body: JSON.stringify({ netuids: [profile.netuid], level: 0 }),
          });
          await load();
        } catch (error) { showToast(error.message); }
      });
      actions.append(remove);
      row.append(name, links, selected, actions);
      list.append(row);
    });
  };
  add.addEventListener("click", async () => {
    try {
      const result = await request("/event-api/subnet-priorities", {
        method: "POST",
        body: JSON.stringify({ netuids: input.value, level: Number(level.value) }),
      });
      showToast(`已更新 ${result.updated} 个子网的优先级`);
      input.value = "";
      await load();
    } catch (error) { showToast(error.message); }
  });
  try { await load(); } catch (error) { list.replaceChildren(infoElement("p", "info-intro", error.message)); }
}

function priorityRuntimeCard(title, value, description) {
  const card = infoElement("article", "priority-runtime-card");
  card.append(
    infoElement("span", "", title),
    infoElement("strong", "", value),
    infoElement("small", "", description),
  );
  return card;
}

function populateSimulationAddresses() {
  const enabled = state.watchedAddresses.filter((item) => item.enabled !== false);
  const previous = el.simulationAddress.value;
  const addressOptions = enabled.length
    ? [
      option("", "请选择测试地址"),
      ...enabled.map((item) => {
        const masked = item.masked || `${String(item.address).slice(0, 4)}…${String(item.address).slice(-4)}`;
        const label = String(item.label || "").trim();
        const display = label && label !== masked ? `${label} · ${masked}` : masked;
        const node = option(String(item.address_id), display);
        node.dataset.label = label;
        node.dataset.masked = masked;
        return node;
      }),
    ]
    : [option("", "请先在地址库启用地址")];
  el.simulationAddress.replaceChildren(...addressOptions);
  if (enabled.some((item) => String(item.address_id) === previous)) {
    el.simulationAddress.value = previous;
  }
}

async function loadSimulationSignals() {
  try {
    const [data, addressData] = await Promise.all([
      request("/event-api/simulation/signals"),
      request("/event-api/addresses"),
    ]);
    state.simulationSignals = data.signals || [];
    state.watchedAddresses = addressData.addresses || [];
    populateSimulationAddresses();
    el.simulationButton.disabled = !data.enabled || state.simulationSignals.length === 0;
    if (!data.enabled) {
      el.simulationButton.textContent = "模拟信号已关闭";
      return;
    }
    el.simulationSignal.replaceChildren(
      option("", "请选择信号"),
      ...state.simulationSignals.map((signal) => option(
        signal.id,
        `${signal.label}｜${signal.description}`,
      )),
    );
  } catch (error) {
    el.simulationButton.disabled = true;
    el.simulationButton.title = error.message;
  }
}

function readInlineFloatingBounds() {
  try {
    const value = JSON.parse(localStorage.getItem(UI_KEYS.inlineBounds) || "null");
    return value && ["x", "y", "width", "height"].every((key) => Number.isFinite(Number(value[key])))
      ? value
      : null;
  } catch {
    return null;
  }
}

function restoreInlineFloatingBounds() {
  if (el.inlineFloating.hidden) return;
  const bounds = readInlineFloatingBounds();
  if (!bounds) return;
  const width = Math.max(330, Math.min(Number(bounds.width), window.innerWidth - 16));
  const height = Math.max(300, Math.min(Number(bounds.height), window.innerHeight - 16));
  el.inlineFloating.style.width = `${width}px`;
  el.inlineFloating.style.height = `${height}px`;
  el.inlineFloating.style.left = `${Math.max(8, Math.min(Number(bounds.x), window.innerWidth - width - 8))}px`;
  el.inlineFloating.style.top = `${Math.max(8, Math.min(Number(bounds.y), window.innerHeight - height - 8))}px`;
  el.inlineFloating.style.right = "auto";
  el.inlineFloating.style.bottom = "auto";
}

function saveInlineFloatingBounds() {
  if (el.inlineFloating.hidden) return;
  const rect = el.inlineFloating.getBoundingClientRect();
  try {
    localStorage.setItem(UI_KEYS.inlineBounds, JSON.stringify({
      x: Math.round(rect.left), y: Math.round(rect.top),
      width: Math.round(rect.width), height: Math.round(rect.height),
    }));
  } catch {
    // Position persistence is optional.
  }
}

function showInlineFloating() {
  el.inlineFloating.hidden = false;
  if (!el.inlineFloatingFrame.getAttribute("src")) {
    el.inlineFloatingFrame.src = eventRoute("/floating?mode=inline&embed=1");
  }
  requestAnimationFrame(restoreInlineFloatingBounds);
}

function openIndependentFloatingWindow(width, height) {
  const popup = window.open(
    eventRoute("/floating?mode=window"),
    "subnetPulseFloating",
    `popup=yes,width=${width},height=${height},resizable=yes,scrollbars=no`,
  );
  if (!popup) {
    showToast("浏览器阻止了浮窗，请允许本站弹出窗口");
    return false;
  }
  popup.focus();
  showToast("独立窗口已开启；窗口层级由操作系统管理");
  return true;
}

async function openFloatingMonitor(mode = "pip") {
  const width = Math.min(520, Math.max(380, Math.round(window.screen.availWidth * 0.32)));
  const height = Math.min(660, Math.max(420, Math.round(window.screen.availHeight * 0.58)));
  const selectedMode = ["inline", "window", "pip"].includes(mode) ? mode : "pip";
  try {
    localStorage.setItem(UI_KEYS.floatingMode, selectedMode);
  } catch {
    // The selected mode remains active for this open action.
  }

  if (selectedMode === "inline") {
    showInlineFloating();
    showToast("已嵌入当前页面；此模式不会跨网页悬浮");
    return;
  }

  if (selectedMode === "pip" && window.documentPictureInPicture?.requestWindow) {
    try {
      const floating = await window.documentPictureInPicture.requestWindow({ width, height });
      floating.document.title = "Wallet Link 事件监控 浮窗监听";
      floating.document.documentElement.style.cssText = "margin:0;background:#080b0f;";
      floating.document.body.style.cssText = "margin:0;overflow:hidden;background:#080b0f;";
      const frame = floating.document.createElement("iframe");
      frame.src = eventRoute("/floating?mode=pip");
      frame.title = "Wallet Link 事件监控 浮窗监听";
      frame.style.cssText = "display:block;width:100vw;height:100vh;border:0;";
      floating.document.body.append(frame);
      showToast("系统置顶浮窗已开启；可拖动并调整窗口大小");
      return;
    } catch (error) {
      showToast(`画中画浮窗不可用，改用独立窗口：${error.message}`);
    }
  } else if (selectedMode === "pip") {
    showToast("当前浏览器不支持画中画，已改用独立窗口");
  }
  openIndependentFloatingWindow(width, height);
}

function openFloatingSettings() {
  const mode = readStoredFloatingMode();
  for (const radio of el.floatingForm.elements.floating_mode) {
    radio.checked = radio.value === mode;
  }
  applyEventFontSize(state.eventFontSize, { persist: false, broadcast: false });
  el.floatingDialog.showModal();
}

function openSimulationDialog() {
  el.simulationForm.reset();
  el.simulationError.textContent = "";
  el.simulationDelay.hidden = true;
  el.simulationCompletion.disabled = false;
  populateSimulationAddresses();
  if (!el.simulationSignal.options.length) {
    el.simulationSignal.append(option("", "没有可用信号"));
  }
  updateSimulationPreview();
  el.simulationDialog.showModal();
  if (el.simulationSignal.value && selectedSimulationSignal()?.simulation_kind === "address") {
    el.simulationAddress.focus();
  } else {
    el.simulationForm.elements.netuid.focus();
  }
}

function selectedSimulationSignal() {
  const signalId = el.simulationSignal.value;
  return state.simulationSignals.find((signal) => signal.id === signalId) || null;
}

function updateSimulationPreview() {
  const signal = selectedSimulationSignal();
  const netuid = Number(el.simulationForm.elements.netuid.value);
  const hasNetuid = Number.isInteger(netuid) && netuid >= 0 && netuid <= 65535;
  const addressSignal = signal?.simulation_kind === "address";
  el.simulationAddressField.hidden = !addressSignal;
  el.simulationNetuidField.hidden = Boolean(addressSignal && !signal.netuid_required);
  el.simulationForm.elements.netuid.required = Boolean(!addressSignal || signal.netuid_required);
  el.simulationAddress.required = Boolean(addressSignal);
  if (!signal) {
    el.simulationSummary.textContent = "请选择信号";
    el.simulationMeaning.textContent = "下拉选项同时显示信号名称与含义。";
    el.simulationCompletion.checked = false;
    el.simulationCompletion.disabled = false;
    el.simulationDelay.hidden = true;
    return;
  }
  if (addressSignal) {
    const selected = el.simulationAddress.selectedOptions[0];
    const masked = selected?.dataset.masked || "";
    const label = String(selected?.dataset.label || "").trim();
    const walletIdentity = label && label !== masked ? `${label} · ${masked}` : masked;
    if (!selected?.value) {
      el.simulationSummary.textContent = "请先在地址库录入并启用地址";
    } else if (signal.event_name.endsWith(".Transfer")) {
      el.simulationSummary.textContent = `TAO 转账 · ${walletIdentity} → 5FHn…94ty · 1 TAO`;
    } else {
      const isAdded = signal.event_name.endsWith("StakeAdded");
      const verb = isAdded ? "质押" : "解质押";
      const subnet = hasNetuid ? `SN${netuid}` : "SN待定";
      el.simulationSummary.textContent = netuid === 0
        ? `${subnet} · ${walletIdentity} · ${verb} 1.00 TAO`
        : `${subnet} · ${walletIdentity} · ${isAdded ? "买入" : "卖出"} 1.00 TAO`;
    }
    el.simulationMeaning.textContent = `${signal.description} · 将生成 source=address、confidence=simulated 的地址监控测试记录。`;
    el.simulationCompletion.checked = false;
    el.simulationCompletion.disabled = true;
    el.simulationDelay.hidden = true;
    return;
  }
  el.simulationSummary.textContent = hasNetuid
    ? signal.summary.replaceAll("{netuid}", String(netuid))
    : signal.summary.replaceAll("SN{netuid}", "SN…").replaceAll("{netuid}", "…");
  const completionText = signal.supports_completion
    ? `可选补全结果：${signal.completed_summary.replaceAll("{netuid}", hasNetuid ? String(netuid) : "…")}`
    : "该信号没有独立补全步骤。";
  el.simulationMeaning.textContent = `${signal.description} · ${completionText}`;
  el.simulationCompletion.disabled = !signal.supports_completion;
  if (!signal.supports_completion) el.simulationCompletion.checked = false;
  el.simulationDelay.hidden = !el.simulationCompletion.checked;
}

async function renderRecordsView() {
  const intro = infoElement(
    "p",
    "info-intro",
    "每个子网生成独立的事件更新记录，按事件发生时间正序排列；时间相同时按首次采集时间排序。可在线查看，也可分别导出 Markdown、CSV、JSON 或 HTML。",
  );
  const toolbar = infoElement("div", "record-toolbar");
  const search = infoElement("input");
  search.type = "search";
  search.placeholder = "搜索子网编号或名称";
  const summary = infoElement("span", "record-count", "正在读取…");
  const batch = infoElement("div", "record-batch-actions");
  const data = await request("/event-api/subnet-event-records");
  if (state.route.kind !== "records") return;
  for (const [format, label] of [["md", "全部 Markdown ZIP"], ["csv", "全部 CSV ZIP"], ["json", "全部 JSON ZIP"], ["html", "全部 HTML ZIP"]]) {
    const link = infoElement("a", "button ghost", label);
    link.href = data.archive_exports[format];
    link.setAttribute("download", "");
    batch.append(link);
  }
  toolbar.append(search, summary, batch);
  const list = infoElement("div", "profile-table record-table");
  const applyFilter = () => {
    const needle = search.value.trim().toLocaleLowerCase("zh-CN");
    const records = data.records.filter((record) => (
      !needle
      || String(record.netuid).includes(needle.replace(/^sn/i, ""))
      || String(record.name || "").toLocaleLowerCase("zh-CN").includes(needle)
    ));
    summary.textContent = `${records.length} / ${data.subnet_count} 个子网`;
    list.replaceChildren(...records.map((record) => {
      const row = infoElement("article", "profile-row record-row");
      row.append(infoElement("div", "profile-id", `SN${record.netuid}`));
      const identity = infoElement("div", "profile-name");
      identity.append(
        infoElement("strong", "", record.name || "未登记名称"),
        infoElement("small", "", record.event_count
          ? `${record.event_count} 条事件 · ${formatFullTime(record.first_event_at)} → ${formatFullTime(record.last_event_at)}`
          : "暂无已采集事件"),
      );
      const actions = infoElement("div", "record-actions");
      const view = infoElement("a", "button ghost", "在线查看");
      view.href = record.view_url;
      actions.append(view);
      for (const [format, label] of [["md", "MD"], ["csv", "CSV"], ["json", "JSON"], ["html", "HTML"]]) {
        const link = infoElement("a", "button ghost", label);
        link.href = record.exports[format];
        link.setAttribute("download", "");
        actions.append(link);
      }
      row.append(identity, actions);
      return row;
    }));
    if (!records.length) list.append(infoElement("p", "info-intro", "没有匹配的子网记录。"));
  };
  search.addEventListener("input", applyFilter);
  el.infoView.replaceChildren(intro, toolbar, list);
  applyFilter();
}

function subnetBookNumber(name) {
  const match = String(name || "").trim().match(/^SN\s*(\d+)/i);
  return match ? Number(match[1]) : null;
}

function compareBookNames(left, right) {
  const leftNumber = subnetBookNumber(left);
  const rightNumber = subnetBookNumber(right);
  if (leftNumber != null && rightNumber != null && leftNumber !== rightNumber) {
    return leftNumber - rightNumber;
  }
  if (leftNumber != null && rightNumber == null) return -1;
  if (leftNumber == null && rightNumber != null) return 1;
  return String(left || "").localeCompare(String(right || ""), "zh-CN", { numeric: true });
}

function bookNameMatches(name, needle) {
  const text = String(name || "").toLocaleLowerCase("zh-CN");
  if (!needle) return false;
  if (text.includes(needle)) return true;
  const compact = String(needle).trim().replace(/^sn/i, "").replace(/\s+/g, "");
  const number = subnetBookNumber(name);
  return /^\d+$/.test(compact) && number != null && number === Number(compact);
}

function eventLibrarySyncMessage(result) {
  const parts = [];
  for (const [book, count] of Object.entries(result.added_books || {})) {
    parts.push(`新增「${book}」${count} 个地址`);
  }
  for (const [book, count] of Object.entries(result.removed_books || {})) {
    parts.push(`移除已解散库「${book}」${count} 个地址`);
  }
  for (const [change, count] of Object.entries(result.updated_books || {})) {
    parts.push(`更新「${change}」${count} 个地址`);
  }
  return parts.length
    ? `地址库同步：${parts.join("；")}`
    : `地址库已最新，检查了 ${Number(result.checked || 0)} 个监听地址`;
}

async function pollEventLibrarySync() {
  try {
    const result = await request("/api/event-monitor/watch/reconcile", { method: "POST" });
    if (Number(result.added || 0) || Number(result.removed || 0) || Number(result.updated || 0)) {
      showToast(eventLibrarySyncMessage(result));
    }
  } catch {}
}

async function renderAddressesView() {
  const intro = infoElement(
    "p",
    "info-intro",
    "事件监控以已知地址库为对象，而不是以单条地址为对象。监控名单默认为空；只监控一个地址时，请先在左侧“已知地址库”建立只含该地址的地址库，再回来选择。库级勾选代表整库，展开后可做二级勾选，只监控库内部分地址。",
  );
  const shared = infoElement("section", "shared-address-panel");
  const sharedCopy = infoElement("div", "shared-address-copy");
  sharedCopy.append(
    infoElement("strong", "", "按地址库选择事件监控地址"),
    infoElement("small", "", "共享的是地址资料，不是监控状态。搜索优先匹配地址库名，也可输入地址后 4 位反查所属库。"),
  );
  const sharedActions = infoElement("div", "monitor-library-actions");
  const syncButton = infoElement("button", "button ghost", "刷新同步");
  syncButton.type = "button";
  syncButton.title = "立即按当前已知地址库更新事件监控名单";
  const sharedButton = infoElement("button", "button primary", "选择地址库");
  sharedButton.type = "button";
  sharedActions.append(syncButton, sharedButton);
  shared.append(sharedCopy, sharedActions);
  const guide = infoElement(
    "p",
    "monitor-group-guide",
    "如需监控单个地址：先到左侧“已知地址库”新建一个只包含该地址的地址库，再在本页勾选整个库。",
  );
  const list = infoElement("div", "address-list");
  list.append(infoElement("p", "info-intro", "正在读取监控地址…"));
  el.infoView.append(intro, shared, guide, list);
  let loadVersion = 0;

  const load = async () => {
    const requestVersion = ++loadVersion;
    try {
      await request("/api/event-monitor/watch/reconcile", { method: "POST" });
    } catch {}
    const data = await request("/event-api/addresses");
    if (requestVersion !== loadVersion) return;
    state.watchedAddresses = data.addresses || [];
    populateSimulationAddresses();
    list.replaceChildren();
    if (!state.watchedAddresses.length) {
      list.append(infoElement("p", "address-empty", "事件监控名单为空。请从命名地址库勾选并确认；确认前不会监控任何地址。"));
      return;
    }
    const groups = new Map();
    for (const item of state.watchedAddresses) {
      const name = String(item.library_book || "").trim() || "历史/未归档监控";
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(item);
    }
    [...groups.entries()]
      .sort(([left], [right]) => compareBookNames(left, right))
      .forEach(([name, items]) => {
        const group = infoElement("details", "monitor-book-group");
        group.open = true;
        const summary = infoElement("summary", "monitor-book-summary");
        const enabled = items.filter((item) => item.enabled).length;
        summary.append(
          infoElement("strong", "", name),
          infoElement("span", "", `${enabled} / ${items.length} 个地址启用监控`),
        );
        const body = infoElement("div", "monitor-book-addresses");
        items.forEach((item) => body.append(renderAddressRow(item, load)));
        group.append(summary, body);
        list.append(group);
      });
  };

  syncButton.addEventListener("click", async () => {
    syncButton.disabled = true;
    syncButton.textContent = "同步中";
    try {
      const result = await request("/api/event-monitor/watch/reconcile", { method: "POST" });
      showToast(eventLibrarySyncMessage(result));
      await load();
    } catch (error) {
      showToast(error.message);
    } finally {
      if (syncButton.isConnected) {
        syncButton.disabled = false;
        syncButton.textContent = "刷新同步";
      }
    }
  });
  sharedButton.addEventListener("click", () => openSharedAddressPicker(load));
  try {
    await load();
  } catch (error) {
    list.replaceChildren(infoElement("p", "address-empty", error.message));
  }
}

async function openSharedAddressPicker(reload) {
  const dialog = infoElement("dialog", "shared-library-dialog");
  const head = infoElement("div", "dialog-head");
  const title = infoElement("div");
  title.append(
    infoElement("p", "eyebrow", "SHARED ADDRESS BOOKS"),
    infoElement("h2", "", "按地址库勾选事件监控"),
  );
  const close = infoElement("button", "icon-button", "×");
  close.type = "button";
  close.setAttribute("aria-label", "关闭");
  head.append(title, close);
  const note = infoElement(
    "p",
    "dialog-copy",
    "一级勾选代表整个地址库；展开后可用二级勾选只监控库内部分地址。已知地址库与事件监控共用地址资料，但监控名单独立；移出监控不会删除已知地址库。",
  );
  const toolbar = infoElement("div", "shared-library-toolbar");
  const search = infoElement("input");
  search.type = "search";
  search.placeholder = "输入 SN 编号、地址库名，或地址后 4 位";
  search.setAttribute("aria-label", "按 SN 编号、地址库名或地址后四位搜索");
  const selectVisible = infoElement("button", "button ghost", "全选当前地址库");
  selectVisible.type = "button";
  const clear = infoElement("button", "button ghost", "清空选择");
  clear.type = "button";
  const summary = infoElement("span", "shared-library-summary", "正在读取已知地址库…");
  toolbar.append(search, selectVisible, clear, summary);
  const list = infoElement("div", "shared-book-list");
  const actions = infoElement("div", "dialog-actions");
  const error = infoElement("span", "shared-library-error");
  error.setAttribute("role", "alert");
  const confirm = infoElement("button", "button primary", "确认加入监控");
  confirm.type = "button";
  confirm.disabled = true;
  actions.append(error, confirm);
  dialog.append(head, note, toolbar, list, actions);
  document.body.append(dialog);

  let items = [];
  let unbookedCount = 0;
  let groups = [];
  const watched = new Map(state.watchedAddresses.map((item) => [String(item.address), item]));
  const selected = new Set();
  const expanded = new Set();

  const updateConfirm = () => {
    confirm.disabled = selected.size === 0;
    confirm.textContent = selected.size ? `确认加入监控 (${selected.size} 个地址)` : "确认加入监控";
  };
  const needleValue = () => search.value.trim().toLocaleLowerCase("zh-CN");
  const addressMatches = (item, needle) => {
    if (!needle) return false;
    const address = String(item.address || "").toLocaleLowerCase("zh-CN");
    const suffix = address.slice(-4);
    return suffix.includes(needle) || address.includes(needle);
  };
  const visibleGroups = () => {
    const needle = needleValue();
    if (!needle) return groups;
    return groups
      .map((group) => {
        const nameMatch = bookNameMatches(group.name, needle);
        const matchedItems = group.items.filter((item) => addressMatches(item, needle));
        return { ...group, nameMatch, matchedItems, matched: nameMatch || matchedItems.length > 0 };
      })
      .filter((group) => group.matched)
      .sort((left, right) => {
        if (left.nameMatch !== right.nameMatch) return left.nameMatch ? -1 : 1;
        return compareBookNames(left.name, right.name);
      });
  };
  const groupState = (group) => {
    const selectable = group.items.filter((item) => !watched.has(String(item.address)));
    const selectedCount = selectable.filter((item) => selected.has(item.address)).length;
    const watchedCount = group.items.length - selectable.length;
    return { selectable, selectedCount, watchedCount };
  };
  const setGroupChecked = (group, checked) => {
    const { selectable } = groupState(group);
    for (const item of selectable) {
      if (checked) selected.add(item.address);
      else selected.delete(item.address);
    }
  };

  const render = () => {
    list.replaceChildren();
    const calls = visibleGroups();
    const totalBooks = groups.length;
    const totalAddresses = items.length;
    summary.textContent = `${calls.length} / ${totalBooks} 个地址库 · ${totalAddresses} 个地址 · 已选 ${selected.size}`;
    if (unbookedCount > 0) {
      list.append(infoElement(
        "p",
        "shared-address-more",
        `另有 ${unbookedCount} 个地址尚未归入命名地址库，本页不直接展示。需要监控时，请先在主工具“已知地址库”中为它建立地址库。`,
      ));
    }
    if (!calls.length) {
      list.append(infoElement("p", "address-empty", groups.length
        ? "没有匹配的地址库。可输入 SN 编号、地址库名，或输入地址后 4 位反查。"
        : "还没有命名地址库。请先在左侧“已知地址库”建立地址库，再回来选择。"));
      updateConfirm();
      return;
    }

    const needle = needleValue();
    for (const group of calls) {
      const card = infoElement("section", "shared-book-card");
      const row = infoElement("div", "shared-book-row");
      const state = groupState(group);
      const checkbox = infoElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "shared-book-checkbox";
      checkbox.disabled = state.selectable.length === 0;
      checkbox.checked = state.selectable.length === 0 || state.selectedCount === state.selectable.length;
      checkbox.indeterminate = state.selectedCount > 0 && state.selectedCount < state.selectable.length;
      checkbox.setAttribute("aria-label", `整库选择 ${group.name}`);
      checkbox.addEventListener("change", () => {
        setGroupChecked(group, checkbox.checked);
        render();
      });
      const identity = infoElement("div", "shared-book-identity");
      identity.append(
        infoElement("strong", "", group.name),
        infoElement(
          "small",
          "",
          `${group.items.length} 个地址 · 已监控 ${state.watchedCount} · 已选 ${state.selectedCount}${group.nameMatch === false ? " · 地址后四位命中" : ""}`,
        ),
      );
      const toggle = infoElement("button", "button ghost shared-book-toggle");
      toggle.type = "button";
      const shouldExpand = expanded.has(group.name) || Boolean(needle && (group.nameMatch || group.matchedItems?.length));
      toggle.textContent = shouldExpand ? "收起地址" : "展开二级勾选";
      toggle.addEventListener("click", () => {
        if (expanded.has(group.name)) expanded.delete(group.name);
        else expanded.add(group.name);
        render();
      });
      row.append(checkbox, identity, toggle);
      card.append(row);

      if (shouldExpand) {
        const children = group.nameMatch || !needle
          ? group.items
          : (group.matchedItems || []);
        const shownChildren = children.slice(0, 200);
        const childList = infoElement("div", "shared-book-children");
        for (const item of shownChildren) {
          const already = watched.has(String(item.address));
          const child = infoElement("label", `shared-address-row${already ? " monitored" : ""}`);
          const childCheckbox = infoElement("input");
          childCheckbox.type = "checkbox";
          childCheckbox.value = item.address;
          childCheckbox.disabled = already;
          childCheckbox.checked = already || selected.has(item.address);
          childCheckbox.addEventListener("change", () => {
            if (childCheckbox.checked) selected.add(item.address);
            else selected.delete(item.address);
            render();
          });
          const body = infoElement("div", "shared-address-body");
          const top = infoElement("div", "shared-address-top");
          top.append(
            infoElement("strong", "", item.label || maskAddress(item.address)),
            infoElement("span", "shared-address-group", already ? "已监控" : `后四位 ${String(item.address).slice(-4)}`),
          );
          body.append(
            top,
            infoElement("code", "shared-address-code", maskAddress(item.address)),
            infoElement("small", "shared-address-detail", item.note || "勾选后才会加入事件监控"),
          );
          child.append(childCheckbox, body);
          childList.append(child);
        }
        if (children.length > shownChildren.length) {
          childList.append(infoElement(
            "p",
            "shared-address-more",
            `该库先显示前 ${shownChildren.length} 个地址。输入地址后 4 位可定位具体地址，或直接用一级勾选选择整库。`,
          ));
        }
        card.append(childList);
      }
      list.append(card);
    }
    updateConfirm();
  };

  search.addEventListener("input", render);
  selectVisible.addEventListener("click", () => {
    for (const group of visibleGroups()) setGroupChecked(group, true);
    render();
  });
  clear.addEventListener("click", () => { selected.clear(); render(); });
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  confirm.addEventListener("click", async () => {
    if (!selected.size) return;
    confirm.disabled = true;
    error.textContent = "";
    let added = 0;
    let skipped = 0;
    try {
      for (const item of items.filter((entry) => selected.has(entry.address))) {
        const response = await request("/event-api/addresses", {
          method: "POST",
          body: JSON.stringify({
            network: "finney",
            addresses: [item.address],
            label: String(item.label || maskAddress(item.address)).slice(0, 120),
            library_book: item.book,
            address_role: item.source === "bittensor:SubtensorModule.SubnetOwner" ? "project_owner" : "",
            min_transfer_tao: "0",
          }),
        });
        added += Number(response.created || 0);
        skipped += Number(response.skipped || 0);
      }
      dialog.close();
      await reload();
      showToast(`已加入事件监控 ${added} 个地址${skipped ? `，跳过 ${skipped} 个` : ""}`);
    } catch (cause) {
      error.textContent = cause.message;
      confirm.disabled = false;
      updateConfirm();
    }
  });

  dialog.showModal();
  try {
    const response = await fetch("/api/event-monitor/library", { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `读取已知地址库失败 (${response.status})`);
    items = payload.items || [];
    unbookedCount = Number(payload.unbooked_count || 0);
    const byBook = new Map();
    for (const item of items) {
      const name = String(item.book || "").trim();
      if (!name) continue;
      if (!byBook.has(name)) byBook.set(name, []);
      byBook.get(name).push(item);
    }
    groups = [...byBook.entries()]
      .map(([name, bookItems]) => ({ name, items: bookItems }))
      .sort((left, right) => compareBookNames(left.name, right.name));
    render();
  } catch (cause) {
    summary.textContent = "读取失败";
    list.replaceChildren(infoElement("p", "address-empty", cause.message));
  }
}

async function copyTextToClipboard(value) {
  const text = String(value || "");
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0;";
    document.body.append(helper);
    helper.select();
    const copied = document.execCommand("copy");
    helper.remove();
    if (!copied) throw new Error("浏览器未允许复制");
  }
}

async function openManualNoteEditor(event) {
  const address = eventPrimaryAddress(event);
  if (!address) {
    showToast("该事件没有可编辑的主监控地址");
    return;
  }

  let watched = state.watchedAddresses.find((item) => String(item.address) === address);
  if (!watched) {
    try {
      const data = await request("/event-api/addresses");
      state.watchedAddresses = data.addresses || [];
      watched = state.watchedAddresses.find((item) => String(item.address) === address);
    } catch (error) {
      showToast(error.message);
      return;
    }
  }
  if (!watched?.address_id) {
    showToast("该地址已不在事件监控中，无法编辑手动备注");
    return;
  }

  const dialog = infoElement("dialog", "manual-note-dialog");
  const head = infoElement("div", "dialog-head");
  const title = infoElement("div");
  title.append(
    infoElement("p", "eyebrow", "ADDRESS NOTE"),
    infoElement("h2", "", "编辑手动备注"),
  );
  const close = infoElement("button", "icon-button", "×");
  close.type = "button";
  close.setAttribute("aria-label", "关闭");
  head.append(title, close);

  const copy = infoElement(
    "p",
    "dialog-copy",
    "原备注会保留；这里填写回购、官方、出货等补充标签。多个备注用逗号、顿号或分号分隔。",
  );
  const meta = infoElement("div", "manual-note-meta");
  meta.append(
    infoElement("span", "", `原备注：【${String(watched.label || "未命名地址").replace(/^[【\[]+|[】\]]+$/g, "")}】`),
    infoElement("code", "", maskAddress(address)),
  );
  const input = infoElement("textarea", "manual-note-input");
  input.rows = 3;
  input.maxLength = 240;
  input.value = watched.manual_note || "";
  input.placeholder = "例如：回购、官方、出货";
  const error = infoElement("p", "dialog-error");
  error.setAttribute("role", "alert");
  const actions = infoElement("div", "dialog-actions");
  const cancel = infoElement("button", "button ghost", "取消");
  cancel.type = "button";
  const save = infoElement("button", "button primary", "保存备注");
  save.type = "button";
  actions.append(error, cancel, save);
  dialog.append(head, copy, meta, input, actions);
  document.body.append(dialog);

  const closeDialog = () => dialog.close();
  close.addEventListener("click", closeDialog);
  cancel.addEventListener("click", closeDialog);
  dialog.addEventListener("close", () => dialog.remove());
  save.addEventListener("click", async () => {
    const value = input.value.trim();
    save.disabled = true;
    save.textContent = "保存中";
    error.textContent = "";
    try {
      await request(`/event-api/addresses/${watched.address_id}`, {
        method: "PUT",
        body: JSON.stringify({ manual_note: value }),
      });
      watched.manual_note = value;
      dialog.close();
      const selectedId = state.selectedId;
      await loadEvents(state.currentPage);
      if (selectedId) await selectEvent(selectedId);
      showToast(value ? "手动备注已保存" : "手动备注已清空");
    } catch (cause) {
      error.textContent = cause.message;
      save.disabled = false;
      save.textContent = "保存备注";
    }
  });
  input.addEventListener("keydown", (keyEvent) => {
    if ((keyEvent.ctrlKey || keyEvent.metaKey) && keyEvent.key === "Enter") {
      keyEvent.preventDefault();
      save.click();
    }
  });
  dialog.showModal();
  input.focus();
  input.select();
}

function manualNoteTags(value) {
  const seen = new Set();
  const tags = [];
  for (const part of String(value || "").split(/[,，;；、\n]+/)) {
    const clean = part.trim().replace(/^[【\[]+|[】\]]+$/g, "").trim();
    const key = compactTag(clean);
    if (!clean || seen.has(key)) continue;
    seen.add(key);
    tags.push(`【${clean}】`);
  }
  return tags;
}

function addressDisplayTags(label, manualNote) {
  const seen = new Set();
  const tags = [];
  const cleanLabel = String(label || "").trim().replace(/^[【\[]+|[】\]]+$/g, "").trim();
  for (const tag of [...(cleanLabel ? [`【${cleanLabel}】`] : []), ...manualNoteTags(manualNote)]) {
    const key = compactTag(tag);
    if (!seen.has(key)) {
      seen.add(key);
      tags.push(tag);
    }
  }
  return tags;
}

function renderAddressRow(item, reload) {
  const row = infoElement("article", `address-row${item.enabled ? "" : " disabled"}`);
  const identity = infoElement("div", "address-identity");
  const codeLine = infoElement("div", "address-code-line");
  const addressCode = infoElement("code", "", maskAddress(item.address));
  const copy = infoElement("button", "address-copy-button", "复制");
  copy.type = "button";
  copy.title = "复制完整 SS58 地址";
  copy.setAttribute("aria-label", `复制地址 ${maskAddress(item.address)}`);
  copy.addEventListener("click", async () => {
    try {
      await copyTextToClipboard(item.address);
      showToast(`已复制 ${maskAddress(item.address)}`);
    } catch (error) {
      showToast(error.message);
    }
  });
  codeLine.append(addressCode, copy);
  const manualDisplay = manualNoteTags(item.manual_note).join("");
  const fallbackLabel = isSystemAutoAddressLabel(item.label, item.address)
    ? ""
    : String(item.label || "").trim();
  const displayName = manualDisplay || String(item.library_book || "").trim() || fallbackLabel || maskAddress(item.address);
  identity.append(infoElement("strong", "", displayName));
  if (item.address_role === "project_owner") {
    identity.append(infoElement("span", "address-official-badge", "官方项目方"));
  }
  identity.append(codeLine);
  const labelEditor = infoElement("div", "address-label-editor");
  const label = infoElement("input", "address-label-input");
  label.value = item.label || "";
  label.maxLength = 120;
  label.placeholder = "地址名称 / 原备注，例如 SN53 项目方";
  const manualNote = infoElement("input", "address-manual-note-input");
  manualNote.value = item.manual_note || "";
  manualNote.maxLength = 240;
  manualNote.placeholder = "手动备注，例如 回购、官方、出货（逗号分隔）";
  manualNote.setAttribute("aria-label", "地址手动备注，多个备注可用逗号分隔");
  const notePreview = infoElement("div", "address-note-preview");
  const refreshNotePreview = () => {
    const tags = addressDisplayTags(label.value, manualNote.value);
    notePreview.replaceChildren(
      ...(tags.length
        ? tags.map((tag) => infoElement("span", "address-note-tag", tag))
        : [infoElement("span", "address-note-empty", "暂无备注标签")]),
    );
  };
  refreshNotePreview();
  label.addEventListener("input", refreshNotePreview);
  manualNote.addEventListener("input", refreshNotePreview);
  label.addEventListener("change", async () => {
    try {
      await request(`/event-api/addresses/${item.address_id}`, {
        method: "PUT",
        body: JSON.stringify({ label: label.value.trim() }),
      });
      showToast("地址原备注已保存");
      await reload();
    } catch (error) {
      showToast(error.message);
    }
  });
  manualNote.addEventListener("change", async () => {
    try {
      await request(`/event-api/addresses/${item.address_id}`, {
        method: "PUT",
        body: JSON.stringify({ manual_note: manualNote.value.trim() }),
      });
      showToast("地址手动备注已保存；后续新事件使用新备注");
      await reload();
    } catch (error) {
      showToast(error.message);
    }
  });
  labelEditor.append(label, manualNote, notePreview);
  const threshold = infoElement("div", "address-threshold");
  const thresholdText = infoElement("span");
  thresholdText.className = "address-threshold-copy";
  thresholdText.dataset.threshold = String(item.min_transfer_tao || 0);
  thresholdText.textContent = Number(item.min_transfer_tao) > 0
    ? `忽略低于 ${formatTao(item.min_transfer_tao)} TAO 的普通转账`
    : "不过滤普通转账";
  const thresholdControl = infoElement("div", "address-threshold-control");
  const thresholdInput = infoElement("input");
  thresholdInput.type = "number";
  thresholdInput.min = "0";
  thresholdInput.step = "0.000000001";
  thresholdInput.value = String(item.min_transfer_tao || 0);
  thresholdInput.setAttribute("aria-label", "小额转账过滤阈值 TAO");
  const saveThresholdButton = infoElement("button", "button address-threshold-save", "保存");
  saveThresholdButton.type = "button";
  saveThresholdButton.title = "保存小额普通转账过滤阈值";
  const saveThreshold = async () => {
    saveThresholdButton.disabled = true;
    const previousText = saveThresholdButton.textContent;
    saveThresholdButton.textContent = "保存中";
    try {
      const value = thresholdInput.value.trim() || "0";
      await request(`/event-api/addresses/${item.address_id}`, {
        method: "PUT",
        body: JSON.stringify({ min_transfer_tao: value }),
      });
      thresholdText.dataset.threshold = String(Number(value) > 0 ? value : 0);
      thresholdText.textContent = Number(value) > 0
        ? `忽略低于 ${formatTao(value)} TAO 的普通转账`
        : "不过滤普通转账";
      showToast(Number(value) > 0 ? `已保存：忽略低于 ${formatTao(value)} TAO 的普通转账` : "已保存：关闭该地址的小额转账过滤");
      await reload();
    } catch (error) {
      thresholdInput.value = String(item.min_transfer_tao || 0);
      showToast(error.message);
    } finally {
      if (saveThresholdButton.isConnected) {
        saveThresholdButton.disabled = false;
        saveThresholdButton.textContent = previousText;
      }
    }
  };
  saveThresholdButton.addEventListener("click", saveThreshold);
  thresholdInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveThreshold();
    }
  });
  thresholdControl.append(thresholdInput, saveThresholdButton, infoElement("em", "", "TAO"));
  threshold.append(thresholdText, thresholdControl);
  const toggleLabel = infoElement("label", "address-toggle");
  const toggle = infoElement("input");
  toggle.type = "checkbox";
  toggle.checked = item.enabled;
  toggleLabel.append(toggle, document.createTextNode("监控"));
  toggle.addEventListener("change", async () => {
    try {
      await request(`/event-api/addresses/${item.address_id}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: toggle.checked }),
      });
      showToast(toggle.checked ? "地址监控已开启" : "地址监控已暂停");
      await reload();
    } catch (error) {
      toggle.checked = !toggle.checked;
      showToast(error.message);
    }
  });
  const remove = infoElement("button", "button ghost danger", "移出监控");
  remove.type = "button";
  remove.addEventListener("click", async () => {
    try {
      await request(`/event-api/addresses/${item.address_id}`, { method: "DELETE" });
      showToast("地址已移出事件监控；共享已知地址库不受影响");
      await reload();
    } catch (error) {
      showToast(error.message);
    }
  });
  const actions = infoElement("div", "address-actions");
  actions.append(toggleLabel, remove);
  row.append(identity, labelEditor, threshold, actions);
  return row;
}

function formatTao(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "0";
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 9 });
}

function maskAddress(value) {
  const text = String(value || "");
  return text.length <= 8 ? text : `${text.slice(0, 4)}…${text.slice(-4)}`;
}

function renderFeaturesView() {
  const product = state.product.product;
  const intro = infoElement("p", "info-intro", `当前版本 ${product.version} · ${product.status} · 更新于 ${product.released_at}`);
  const grid = infoElement("div", "info-grid");
  state.product.features.forEach((feature) => {
    const card = infoElement("article", "info-card");
    card.append(infoElement("h3", "", feature.name), infoElement("p", "", feature.detail));
    card.append(infoElement("span", `feature-status${feature.status === "后续" ? " planned" : ""}`, feature.status));
    grid.append(card);
  });
  const tradingTitle = infoElement("h2", "", "交易开放监控");
  tradingTitle.style.marginTop = "28px";
  const tradingCopy = infoElement("p", "info-intro", "SubtokenEnabled=true 是当前运行时定义的子网交易开关。系统每 5 秒比较前后状态，false → true 时创建并推送交易开放事件。");
  const tradingGrid = infoElement("div", "trading-grid");
  tradingGrid.append(infoElement("p", "info-intro", "正在读取链上状态…"));
  el.infoView.append(intro, grid, tradingTitle, tradingCopy, tradingGrid);
  request("/event-api/trading").then((data) => {
    tradingGrid.replaceChildren();
    const featured = data.subnets.filter((state) => [78, 99].includes(state.netuid));
    const cards = [...featured, ...data.subnets.filter((state) => ![78, 99].includes(state.netuid))].slice(0, 12);
    if (!cards.length) {
      tradingGrid.append(infoElement("p", "info-intro", "交易状态尚未完成首次链上同步。"));
      return;
    }
    cards.forEach((state) => {
      const card = infoElement("article", "trading-card");
      card.append(infoElement("strong", "", `SN${state.netuid}`));
      card.append(infoElement("span", state.enabled ? "open" : "closed", state.enabled ? "交易已开放" : "交易未开放"));
      card.append(infoElement("small", "", state.registration_block
        ? `注册区块 ${Number(state.registration_block).toLocaleString("zh-CN")}`
        : "注册区块未知"));
      card.append(infoElement("small", "", `状态观察 ${formatFullTime(state.observed_at)}`));
      tradingGrid.append(card);
    });
  }).catch((error) => {
    tradingGrid.replaceChildren(infoElement("p", "info-intro", error.message));
  });
}

function renderGuideView() {
  const intro = infoElement("p", "info-intro", "以下步骤覆盖当前首期版本。页面数据以本地服务实际采集结果为准。");
  const list = infoElement("div", "guide-list");
  state.product.guide.forEach((item) => {
    const entry = infoElement("article", "guide-item");
    entry.append(infoElement("h3", "", item.title), infoElement("p", "", item.body));
    list.append(entry);
  });
  el.infoView.append(intro, list);
}

function renderChangelogView() {
  const intro = infoElement("p", "info-intro", `当前运行版本 ${state.product.product.version}，发布日期 ${state.product.product.released_at}。`);
  const releases = infoElement("div", "");
  state.product.releases.forEach((release) => {
    const row = infoElement("article", "release");
    const version = infoElement("div", "release-version");
    version.append(infoElement("strong", "", release.version), infoElement("span", "", release.date));
    const changes = infoElement("div");
    changes.append(infoElement("h3", "", release.title));
    const values = infoElement("ul");
    release.changes.forEach((change) => values.append(infoElement("li", "", change)));
    changes.append(values);
    row.append(version, changes);
    releases.append(row);
  });
  el.infoView.append(intro, releases);
}

async function renderLogsView() {
  const intro = infoElement("p", "info-intro", "日志保存在本地 SQLite 中，默认保留最近 5,000 条。异常详情仍以完整日志文件为准。");
  const toolbar = infoElement("div", "log-toolbar");
  const select = infoElement("select");
  select.setAttribute("aria-label", "日志级别");
  [["", "全部级别"], ["INFO", "INFO"], ["WARNING", "WARNING"], ["ERROR", "ERROR"]].forEach(([value, label]) => {
    const item = infoElement("option", "", label);
    item.value = value;
    select.append(item);
  });
  const list = infoElement("div", "log-list");
  list.append(infoElement("p", "info-intro", "正在读取日志…"));
  toolbar.append(select);
  el.infoView.append(intro, toolbar, list);
  const refresh = async () => {
    try {
      const suffix = select.value ? `&level=${encodeURIComponent(select.value)}` : "";
      const data = await request(`/event-api/logs?limit=300${suffix}`);
      list.replaceChildren();
      if (!data.logs.length) {
        list.append(infoElement("p", "info-intro", "暂无日志。"));
        return;
      }
      data.logs.forEach((log) => {
        const row = infoElement("div", "log-row");
        row.append(
          infoElement("time", "", formatFullTime(log.created_at)),
          infoElement("span", `log-level ${log.level.toLowerCase()}`, log.level),
          infoElement("span", "component", log.component),
          infoElement("span", "log-message", log.message),
        );
        list.append(row);
      });
    } catch (error) {
      list.replaceChildren(infoElement("p", "info-intro", error.message));
    }
  };
  select.addEventListener("change", refresh);
  await refresh();
  state.logTimer = setInterval(refresh, 10000);
}

let searchDebounce = null;
let inlineDrag = null;
let inlineResizeTimer = null;
const inlineResizeObserver = "ResizeObserver" in window ? new ResizeObserver(() => {
  clearTimeout(inlineResizeTimer);
  inlineResizeTimer = setTimeout(saveInlineFloatingBounds, 180);
}) : null;

const revealPanel = () => {
  setPanelCleared(false, { render: false });
};
el.search.addEventListener("input", () => {
  revealPanel();
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    loadEvents(1).catch((error) => showToast(error.message));
  }, 250);
});
el.typeFilter.addEventListener("change", () => {
  revealPanel();
  loadEvents(1).catch((error) => showToast(error.message));
});
el.statusFilter.addEventListener("change", () => {
  revealPanel();
  loadEvents(1).catch((error) => showToast(error.message));
});
el.sourceFilter.addEventListener("change", () => {
  revealPanel();
  loadEvents(1).catch((error) => showToast(error.message));
});
const querySubnet = () => {
  const raw = el.subnetFilter.value.trim();
  const netuid = raw === "" ? null : Number(raw);
  if (netuid != null && (!Number.isInteger(netuid) || netuid < 0 || netuid > 65535)) {
    showToast("子网编号必须是 0–65535 的整数");
    return;
  }
  revealPanel();
  state.subnetFilter = netuid;
  loadEvents(1).catch((error) => showToast(error.message));
};
el.subnetQuery.addEventListener("click", querySubnet);
el.subnetFilter.addEventListener("keydown", (event) => {
  if (event.key === "Enter") querySubnet();
});
el.pageFirst.addEventListener("click", () => loadEvents(1).catch((error) => showToast(error.message)));
el.pagePrev.addEventListener("click", () => loadEvents(state.currentPage - 1).catch((error) => showToast(error.message)));
el.pageNext.addEventListener("click", () => loadEvents(state.currentPage + 1).catch((error) => showToast(error.message)));
el.pageLast.addEventListener("click", () => loadEvents(totalPages()).catch((error) => showToast(error.message)));
el.pageInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const page = Math.max(1, Math.min(totalPages(), Number(el.pageInput.value) || 1));
  loadEvents(page).catch((error) => showToast(error.message));
});
el.clearPanelButton.addEventListener("click", () => {
  closeDetail();
  state.paused = false;
  state.pending = [];
  el.pause.checked = false;
  state.currentPage = 1;
  setPanelCleared(true);
  showToast("首页显示已清空，本地记录仍保留");
});
el.pause.addEventListener("change", () => {
  state.paused = el.pause.checked;
  if (!state.paused) flushPending();
  render();
});
el.chainReset.addEventListener("click", async () => {
  const previous = el.chainReset.textContent;
  el.chainReset.disabled = true;
  el.chainReset.textContent = "复位中";
  el.chainHealthCopy.textContent = "正在取消旧请求并重新连接主链…";
  try {
    const result = await request("/api/event-monitor/chain/reset", { method: "POST" });
    showToast(result.message || "主链连接已复位");
    await loadStatus();
    setTimeout(() => loadStatus().catch(() => {}), 2500);
  } catch (error) {
    showToast(error.message);
    await loadStatus().catch(() => {});
  } finally {
    if (el.chainReset.isConnected) {
      el.chainReset.disabled = false;
      el.chainReset.textContent = previous;
    }
  }
});

el.refresh.addEventListener("click", async () => {
  try {
    await Promise.all([loadEvents(state.currentPage), loadStatus(), loadProduct()]);
    showToast("数据已刷新");
  } catch (error) {
    showToast(error.message);
  }
});
el.simulationButton.addEventListener("click", openSimulationDialog);
el.floatingButton.addEventListener("click", openFloatingSettings);
el.floatingClose.addEventListener("click", () => el.floatingDialog.close());
el.eventFontSizeInput.addEventListener("input", () => {
  applyEventFontSize(el.eventFontSizeInput.value);
});
el.floatingForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const data = new FormData(el.floatingForm);
  applyEventFontSize(data.get("event_font_size") || el.eventFontSizeInput.value);
  const mode = String(data.get("floating_mode") || "pip");
  el.floatingDialog.close();
  openFloatingMonitor(mode).catch((error) => showToast(error.message));
});
el.inlineFloatingClose.addEventListener("click", () => {
  saveInlineFloatingBounds();
  el.inlineFloating.hidden = true;
});
el.inlineFloatingHead.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || event.target.closest("button")) return;
  const rect = el.inlineFloating.getBoundingClientRect();
  inlineDrag = {
    pointerId: event.pointerId,
    offsetX: event.clientX - rect.left,
    offsetY: event.clientY - rect.top,
  };
  el.inlineFloatingHead.setPointerCapture(event.pointerId);
  el.inlineFloating.classList.add("is-dragging");
});
el.inlineFloatingHead.addEventListener("pointermove", (event) => {
  if (!inlineDrag || inlineDrag.pointerId !== event.pointerId) return;
  const rect = el.inlineFloating.getBoundingClientRect();
  const left = Math.max(8, Math.min(event.clientX - inlineDrag.offsetX, window.innerWidth - rect.width - 8));
  const top = Math.max(8, Math.min(event.clientY - inlineDrag.offsetY, window.innerHeight - rect.height - 8));
  el.inlineFloating.style.left = `${Math.round(left)}px`;
  el.inlineFloating.style.top = `${Math.round(top)}px`;
  el.inlineFloating.style.right = "auto";
  el.inlineFloating.style.bottom = "auto";
});
const finishInlineDrag = (event) => {
  if (!inlineDrag || inlineDrag.pointerId !== event.pointerId) return;
  inlineDrag = null;
  el.inlineFloating.classList.remove("is-dragging");
  if (el.inlineFloatingHead.hasPointerCapture(event.pointerId)) {
    el.inlineFloatingHead.releasePointerCapture(event.pointerId);
  }
  saveInlineFloatingBounds();
};
el.inlineFloatingHead.addEventListener("pointerup", finishInlineDrag);
el.inlineFloatingHead.addEventListener("pointercancel", finishInlineDrag);
window.addEventListener("resize", restoreInlineFloatingBounds);
inlineResizeObserver?.observe(el.inlineFloating);
uiChannel?.addEventListener("message", (event) => {
  if (event.data && typeof event.data.type === "string") {
    applyExternalUiState(event.data.type, event.data.value);
  }
});
window.addEventListener("storage", (event) => {
  if (event.key === UI_KEYS.eventFontSize) applyExternalUiState("event-font-size", event.newValue);
  if (event.key === UI_KEYS.panelCleared) applyExternalUiState("panel-cleared", event.newValue === "true");
});
window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || event.data?.source !== "subnet-pulse-floating") return;
  if (event.data.type === "panel-cleared") applyExternalUiState("panel-cleared", Boolean(event.data.value));
  if (event.data.type === "event-font-size") applyExternalUiState("event-font-size", event.data.value);
});
applyEventFontSize(state.eventFontSize, { persist: false, broadcast: false });
el.simulationClose.addEventListener("click", () => el.simulationDialog.close());
el.deleteSimulations.addEventListener("click", async () => {
  if (!window.confirm("确定永久删除全部模拟测试事件？链上、GitHub 和地址监控记录不会受影响。")) return;
  el.deleteSimulations.disabled = true;
  el.simulationError.textContent = "";
  try {
    const result = await request("/event-api/simulation/events", { method: "DELETE" });
    await Promise.allSettled([loadEvents(1), loadStatus()]);
    showToast(result.deleted
      ? `已从数据库永久删除 ${result.deleted} 条模拟事件`
      : "当前没有模拟事件需要删除");
  } catch (error) {
    el.simulationError.textContent = error.message;
  } finally {
    el.deleteSimulations.disabled = false;
  }
});
el.simulationSignal.addEventListener("change", updateSimulationPreview);
el.simulationAddress.addEventListener("change", updateSimulationPreview);
el.simulationCompletion.addEventListener("change", updateSimulationPreview);
el.simulationForm.elements.netuid.addEventListener("input", updateSimulationPreview);
el.simulationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.simulationError.textContent = "";
  const data = new FormData(el.simulationForm);
  try {
    const response = await request("/event-api/simulation/events", {
      method: "POST",
      body: JSON.stringify((() => {
        const signal = selectedSimulationSignal();
        const addressSignal = signal?.simulation_kind === "address";
        return {
          network: "finney",
          netuid: addressSignal && !signal.netuid_required
            ? null
            : Number(data.get("netuid")),
          signal_id: data.get("signal_id"),
          address_id: addressSignal ? Number(data.get("address_id")) : null,
          delay_seconds: el.simulationCompletion.checked
            ? Number(data.get("delay_seconds") || 2)
            : 0,
        };
      })()),
    });
    el.simulationDialog.close();
    state.paused = false;
    state.pending = [];
    state.subnetFilter = null;
    state.currentPage = 1;
    el.pause.checked = false;
    el.subnetFilter.value = "";
    el.search.value = "";
    el.typeFilter.value = "";
    el.statusFilter.value = "";
    el.sourceFilter.value = "";
    setPanelCleared(false, { render: false });
    await loadEvents(1);
    showToast(`已发送并显示：${eventSummaryText(response.event)}`);
  } catch (error) {
    el.simulationError.textContent = error.message;
  }
});
window.addEventListener("popstate", routeChanged);
el.detailBackdrop.addEventListener("click", returnToEventStream);

el.repoButton?.addEventListener("click", () => {
  el.repoError.textContent = "";
  el.repoForm.reset();
  if (state.route.kind === "subnet") el.repoForm.elements.netuid.value = state.route.netuid;
  el.repoDialog.showModal();
});
el.repoClose.addEventListener("click", () => el.repoDialog.close());
el.repoForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.repoError.textContent = "";
  const form = new FormData(el.repoForm);
  const repository = String(form.get("repository") || "").trim().replace(/^https?:\/\/github\.com\//i, "");
  const [owner, name] = repository.replace(/\.git$/i, "").split("/");
  try {
    await request("/event-api/repositories", {
      method: "POST",
      body: JSON.stringify({
        netuid: Number(form.get("netuid")),
        reviewed_by: form.get("reviewed_by"),
        owner,
        name,
      }),
    });
    el.repoDialog.close();
    showToast("仓库关联已保存");
  } catch (error) {
    el.repoError.textContent = error.message;
  }
});
el.profileClose.addEventListener("click", () => el.profileDialog.close());
el.profileForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.profileError.textContent = "";
  const form = new FormData(el.profileForm);
  const netuid = Number(form.get("netuid"));
  const payload = { network: "finney", edited_by: form.get("edited_by") };
  for (const field of ["name", "website_url", "x_url", "github_url", "discord_url", "logo_url", "description", "additional"]) {
    payload[field] = form.get(field) || "";
  }
  try {
    await request(`/event-api/subnet-profiles/${netuid}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    el.profileDialog.close();
    showToast(`SN${netuid} 档案已保存`);
    renderInfoView();
  } catch (error) {
    el.profileError.textContent = error.message;
  }
});

Promise.all([loadEvents(), loadStatus(), loadProduct(), loadSimulationSignals()])
  .then(() => {
    render();
    if (state.route.kind === "event") selectEvent(state.route.eventId);
    connectStream();
  })
  .catch((error) => showToast(error.message));
setInterval(loadStatus, 5000);
setInterval(renderHeartbeatCountdown, 1000);
setInterval(pollEventLibrarySync, 30000);
setTimeout(pollEventLibrarySync, 2000);


// 事件 API 返回的导出/详情链接由后端拼出原始路径；统一在 DOM 层补全合并前缀。
function rewriteAllEventLinks() {
  document.querySelectorAll("a[href]").forEach(rewriteEventLink);
}
const eventLinkObserver = new MutationObserver(rewriteAllEventLinks);
eventLinkObserver.observe(document.body, {
  subtree: true,
  childList: true,
  attributes: true,
  attributeFilter: ["href"],
});
document.addEventListener("click", (event) => {
  const link = event.target.closest?.("a[href]");
  if (link) rewriteEventLink(link);
}, true);
rewriteAllEventLinks();
