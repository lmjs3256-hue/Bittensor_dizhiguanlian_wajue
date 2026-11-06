"""Small, rate-limited client for Taostats indexed transfer history."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx


API_URL = "https://api.taostats.io/v1/transfers"
VALIDATORS_ACTIVE_URL = "https://api.taostats.io/v1/validators/active"
EXCHANGE_ADDRESSES_URL = "https://api.taostats.io/v1/exchange-addresses"
STAKE_EVENTS_URL = "https://api.taostats.io/v1/subnets/stake-events"
ACCOUNT_LATEST_URL = "https://api.taostats.io/api/account/latest/v1"
LOCAL_REQUESTS_PER_MINUTE = 5
CACHE_TTL_SECONDS = 300


def is_configured() -> bool:
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
_key_override: str | None = None
_key_override_active = False


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

    api_key = _configured_api_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key，无法自动读取验证者名单。"
        )
    _reserve_request()
    try:
        response = _request_get(VALIDATORS_ACTIVE_URL, api_key=api_key)
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 验证者名单请求失败：{exc}", request_made=True
        ) from exc
    if response.status_code in {401, 403}:
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
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

    api_key = _configured_api_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key，无法同步交易所地址。"
        )
    _reserve_request()
    try:
        response = _request_get(EXCHANGE_ADDRESSES_URL, api_key=api_key)
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 交易所地址请求失败：{exc}", request_made=True
        ) from exc
    if response.status_code in {401, 403}:
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
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
    """

    last_error: httpx.HTTPError | None = None
    for attempt in range(TRANSIENT_RETRY_ATTEMPTS):
        try:
            return httpx.get(
                url,
                params=params,
                headers={"Authorization": api_key},
                timeout=timeout,
            )
        except httpx.TransportError as exc:  # 覆盖 SSL EOF / 连接重置 / 读超时
            last_error = exc
            if attempt + 1 < TRANSIENT_RETRY_ATTEMPTS:
                time.sleep(TRANSIENT_RETRY_BACKOFF_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error


def _reserve_request() -> None:
    now = time.monotonic()
    with _lock:
        while _request_times and now - _request_times[0] >= 60:
            _request_times.popleft()
        if len(_request_times) >= LOCAL_REQUESTS_PER_MINUTE:
            raise TaostatsRateLimitError(
                "Taostats 本地保护限速为 5 次/分钟，请稍后重试。"
            )
        _request_times.append(now)


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

    api_key = _configured_api_key()
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

    _reserve_request()
    try:
        response = _request_get(API_URL, api_key=api_key, params=params)
    except httpx.HTTPError as exc:
        raise TaostatsUnavailableError(
            f"Taostats 请求失败：{exc}", request_made=True
        ) from exc

    if response.status_code in {401, 403}:
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
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

    api_key = _configured_api_key()
    if not api_key:
        raise TaostatsAuthError(
            "未配置 Taostats API key。请在页面中的 Taostats 设置里填写并保存。"
        )

    _reserve_request()
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
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
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

    api_key = _configured_api_key()
    if not api_key:
        raise TaostatsAuthError("未配置 Taostats API key。")

    _reserve_request()
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
        raise TaostatsAuthError(
            "Taostats API key 无效或没有访问权限。",
            request_made=True,
            status_code=response.status_code,
        )
    if response.status_code == 429:
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
        _validator_cache = None
        _exchange_cache = None
        _key_override = None
        _key_override_active = False
