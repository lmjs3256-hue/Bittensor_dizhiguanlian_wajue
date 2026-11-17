"use strict";

const UI_KEYS = {
  panelCleared: "subnetPulse.panelCleared",
  eventFontSize: "subnetPulse.eventFontSize",
  floatingCompact: "subnetPulse.floatingCompact",
};
const FONT_MIN = 11;
const FONT_MAX = 18;
const list = document.querySelector("#events");
const empty = document.querySelector("#empty");
const count = document.querySelector("#count");
const chain = document.querySelector("#chain");
const live = document.querySelector("#live");
const liveCopy = document.querySelector("#live-copy");
const liveDetail = document.querySelector("#live-detail");
const fontInput = document.querySelector("#font-size");
const fontOutput = document.querySelector("#font-size-output");
const clearHome = document.querySelector("#clear-home");
const restoreHome = document.querySelector("#restore-home");
const toolbar = document.querySelector("#floating-toolbar");
const toolbarToggle = document.querySelector("#toolbar-toggle");
let streamState = "connecting";
let panelCleared = false;
let statusReceivedAt = null;
let statusErrorAt = null;
let worker = null;
let details = {};
let channel = null;
try {
  if ("BroadcastChannel" in window) channel = new BroadcastChannel("subnet-pulse-ui");
} catch {
  channel = null;
}

function esc(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function time(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
}

function clampFont(value) {
  const size = Number(value);
  return Number.isFinite(size) ? Math.max(FONT_MIN, Math.min(FONT_MAX, Math.round(size))) : 13;
}

function readFont() {
  try {
    return clampFont(localStorage.getItem(UI_KEYS.eventFontSize) || 13);
  } catch {
    return 13;
  }
}

function applyFont(value, options = {}) {
  const size = clampFont(value);
  document.documentElement.style.setProperty("--event-font-size", `${size}px`);
  fontInput.value = String(size);
  fontOutput.textContent = `${size} px`;
  if (options.persist !== false) {
    try { localStorage.setItem(UI_KEYS.eventFontSize, String(size)); } catch {}
  }
  if (options.broadcast !== false) postUi("event-font-size", size);
  return size;
}

function postUi(type, value) {
  const message = { source: "subnet-pulse-floating", type, value };
  try { channel?.postMessage({ type, value }); } catch {}
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage(message, location.origin);
    }
  } catch {}
  try {
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage(message, location.origin);
    }
  } catch {}
}

function readPanelCleared() {
  try { return localStorage.getItem(UI_KEYS.panelCleared) === "true"; } catch { return false; }
}

function updatePanelButtons(cleared) {
  clearHome.disabled = cleared;
  restoreHome.disabled = !cleared;
  clearHome.classList.toggle("active", cleared);
  restoreHome.classList.toggle("active", !cleared);
}

function applyPanelCleared(value, options = {}) {
  panelCleared = Boolean(value);
  updatePanelButtons(panelCleared);
  if (panelCleared) {
    list.replaceChildren();
    count.textContent = "首页显示已清空";
    empty.textContent = "首页事件显示已清空；本地数据库记录仍然保留。";
    empty.hidden = false;
  } else if (options.reload !== false) {
    empty.textContent = "正在等待第一条事件";
    loadEvents().catch(() => {});
  }
}

function setPanelCleared(value, options = {}) {
  const cleared = Boolean(value);
  applyPanelCleared(cleared, { reload: false });
  if (options.persist !== false) {
    try { localStorage.setItem(UI_KEYS.panelCleared, cleared ? "true" : "false"); } catch {}
  }
  if (options.broadcast !== false) postUi("panel-cleared", cleared);
  if (!cleared && options.reload !== false) loadEvents().catch(() => {});
}

async function loadEvents() {
  try {
    const response = await fetch("/api/events?limit=30&offset=0", { cache: "no-store" });
    if (!response.ok) throw new Error(`事件接口返回 ${response.status}`);
    const data = await response.json();
    if (panelCleared) return;
    count.textContent = `${Number(data.total || 0).toLocaleString("zh-CN")} 条事件`;
    list.innerHTML = (data.events || []).map((event, index) => {
      const tone = ["red", "yellow", "green"][index % 3];
      const eventTimestamp = new Date(event.updated_at || event.last_enriched_at || event.first_seen_at || event.event_at).getTime();
      const latest = index === 0 && Number.isFinite(eventTimestamp) && Date.now() - eventTimestamp >= 0 && Date.now() - eventTimestamp <= 30000;
      const priorityLevel = Number(event.priority_level || 0);
      const priorityClass = priorityLevel === 2 ? " priority-critical" : priorityLevel === 1 ? " priority-watch" : "";
      const simulated = event.source === "simulation" || event.confidence === "simulated" || event.details?.simulated === true;
      const source = event.source === "address"
        ? `地址监控${simulated ? " · 模拟" : ""}`
        : event.source === "github" ? "GitHub" : event.source === "simulation" ? "模拟" : "链上";
      return `<article class="event tone-${tone}${priorityClass}${latest ? " is-latest-event" : ""}" data-latest-until="${latest ? eventTimestamp + 30000 : ""}">
        <i></i>
        <div class="event-copy"><strong>${esc(event.summary)}</strong><small>${esc(source)} · ${esc(event.event_type)} · ${esc(event.event_name)}</small></div>
        <time>${esc(time(event.event_at))}</time>
      </article>`;
    }).join("");
    const latestNode = list.querySelector(".is-latest-event");
    const latestUntil = Number(latestNode?.dataset.latestUntil);
    if (latestNode && Number.isFinite(latestUntil)) {
      setTimeout(
        () => latestNode.classList.remove("is-latest-event"),
        Math.max(0, latestUntil - Date.now()),
      );
    }
    empty.hidden = (data.events || []).length > 0;
  } catch (error) {
    empty.textContent = `事件读取失败：${error.message}`;
    empty.hidden = false;
    throw error;
  }
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`状态接口返回 ${response.status}`);
    const data = await response.json();
    worker = data.workers?.chain || null;
    details = worker?.details || {};
    statusReceivedAt = Date.now();
    statusErrorAt = null;
    chain.textContent = details.best_height
      ? `区块 ${Number(details.best_height).toLocaleString("zh-CN")} · 落后 ${details.lag_blocks ?? "—"}`
      : "等待链上心跳";
  } catch (error) {
    worker = null;
    statusErrorAt = Date.now();
    chain.textContent = `状态读取失败：${error.message}`;
  }
  renderLiveState();
}

