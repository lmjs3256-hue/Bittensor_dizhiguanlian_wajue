"""Small, rate-limited client for Taostats indexed transfer history."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx


API_URL = "https://api.taostats.io/v1/transfers"
VALIDATORS_ACTIVE_URL = "https://api.taostats.io/v1/validators/active"
EXCHANGE_ADDRESSES_URL = "https://api.taostats.io/v1/exchange-addresses"
STAKE_EVENTS_URL = "https://api.taostats.io/v1/subnets/stake-events"
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


_lock = threading.Lock()
_request_times: deque[float] = deque()
_cache: dict[tuple[str, int], tuple[float, TransferPage]] = {}
_stake_cache: dict[tuple[str, int], tuple[float, TransferPage]] = {}
_validator_cache: tuple[float, frozenset[str]] | None = None
_exchange_cache: tuple[float, tuple[dict[str, str], ...]] | None = None
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
        response = httpx.get(
            VALIDATORS_ACTIVE_URL,
            headers={"Authorization": api_key},
            timeout=20,
        )
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
        response = httpx.get(
            EXCHANGE_ADDRESSES_URL,
            headers={"Authorization": api_key},
            timeout=20,
        )
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
        response = httpx.get(
            API_URL,
            params=params,
            headers={"Authorization": api_key},
            timeout=20,
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
        response = httpx.get(
            STAKE_EVENTS_URL,
            params={
                "coldkey": address,
                "is_transfer": "true",
                "page": page,
                "limit": 200,
            },
            headers={"Authorization": api_key},
            timeout=20,
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


def reset_client_state_for_tests() -> None:
    global _key_override, _key_override_active, _validator_cache, _exchange_cache
    with _lock:
        _request_times.clear()
        _cache.clear()
        _stake_cache.clear()
        _validator_cache = None
        _exchange_cache = None
        _key_override = None
        _key_override_active = False
