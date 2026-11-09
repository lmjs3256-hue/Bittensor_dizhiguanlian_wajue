"""Small, rate-limited client for Taostats indexed transfer history."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx


API_URL = "https://api.taostats.io/v1/transfers"
VALIDATORS_ACTIVE_URL = "https://api.taostats.io/v1/validators/active"
EXCHANGE_ADDRESSES_URL = "https://api.taostats.io/v1/exchange-addresses"
STAKE_EVENTS_URL = "https://api.taostats.io/v1/subnets/stake-events"
ACCOUNT_LATEST_URL = "https://api.taostats.io/api/account/latest/v1"
# 每把 key 每分钟允许的本地请求数。
#
# **不是固定 5，也不做"自动识别套餐"**——Taostats 没提供任何接口能让本地查询
# 出「这把 key 属于哪个套餐、每分钟多少额度」，服务端只在超限时回 429。所以
# 这里做成**可配置**：默认 5（免费档），升级后在页面填实际额度即可。
# 配得比真实额度高只会让你多吃几次 429（会被自动冷却），配低了会白白浪费额度。
DEFAULT_REQUESTS_PER_MINUTE = 5
LOCAL_REQUESTS_PER_MINUTE = DEFAULT_REQUESTS_PER_MINUTE


def set_requests_per_minute(value: int) -> int:
    """设置每把 key 每分钟的本地限速；返回生效值。

    只影响本地保护窗口，不改变 Taostats 侧的配额。运行时生效，不需要重启。
    """

    global LOCAL_REQUESTS_PER_MINUTE
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_REQUESTS_PER_MINUTE
    # 太低会让查询几乎无法推进，太高则只是多吃 429；夹在合理区间内。
    LOCAL_REQUESTS_PER_MINUTE = max(1, min(limit, 600))
    return LOCAL_REQUESTS_PER_MINUTE


def requests_per_minute() -> int:
    return LOCAL_REQUESTS_PER_MINUTE
CACHE_TTL_SECONDS = 300


def is_configured() -> bool:
    """有没有可用的 key。

    池非空也算已配置——只跑池、不设单 key 兼容字段的场景（测试、脚本直接
    调 set_key_pool）必须能通过这道判断，否则会在接口层被误拦成"未配置"。
    """

    with _lock:
        # 用 globals() 取：这个函数定义在池声明之前，模块导入期间若被调用
        # 直接写 _key_pool 会 NameError。
        if globals().get("_key_pool"):
            return True
    return bool(_configured_api_key())


class TaostatsError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request_made: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.request_made = request_made
        self.status_code = status_code


class TaostatsAuthError(TaostatsError):
    pass


class TaostatsRateLimitError(TaostatsError):
    pass


class TaostatsUnavailableError(TaostatsError):
    pass


@dataclass
class TransferPage:
    rows: list[dict[str, Any]]
    next_page: int | None
    truncated_by_window: bool = False
    from_cache: bool = False
    total_items: int = 0
    total_pages: int = 0
    per_page: int = 200

    @property
    def has_next(self) -> bool:
        return self.next_page is not None


@dataclass(frozen=True)
class AccountProfile:
    """起始地址的账户画像：注册日期 + 余额构成。

    Taostats 的 ``/api/account/latest/v1`` 一次返回这些字段，比逐个查区块
    便宜得多；拿不到时各字段留 None，界面显示「未获取」，绝不猜。
    """

    created_on_date: str | None = None
    created_on_network: str | None = None
    balance_free_tao: str | None = None
    balance_staked_tao: str | None = None
    balance_staked_alpha_as_tao: str | None = None
    balance_reserved_tao: str | None = None
    balance_total_tao: str | None = None
    rank: int | None = None
    from_cache: bool = False


_lock = threading.Lock()
_request_times: deque[float] = deque()
_cache: dict[tuple[str, int], tuple[float, TransferPage]] = {}
_stake_cache: dict[tuple[str, int], tuple[float, TransferPage]] = {}
_validator_cache: tuple[float, frozenset[str]] | None = None
_exchange_cache: tuple[float, tuple[dict[str, str], ...]] | None = None
_profile_cache: dict[str, tuple[float, AccountProfile]] = {}
# 质押操作次数缓存：address → (单调时钟, 总数)。与分页缓存分开，避免互相污染。
_stake_total_cache: dict[str, tuple[float, int]] = {}
_key_override: str | None = None
_key_override_active = False


# -- API key 池 -----------------------------------------------------------------
#
# 多人同时用会互相挤：限速器原先是**一个全局 deque**，所有人共用 5 次/分钟的
# 窗口，先到的人占满、后到的人直接吃 429。多把 key 是唯一能线性扩容的办法。
#
# 池化调度的规则（用户 2026-11-08 选定）：
#   - 每把 key **各自**算 5 次/分钟的滑动窗口（服务端也是按 key 计的）；
#   - 选"当前可用额度最多"的那把；并列时按轮转，避免总压在第一把上；
#   - 某把 key 被服务端 429 / 401 时**临时休息**（429 冷却 60 秒、401 冷却
#     10 分钟），期间不再选它，剩下的 key 继续干活；
#   - 全部都在冷却时，按最早恢复的那把等——报错交给调用方，不做静默重试。
#
# 为什么按 key 分别记窗口而不是全局：全局窗口在单 key 时是对的，多 key 时会把
# 不同 key 的额度混在一起算，等于多买了几把 key 却只用到一把的量。

_KEY_COOLDOWN_RATE_LIMIT = 60.0
_KEY_COOLDOWN_AUTH = 600.0


@dataclass
class KeyState:
    """一把 key 的运行时状态。

    ``secret`` 只留在内存，绝不写日志、绝不回显给前端——页面上只显示
    ``label`` 和 ``masked``（尾四位）。
    """

    key_id: str
    secret: str
    label: str = ""
    _times: deque = field(default_factory=deque)
    cooldown_until: float = 0.0
    last_error: str = ""
    requests_total: int = 0

    @property
    def masked(self) -> str:
        text = self.secret or ""
        if len(text) <= 4:
            return "****"
        return f"****{text[-4:]}"

    def used_in_window(self, now: float) -> int:
        while self._times and now - self._times[0] >= 60:
            self._times.popleft()
        return len(self._times)

    def remaining(self, now: float, limit: int | None = None) -> int:
        # 注意：``limit`` 不能写成 ``= LOCAL_REQUESTS_PER_MINUTE``——默认参数在
        # 函数定义时求值，会把当时的 5 冻结进去，set_requests_per_minute()
        # 改了全局也读不到。这里显式在调用时读。
        if limit is None:
            limit = LOCAL_REQUESTS_PER_MINUTE
        if now < self.cooldown_until:
            return -1  # 冷却中，视为不可用
        return max(0, limit - self.used_in_window(now))

    def reserve(self, now: float, limit: int | None = None) -> None:
        self._times.append(now)
        self.requests_total += 1


_key_pool: list[KeyState] = []
_key_cursor = 0


def _key_fingerprint(secret: str) -> str:
    """给 key 生成稳定短 id，用于日志与配置引用（不回显明文）。"""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:10]


def set_key_pool(entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    """设置 key 池。``entries`` 形如 ``[{"key": "...", "label": "主号"}]``。

    保留已有 key 的运行时状态（窗口计数、冷却），只增删差异——否则每次保存
    设置都会把刚建立的冷却和计数清空，等于给被限流的 key 立刻复活。
    """

    global _key_pool
    prepared: list[KeyState] = []
    existing = {state.key_id: state for state in _key_pool}
    seen: set[str] = set()
    for item in entries or []:
        secret = str((item or {}).get("key") or "").strip()
        if not secret:
            continue
        key_id = _key_fingerprint(secret)
        if key_id in seen:
            continue  # 同一把 key 填两次，只算一次
        seen.add(key_id)
        state = existing.get(key_id)
        if state is None:
            state = KeyState(key_id=key_id, secret=secret)
        state.label = str((item or {}).get("label") or "").strip()
        prepared.append(state)
    with _lock:
        _key_pool = prepared
    return key_pool_status()


def key_pool_status() -> list[dict[str, Any]]:
    """给诊断接口/页面用的池状态（不含明文）。"""

    now = time.monotonic()
    with _lock:
        snapshot = list(_key_pool)
    items = []
    for state in snapshot:
        remaining = state.remaining(now)
        items.append(
            {
                "id": state.key_id,
                "label": state.label,
                "masked": state.masked,
                "remaining": max(0, remaining),
                "cooldown_seconds": max(0, int(state.cooldown_until - now)),
                "requests_total": state.requests_total,
                "last_error": state.last_error,
            }
        )
    return items


def _pick_key(now: float) -> KeyState | None:
    """挑一把当前可用额度最多的 key（并列按轮转）。"""

    global _key_cursor
    with _lock:
        pool = list(_key_pool)
    if not pool:
        return None
    best: KeyState | None = None
    best_remaining = -1
    # 从游标开始扫一圈，确保并列时不会总压在第一把上。
    start = _key_cursor % len(pool)
    for offset in range(len(pool)):
        state = pool[(start + offset) % len(pool)]
        remaining = state.remaining(now)
        if remaining <= 0:
            continue
        if remaining > best_remaining:
            best = state
            best_remaining = remaining
            _key_cursor = (start + offset + 1) % len(pool)
    return best


def _note_key_failure(key_id: str, exc: Exception) -> None:
    """记一次失败：限流冷却 60 秒，鉴权失败冷却 10 分钟。"""

    now = time.monotonic()
    with _lock:
        for state in _key_pool:
            if state.key_id != key_id:
                continue
            if isinstance(exc, TaostatsAuthError):
                state.cooldown_until = now + _KEY_COOLDOWN_AUTH
            else:
                state.cooldown_until = now + _KEY_COOLDOWN_RATE_LIMIT
            state.last_error = f"{type(exc).__name__}: {exc}"[:160]
            break


def reset_key_pool_for_tests() -> None:
    """清空池（测试用）。"""

    global _key_pool, _key_cursor
    with _lock:
        _key_pool = []
        _key_cursor = 0


# -- 出站代理 -----------------------------------------------------------------
#
# 实测踩过的坑：这台机器直连 taostats.io 会超时（国内路由不稳），必须走本机
# 代理（Clash 之类）。但 Python 的 httpx 只认环境变量，不读 Windows 系统代理
# 设置——于是出现「浏览器打得开 taostats.io，服务查询一直超时」，查询静默
# 退回本地索引，页面还把它报成『没有可用的 API key』，完全误导排查方向。
#
# 这里按下述顺序决定出站路径，传输层失败时自动换下一条能用的：
#
#   1. WALLET_LINK_HTTP_PROXY 环境变量（"none"/"off" 强制直连）
#   2. Windows 系统代理（WinINET 注册表，ProxyEnable=1 时；Clash 写在这里）
#   3. HTTPS_PROXY / HTTP_PROXY / ALL_PROXY 环境变量
#   4. 直连
#
# 某条路失败后会被「拉黑」10 分钟，期间优先走其他路；全部失败再从头试。

_PROXY_OVERRIDE_ENV = "WALLET_LINK_HTTP_PROXY"
_PROXY_BAN_SECONDS = 600.0
_PROBE_TIMEOUT_SECONDS = 6.0

_proxy_state_lock = threading.Lock()
_proxy_state: dict[str, Any] = {
    "url": None,
    "source": "undecided",
    "banned": {},  # url(None 表示直连) -> 拉黑截止时刻（单调时钟）
}
_proxy_clients: dict[str | None, httpx.Client] = {}
_wininet_cache: tuple[float, str | None] | None = None


def _normalize_proxy_server(server: str) -> str | None:
    """把注册表里的 ProxyServer 规范成 httpx 认的 URL。

    支持两种形态：``127.0.0.1:7890``（全局代理）和
    ``http=127.0.0.1:7890;https=127.0.0.1:7890``（按协议指定）。
    """

    text = server.strip()
    if not text:
        return None
    if "=" in text:
        parts = {}
        for item in text.split(";"):
            if "=" in item:
                name, _, value = item.partition("=")
                parts[name.strip().lower()] = value.strip()
        text = parts.get("https") or parts.get("http") or ""
    text = text.strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    return text


def _wininet_proxy_url() -> str | None:
    """读 Windows 系统代理（Clash / v2rayN 等都会写 WinINET 设置）。"""

    global _wininet_cache

    if os.name != "nt":
        return None
    now = time.monotonic()
    if _wininet_cache and now - _wininet_cache[0] < 60:
        return _wininet_cache[1]
    value = None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_READ,
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                value = _normalize_proxy_server(str(server))
    except Exception:  # noqa: BLE001 - 注册表读不到就当没有系统代理
        value = None
    _wininet_cache = (now, value)
    return value


def _proxy_candidates() -> list[tuple[str | None, str]]:
    """按优先级列出出站路径候选：(代理 URL 或 None=直连, 来源说明)。"""

    override = os.getenv(_PROXY_OVERRIDE_ENV, "").strip()
    if override:
        if override.lower() in {"none", "off", "direct", "no"}:
            return [(None, "env-override")]
        if "://" not in override:
            override = "http://" + override
        return [(override, "env-override")]

    candidates: list[tuple[str | None, str]] = []
    wininet = _wininet_proxy_url()
    if wininet:
        candidates.append((wininet, "系统代理"))
    env_url = (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or os.getenv("ALL_PROXY")
        or os.getenv("all_proxy")
        or ""
    ).strip()
    if env_url:
        candidates.append((env_url, "环境变量"))
    candidates.append((None, "直连"))
    return candidates


def _decide_proxy(now: float | None = None) -> tuple[str | None, str]:
    """挑第一条没有被拉黑的候选路径。"""

    moment = time.monotonic() if now is None else now
    banned = _proxy_state.get("banned", {})
    candidates = _proxy_candidates()
    for url, source in candidates:
        until = banned.get(url)
        if until and moment < until:
            continue
        return url, source
    return candidates[0]


def _log_proxy_change(url: str | None, source: str, reason: str) -> None:
    target = url if url else "直连"
    print(f"[taostats] 出站请求走 {source}（{target}）：{reason}", file=sys.stderr)


def _active_proxy() -> tuple[str | None, str]:
    """当前生效的出站路径；第一次调用时决定，之后沿用直到失败换路。"""

    with _proxy_state_lock:
        if _proxy_state["source"] != "undecided":
            return _proxy_state["url"], _proxy_state["source"]
        url, source = _decide_proxy()
        _proxy_state["url"] = url
        _proxy_state["source"] = source
        _log_proxy_change(url, source, "初始选择")
        return url, source


def _note_proxy_failure(url: str | None, source: str, exc: Exception) -> None:
    """某条路传输层失败：拉黑它，换下一条（换了会打一行日志）。"""

    with _proxy_state_lock:
        banned = _proxy_state.setdefault("banned", {})
        banned[url] = time.monotonic() + _PROXY_BAN_SECONDS
        new_url, new_source = _decide_proxy()
        if (new_url, new_source) != (url, source):
            _proxy_state["url"] = new_url
            _proxy_state["source"] = new_source
            _log_proxy_change(new_url, new_source, f"原路径（{source}）失败：{exc}")


def _reset_proxy_decision() -> None:
    """重置出站路径决策（测试用）。"""

    with _proxy_state_lock:
        _proxy_state["url"] = None
        _proxy_state["source"] = "undecided"
        _proxy_state["banned"] = {}
    for client in _proxy_clients.values():
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    _proxy_clients.clear()


def _client_for(proxy_url: str | None) -> httpx.Client:
    """按代理 URL 缓存 Client。trust_env=False：代理选择完全由上面接管。"""

    client = _proxy_clients.get(proxy_url)
    if client is None:
        client = httpx.Client(proxy=proxy_url, trust_env=False)
        _proxy_clients[proxy_url] = client
    return client


def _http_get(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
    proxy_url: str | None,
):
    """单次 HTTP GET —— 全模块唯一的出站发送点。

    单独抽成函数是为了留一个稳定的测试接缝：换代理实现（Client / 直连 /
    将来的异步版本）时，测试只需要替换这一个函数。
    """

    return _client_for(proxy_url).get(
        url, params=params, headers=headers, timeout=timeout
    )


def outbound_proxy_status() -> dict[str, Any]:
    """当前出站路径（诊断接口用）。"""

    url, source = _active_proxy()
    return {"url": url, "source": source}


def probe_outbound(timeout: float = _PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """逐条候选路径探测 taostats.io 的可达性，返回每条的结果。

    只做连接层探测（HEAD 请求），不带 API key、不占查询额度。
    """

    results: list[dict[str, Any]] = []
    for url, source in _proxy_candidates():
        item: dict[str, Any] = {"proxy": url, "source": source}
        started = time.monotonic()
        try:
            response = _client_for(url).head("https://taostats.io/", timeout=timeout)
            item["ok"] = response.status_code < 500
            item["status_code"] = response.status_code
        except Exception as exc:  # noqa: BLE001 - 探测失败本身就是结果
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"[:120]
        item["seconds"] = round(time.monotonic() - started, 1)
        results.append(item)
    return {"results": results}


def configure_api_key(api_key: str) -> None:
    """Keep a user-provided API key in process memory only."""

    global _key_override, _key_override_active, _validator_cache, _exchange_cache
    with _lock:
        _key_override = api_key.strip()
        _key_override_active = True
        _cache.clear()
        _stake_cache.clear()
        _validator_cache = None
        _exchange_cache = None


def clear_api_key() -> None:
    """Clear keys for this process and do not fall back to the environment."""

    configure_api_key("")


def _configured_api_key() -> str:
    with _lock:
        if _key_override_active:
            return _key_override or ""
    return os.getenv("TAOSTATS_API_KEY", "").strip()


def get_active_validator_addresses() -> tuple[set[str], bool]:
    """Return current Taostats validator hotkeys/coldkeys.

    The boolean indicates whether the result came from the process cache. The
    endpoint exposes explicit validator records; addresses are only filtered
    when they are present in that response.
    """

    global _validator_cache
    now = time.monotonic()
    with _lock:
        if _validator_cache and now - _validator_cache[0] < CACHE_TTL_SECONDS:
            return set(_validator_cache[1]), True

    key_id, api_key = _acquire_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key，无法自动读取验证者名单。"
        )
    try:
        response = _request_get(VALIDATORS_ACTIVE_URL, api_key=api_key)
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 验证者名单请求失败：{exc}", request_made=True
        ) from exc
    if response.status_code in {401, 403}:
        _record_key_failure(
            key_id, TaostatsAuthError("key 无效", status_code=response.status_code)
        )
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
        _record_key_failure(
            key_id, TaostatsRateLimitError("429", status_code=response.status_code)
        )
        raise TaostatsRateLimitError(
            "Taostats 返回限流，请稍后重试。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise TaostatsUnavailableError(
            f"Taostats 验证者名单返回 HTTP {response.status_code}。",
            request_made=True,
            status_code=response.status_code,
        )

    try:
        payload = response.json()
        rows = payload["data"]
        if not isinstance(rows, list):
            raise TypeError("data is not a list")
        addresses: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("address", "hotkey", "coldkey"):
                value = row.get(field)
                if isinstance(value, dict):
                    value = value.get("ss58")
                if isinstance(value, str) and value.strip():
                    addresses.add(value.strip())
        if not addresses and rows:
            raise ValueError("validator rows contain no SS58 addresses")
    except (ValueError, KeyError, TypeError) as exc:
        raise TaostatsUnavailableError("Taostats 验证者名单响应格式无法识别。") from exc

    with _lock:
        _validator_cache = (time.monotonic(), frozenset(addresses))
    return addresses, False


def get_exchange_addresses() -> tuple[list[dict[str, str]], bool]:
    """Return the provider's current exchange address directory.

    The endpoint is a fixed directory rather than transfer history. The
    normalized records deliberately keep only fields needed by the local
    address library so provider response changes cannot leak into the UI.
    The boolean indicates whether the result came from the process cache.
    """

    global _exchange_cache
    now = time.monotonic()
    with _lock:
        if _exchange_cache and now - _exchange_cache[0] < CACHE_TTL_SECONDS:
            return [dict(row) for row in _exchange_cache[1]], True

    key_id, api_key = _acquire_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key，无法同步交易所地址。"
        )
    try:
        response = _request_get(EXCHANGE_ADDRESSES_URL, api_key=api_key)
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 交易所地址请求失败：{exc}", request_made=True
        ) from exc
    if response.status_code in {401, 403}:
        _record_key_failure(
            key_id, TaostatsAuthError("key 无效", status_code=response.status_code)
        )
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
        _record_key_failure(
            key_id, TaostatsRateLimitError("429", status_code=response.status_code)
        )
        raise TaostatsRateLimitError(
            "Taostats 返回限流，请稍后重试。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise TaostatsUnavailableError(
            f"Taostats 交易所地址返回 HTTP {response.status_code}。",
            request_made=True,
            status_code=response.status_code,
        )

    try:
        payload = response.json()
        rows = payload["data"]
        if not isinstance(rows, list):
            raise TypeError("data is not a list")
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            address = row.get("coldkey") or row.get("address")
            if isinstance(address, dict):
                address = address.get("ss58") or address.get("address")
            if not isinstance(address, str) or not address.strip():
                continue
            address = address.strip()
            if address in seen:
                continue
            seen.add(address)
            name = row.get("name")
            icon = row.get("icon")
            result.append(
                {
                    "address": address,
                    "name": str(name).strip() if name is not None else "",
                    "icon": str(icon).strip() if icon is not None else "",
                }
            )
    except (ValueError, KeyError, TypeError) as exc:
        raise TaostatsUnavailableError("Taostats 交易所地址响应格式无法识别。") from exc

    with _lock:
        _exchange_cache = (time.monotonic(), tuple(dict(row) for row in result))
    return result, False


TRANSIENT_RETRY_ATTEMPTS = 3
TRANSIENT_RETRY_BACKOFF_SECONDS = 1.2


def _request_get(
    url: str,
    *,
    api_key: str,
    params: dict[str, Any] | None = None,
    timeout: float = 20,
):
    """取一次 GET 响应，传输层瞬时错误自动重试。

    Taostats 的 TLS 握手偶发 `UNEXPECTED_EOF_WHILE_READING`（服务端根本没处理
    这个请求），只发一次就被判成「服务不可用」太脆。这里只重试传输层异常；
    HTTP 状态码（401/429/5xx）原样交给调用方判定，避免把限流当成网络抖动重试。
    重试不额外占用本地限速额度——失败的请求没有真正消耗上游配额。

    重试时若传输层仍失败，会自动换一条出站路径（代理 ↔ 直连）再试，
    避免本机代理挂了或直连被墙时整条链路长时间不可用。
    """

    last_error: httpx.HTTPError | None = None
    for attempt in range(TRANSIENT_RETRY_ATTEMPTS):
        proxy_url, source = _active_proxy()
        try:
            return _http_get(
                url,
                params=params,
                headers={"Authorization": api_key},
                timeout=timeout,
                proxy_url=proxy_url,
            )
        except httpx.TransportError as exc:  # 覆盖 SSL EOF / 连接重置 / 读超时
            last_error = exc
            _note_proxy_failure(proxy_url, source, exc)
            if attempt + 1 < TRANSIENT_RETRY_ATTEMPTS:
                time.sleep(TRANSIENT_RETRY_BACKOFF_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error


def _acquire_key() -> tuple[str, str]:
    """取一把可用 key，返回 ``(key_id, secret)``。

    走 key 池时按「各自剩余额度最多 + 轮转」挑；池为空则回退到单 key 的
    老逻辑（`_configured_api_key` + 全局窗口），保证单 key 用法完全不变。

    全部 key 都在冷却时直接报限流——用户已明确选择"不排队、直接提示"，
    静默等待会让页面看起来像卡死。
    """

    now = time.monotonic()
    with _lock:
        has_pool = bool(_key_pool)
    if not has_pool:
        # 单 key 老路径：保持原来的窗口与文案，别改坏现有行为。
        with _lock:
            while _request_times and now - _request_times[0] >= 60:
                _request_times.popleft()
            if len(_request_times) >= LOCAL_REQUESTS_PER_MINUTE:
                raise TaostatsRateLimitError(
                    f"Taostats 本地保护限速为 {LOCAL_REQUESTS_PER_MINUTE} 次/分钟，"
                    "请稍后重试。额度已升级的话，可在「Taostats API key」面板改"
                    "「每分钟额度」。"
                )
            _request_times.append(now)
        return "", _configured_api_key()

    state = _pick_key(now)
    if state is None:
        # 全都在冷却：把最早恢复的那把报给用户，比一句"请稍后"有用。
        with _lock:
            pool = list(_key_pool)
        soonest = min(pool, key=lambda s: s.cooldown_until) if pool else None
        wait = max(1, int(soonest.cooldown_until - now)) if soonest else 60
        raise TaostatsRateLimitError(
            f"所有 Taostats API key 都在冷却中，最早 {wait} 秒后恢复，请稍后重试。"
        )
    state.reserve(now)
    return state.key_id, state.secret


def _reserve_request() -> None:
    """旧接口：只抢额度不要 key（保留给不需要 key 的调用方与测试）。"""

    _acquire_key()


def _record_key_failure(key_id: str, exc: Exception) -> None:
    if key_id:
        _note_key_failure(key_id, exc)


def get_transfer_page(address: str, page: int = 1) -> TransferPage:
    return get_transfer_page_query(address=address, page=page)


def get_transfer_page_query(
    *,
    address: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    page: int = 1,
    limit: int = 200,
) -> TransferPage:
    """按地址 / 精确发送方 / 精确接收方取一页转账。

    `from_address` + `to_address` 同时给，就是"这两个地址之间的直接转账"，
    服务端精确匹配，不受分页窗口的干扰——两地址关联检索靠它拿明细。
    """
    if not (address or from_address or to_address):
        raise ValueError("address、from_address、to_address 至少要给一个")
    key = (address or "", from_address or "", to_address or "", page, limit)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            value = cached[1]
            return TransferPage(
                value.rows,
                value.next_page,
                truncated_by_window=value.truncated_by_window,
                from_cache=True,
                total_items=value.total_items,
                total_pages=value.total_pages,
                per_page=value.per_page,
            )
        if cached:
            _cache.pop(key, None)

    key_id, api_key = _acquire_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key。请在页面中的 Taostats 设置里填写并保存。"
        )

    params: dict[str, Any] = {"network": "finney", "page": page, "limit": limit}
    if address:
        params["address"] = address
    if from_address:
        params["from"] = from_address
    if to_address:
        params["to"] = to_address

    try:
        response = _request_get(API_URL, api_key=api_key, params=params)
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 请求失败：{exc}", request_made=True
        ) from exc

    if response.status_code in {401, 403}:
        _record_key_failure(
            key_id, TaostatsAuthError("key 无效", status_code=response.status_code)
        )
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
        _record_key_failure(
            key_id, TaostatsRateLimitError("429", status_code=response.status_code)
        )
        raise TaostatsRateLimitError(
            "Taostats 返回限流，请稍后重试。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise TaostatsUnavailableError(
            f"Taostats API 返回 HTTP {response.status_code}。",
            request_made=True,
            status_code=response.status_code,
        )

    try:
        payload = response.json()
        rows = payload["data"]
        pagination = payload["pagination"]
        if not isinstance(rows, list):
            raise TypeError("data is not a list")
        if not isinstance(pagination, dict):
            raise TypeError("pagination is not an object")
        next_page = pagination.get("next_page")
        next_page = int(next_page) if next_page is not None else None
        if next_page is not None and (next_page <= page or next_page > 5000):
            raise ValueError("pagination.next_page is out of range")
        per_page = int(pagination["per_page"])
        total_pages = int(pagination["total_pages"])
        total_items = int(pagination["total_items"])
        if per_page < 1 or total_pages < 0 or total_items < 0:
            raise ValueError("pagination values are out of range")
        truncated_by_window = total_items > per_page * total_pages
        result = TransferPage(
            rows=rows,
            next_page=next_page,
            truncated_by_window=truncated_by_window,
            total_items=total_items,
            total_pages=total_pages,
            per_page=per_page,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TaostatsUnavailableError("Taostats 返回了无法识别的响应格式。") from exc

    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


def get_stake_transfer_page(address: str, page: int = 1) -> TransferPage:
    """Fetch dTAO stake-transfer legs that involve one coldkey.

    The documented endpoint is /v1/subnets/stake-events with ``coldkey`` and
    ``is_transfer`` filters. Pagination objects differ across Taostats endpoint
    families, so only fields actually present in the response are consumed.
    """

    key = (address, page)
    now = time.monotonic()
    with _lock:
        cached = _stake_cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            value = cached[1]
            return TransferPage(
                value.rows,
                value.next_page,
                truncated_by_window=value.truncated_by_window,
                from_cache=True,
            )
        if cached:
            _stake_cache.pop(key, None)

    key_id, api_key = _acquire_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key。请在页面中的 Taostats 设置里填写并保存。"
        )
    try:
        response = _request_get(
            STAKE_EVENTS_URL,
            api_key=api_key,
            params={
                "coldkey": address,
                "is_transfer": "true",
                "page": page,
                "limit": 200,
            },
        )
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 请求失败：{exc}", request_made=True
        ) from exc

    if response.status_code in {401, 403}:
        _record_key_failure(
            key_id, TaostatsAuthError("key 无效", status_code=response.status_code)
        )
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
        _record_key_failure(
            key_id, TaostatsRateLimitError("429", status_code=response.status_code)
        )
        raise TaostatsRateLimitError(
            "Taostats 返回限流，请稍后重试。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise TaostatsUnavailableError(
            f"Taostats API 返回 HTTP {response.status_code}。",
            request_made=True,
            status_code=response.status_code,
        )

    try:
        payload = response.json()
        rows = payload["data"]
        pagination = payload.get("pagination", {})
        if not isinstance(rows, list):
            raise TypeError("data is not a list")
        if not isinstance(pagination, dict):
            raise TypeError("pagination is not an object")
        next_page = pagination.get("next_page")
        next_page = int(next_page) if next_page is not None else None
        if next_page is not None and (next_page <= page or next_page > 5000):
            raise ValueError("pagination.next_page is out of range")
        truncated = False
        per_page = pagination.get("per_page")
        total_items = pagination.get("total_items")
        total_pages = pagination.get("total_pages")
        if per_page is not None and total_items is not None and total_pages is not None:
            truncated = int(total_items) > int(per_page) * int(total_pages)
        result = TransferPage(
            rows=rows,
            next_page=next_page,
            truncated_by_window=truncated,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TaostatsUnavailableError("Taostats 返回了无法识别的响应格式。") from exc

    with _lock:
        _stake_cache[key] = (time.monotonic(), result)
    return result


def get_account_profile(address: str) -> AccountProfile:
    """取账户画像（注册日期 + 余额构成）。

    只读一次 ``/api/account/latest/v1``，走本地缓存；未配置 key、限流、
    网络失败都按「拿不到」处理（抛 TaostatsError 子类），由调用方降级为
    「未获取」，不影响主查询。
    """

    now = time.monotonic()
    with _lock:
        cached = _profile_cache.get(address)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return AccountProfile(**{**cached[1].__dict__, "from_cache": True})
        if cached:
            _profile_cache.pop(address, None)

    key_id, api_key = _acquire_key()
    if not api_key:
        raise TaostatsAuthError("未配置 Taostats API key。")
    try:
        response = _request_get(
            ACCOUNT_LATEST_URL,
            api_key=api_key,
            params={"network": "finney", "address": address},
        )
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 账户资料请求失败：{exc}", request_made=True
        ) from exc

    if response.status_code in {401, 403}:
        _record_key_failure(
            key_id, TaostatsAuthError("key 无效", status_code=response.status_code)
        )
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
        _record_key_failure(
            key_id, TaostatsRateLimitError("429", status_code=response.status_code)
        )
        raise TaostatsRateLimitError(
            "Taostats 返回限流，请稍后重试。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise TaostatsUnavailableError(
            f"Taostats 账户资料接口返回 HTTP {response.status_code}。",
            request_made=True,
            status_code=response.status_code,
        )

    try:
        payload = response.json()
        rows = payload["data"]
        if not isinstance(rows, list) or not rows:
            raise TypeError("data is empty")
        row = rows[0]
        if not isinstance(row, dict):
            raise TypeError("data[0] is not an object")
        profile = AccountProfile(
            created_on_date=_text_or_none(row.get("created_on_date")),
            created_on_network=_text_or_none(row.get("created_on_network")),
            balance_free_tao=_rao_to_tao_text(row.get("balance_free")),
            balance_staked_tao=_rao_to_tao_text(row.get("balance_staked")),
            balance_staked_alpha_as_tao=_rao_to_tao_text(
                row.get("balance_staked_alpha_as_tao")
            ),
            balance_reserved_tao=_rao_to_tao_text(row.get("balance_reserved")),
            balance_total_tao=_rao_to_tao_text(row.get("balance_total")),
            rank=_int_or_none(row.get("rank")),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TaostatsUnavailableError("Taostats 账户资料响应格式无法识别。") from exc

    with _lock:
        _profile_cache[address] = (time.monotonic(), profile)
    return profile


def get_transfer_total(address: str) -> tuple[int, bool]:
    """只取「这个地址共有多少笔转账」（分页的 total_items），不翻页。

    返回 ``(总数, 是否命中缓存)``。limit 取 1 是为了让这一页只带 1 条数据，
    但服务端仍会给出真实的 total_items，一次请求就够。
    """

    page = get_transfer_page_query(address=address, page=1, limit=1)
    return page.total_items, page.from_cache


def get_stake_event_total(address: str) -> tuple[int, bool]:
    """取「这个地址有多少次子网质押操作」——质押与解质押都算一次。

    走 ``/v1/subnets/stake-events`` 的 ``action=all``：实测 ``stake`` + ``unstake``
    的 ``total_items`` 恰好等于 ``all``（例如 475 + 431 = 906），所以两件事都被
    计入，且不会混入 ``is_transfer=true`` 的质押转移。

    返回 ``(总数, 是否命中缓存)``。``limit=1`` 只为拿分页里的真实总量，不翻页。
    """

    key = address
    now = time.monotonic()
    with _lock:
        cached = _stake_total_cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], True
        if cached:
            _stake_total_cache.pop(key, None)

    key_id, api_key = _acquire_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key。请在页面中的 Taostats 设置里填写并保存。"
        )
    try:
        response = _request_get(
            STAKE_EVENTS_URL,
            api_key=api_key,
            params={"coldkey": address, "action": "all", "page": 1, "limit": 1},
        )
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 请求失败：{exc}", request_made=True
        ) from exc

    if response.status_code in {401, 403}:
        _record_key_failure(
            key_id, TaostatsAuthError("key 无效", status_code=response.status_code)
        )
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
        _record_key_failure(
            key_id, TaostatsRateLimitError("429", status_code=response.status_code)
        )
        raise TaostatsRateLimitError(
            "Taostats 返回限流，请稍后重试。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise TaostatsUnavailableError(
            f"Taostats API 返回 HTTP {response.status_code}。",
            request_made=True,
            status_code=response.status_code,
        )

    try:
        pagination = response.json().get("pagination") or {}
        total = int(pagination["total_items"])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise TaostatsUnavailableError("Taostats 质押事件响应格式无法识别。") from exc

    with _lock:
        _stake_total_cache[key] = (time.monotonic(), total)
    return total, False



def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rao_to_tao_text(value: Any) -> str | None:
    """Rao（整数，1 TAO = 1e9 rao）转成 TAO 十进制字符串；空值返回 None。"""

    if value is None or value == "":
        return None
    try:
        rao = int(value)
    except (TypeError, ValueError):
        return None
    return f"{Decimal(rao) / Decimal(10**9):.9f}".rstrip("0").rstrip(".") or "0"


def reset_client_state_for_tests() -> None:
    global _key_override, _key_override_active, _validator_cache, _exchange_cache
    with _lock:
        _request_times.clear()
        _cache.clear()
        _stake_cache.clear()
        _profile_cache.clear()
        _stake_total_cache.clear()
        _validator_cache = None
        _exchange_cache = None
        _key_override = None
        _key_override_active = False