function heartbeatAge() {
  if (!worker?.updated_at) return null;
  const timestamp = new Date(worker.updated_at).getTime();
  return Number.isFinite(timestamp) ? Math.max(0, Math.floor((Date.now() - timestamp) / 1000)) : null;
}

function renderLiveState() {
  const age = heartbeatAge();
  const statusAge = statusReceivedAt == null ? Infinity : Math.max(0, (Date.now() - statusReceivedAt) / 1000);
  const chainHealthy = worker?.state === "connected" && age != null && age <= 30;
  const apiHealthy = statusErrorAt == null && statusAge <= 15;
  const online = chainHealthy && streamState === "connected" && apiHealthy;
  live.className = `live ${online ? "online" : "offline"}`;
  live.dataset.state = online ? "online" : (streamState === "connecting" ? "waiting" : "offline");
  if (online) {
    liveCopy.textContent = "正在实时监听";
    liveDetail.textContent = `链上心跳 ${age < 3 ? "刚刚" : `${age} 秒前`} · 实时通道正常`;
  } else if (statusErrorAt != null || statusAge > 15) {
    liveCopy.textContent = "监听暂停";
    liveDetail.textContent = "本地状态接口无响应";
  } else if (streamState !== "connected") {
    liveCopy.textContent = "监听暂停";
    liveDetail.textContent = "实时事件通道正在重连";
  } else if (age != null && age > 30) {
    liveCopy.textContent = "监听暂停";
    liveDetail.textContent = `链上心跳已中断 ${age} 秒`;
  } else if (worker?.state === "degraded") {
    liveCopy.textContent = "监听暂停";
    liveDetail.textContent = details.error || "链上连接异常";
  } else {
    liveCopy.textContent = "正在连接监听";
    liveDetail.textContent = "等待链上心跳与实时通道";
  }
}

function connect() {
  const stream = new EventSource("/api/stream");
  stream.addEventListener("ready", () => {
    streamState = "connected";
    loadStatus();
  });
  stream.onopen = () => {
    streamState = "connected";
    renderLiveState();
  };
  stream.addEventListener("event", () => loadEvents().catch(() => {}));
  stream.onerror = () => {
    streamState = "reconnecting";
    renderLiveState();
  };
}

function readCompact() {
  try { return localStorage.getItem(UI_KEYS.floatingCompact) === "true"; } catch { return false; }
}

function applyCompact(value, options = {}) {
  const compact = Boolean(value);
  document.body.classList.toggle("floating-compact", compact);
  toolbar.hidden = compact;
  toolbarToggle.setAttribute("aria-expanded", String(!compact));
  toolbarToggle.setAttribute("aria-label", compact ? "展开浮窗工具" : "收起浮窗工具");
  toolbarToggle.title = compact ? "展开工具" : "收起工具";
  toolbarToggle.textContent = compact ? "⌄" : "⌃";
  if (options.persist !== false) {
    try { localStorage.setItem(UI_KEYS.floatingCompact, compact ? "true" : "false"); } catch {}
  }
}

toolbarToggle.addEventListener("click", () => {
  applyCompact(!document.body.classList.contains("floating-compact"));
});

document.querySelector("#refresh").addEventListener("click", () => {
  Promise.allSettled([loadEvents(), loadStatus()]);
});
fontInput.addEventListener("input", () => applyFont(fontInput.value));
clearHome.addEventListener("click", () => setPanelCleared(true));
restoreHome.addEventListener("click", () => setPanelCleared(false));
channel?.addEventListener("message", (event) => {
  if (event.data?.type === "event-font-size") applyFont(event.data.value, { persist: false, broadcast: false });
  if (event.data?.type === "panel-cleared") applyPanelCleared(Boolean(event.data.value));
});
window.addEventListener("storage", (event) => {
  if (event.key === UI_KEYS.eventFontSize) applyFont(event.newValue, { persist: false, broadcast: false });
  if (event.key === UI_KEYS.panelCleared) applyPanelCleared(event.newValue === "true");
});

applyCompact(readCompact(), { persist: false });
applyFont(readFont(), { persist: false, broadcast: false });
panelCleared = readPanelCleared();
updatePanelButtons(panelCleared);
if (panelCleared) applyPanelCleared(true, { reload: false });
else loadEvents().catch(() => {});
loadStatus();
connect();
setInterval(loadStatus, 5000);
setInterval(renderLiveState, 1000);
