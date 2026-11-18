"""FastAPI application for Bittensor address links."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from . import access as access_mod
from . import diagnostics
from . import license as license_mod
from . import netinfo
from . import remote_launch
from . import settings_store
from . import taostats
from .config import ROOT_DIR, database_path, settings
from .event_monitor.default_watch import seed_default_subnet_watch
from .event_monitor.runtime import EVENT_MONITOR, settings_from_env as event_monitor_settings
from .event_monitor.address_monitor import address_event_record, derived_stake_transfer_buy_raw
from .db import Database, install_seed_books
from .inputs import InputError
from .jobs import RelationJobManager
from .relation_config import DEFAULT_RELATION_DEPTH, MAX_RELATION_DEPTH
from .validation import is_valid_ss58
from .service import LinkService, SUBNET_OWNER_SOURCE, ScanBusyError
from .shared_chain import (
    configure_shared_chain,
    reset_shared_chain,
    shared_chain_stats,
    shutdown_shared_chain,
)
from .taostats import (
    TaostatsAuthError,
    TaostatsRateLimitError,
    TaostatsUnavailableError,
    clear_api_key as clear_taostats_api_key,
    configure_api_key as configure_taostats_api_key,
    is_configured as taostats_is_configured,
)
from .version_info import read_version_info
from .watch import SWEEP_TICK_SECONDS, WatchService


ROOT = Path(__file__).resolve().parent
CONFIG = settings()
DATABASE = Database(database_path())
EVENT_MONITOR_ENABLED = True
EVENT_MONITOR_STATIC = ROOT / "static" / "event-monitor"
_EVENT_PROXY_CLIENT: Any | None = None
VERSION_INFO = read_version_info()
# 预置地址库只在真实启动时导入；测试里换成临时库后关掉，避免污染用例数据。
SEED_BOOKS_ENABLED = True
# 官方交易所名单默认导入：每次启动在后台静默同步一次（有 key 才发请求）。
AUTO_EXCHANGE_SYNC_ENABLED = True
# 子网项目方地址：启动时检查一次，此后每小时检查是否已满 7 天。
AUTO_SUBNET_OWNER_SYNC_ENABLED = True
SUBNET_OWNER_SYNC_INTERVAL_SECONDS = 7 * 24 * 60 * 60
SUBNET_OWNER_SYNC_CHECK_SECONDS = 60 * 60
# 内置固定名单（人工确认的交易所/验证者/公共地址）：每次启动幂等写入地址库。
BUILTIN_ADDRESSES_ENABLED = True
# 本次启动内置名单的写入结果，供 /api/diagnose 显示「新版本真的跑起来了吗」。
BUILTIN_STARTUP_RESULT: dict[str, Any] | None = None
DEFAULT_SUBNET_WATCH_RESULT: dict[str, Any] | None = None
EVENT_WATCH_AUTO_SYNC_RESULT: dict[str, Any] | None = None
EVENT_WATCH_RECONCILE_RESULT: dict[str, Any] | None = None
SUBNET_LIFECYCLE_RESULT: dict[str, Any] | None = None
_SUBNET_LIFECYCLE_LOCK = threading.Lock()
_SUBNET_LIFECYCLE_QUEUE: list[dict[str, Any]] = []
_SUBNET_LIFECYCLE_RUNNING = False
_SUBNET_LIFECYCLE_WATCH_STATE: dict[int, bool] = {}
# 地址库自动入库的调度间隔：每 30 秒拿本地索引的新块撞一次地址库。
# 不消耗任何 Taostats 额度（数据来自 RPC 直采的 transfers 表）。
# 与 watch.py 的 SWEEP_TICK_SECONDS 同源：前端「距下一轮」倒计时依赖这个值，
# 两处写死同一个数字迟早会漂移。
WATCH_TICK_SECONDS = SWEEP_TICK_SECONDS
# 本地索引定期自动扫描：不消耗 Taostats 额度，走链上 RPC。
#
# 以前的配置是「每 300 秒扫 16 块」，而 Bittensor 每 300 秒新增长约 25 块
# （12 秒/块）——每轮净落后 9 块，跑得越久欠账越多。实测库里已经落后链头
# 7,541 块（约 25 小时），本地索引因此只剩 2.5 天的碎片数据，764 个可巡检
# 地址里只有 54 个有记录，根本撑不起「改本地查询」。
#
# 实测采集器能力约 0.32 块/秒（链速仅 0.083 块/秒，余量 4 倍），所以按
# 「每轮要覆盖这期间链增长的量 + 一小段历史欠账」来取块数即可追上。
LOCAL_AUTO_SCAN_ENABLED = True
# 180 秒一轮：链在 180 秒内约长 15 块，仍按 75 块/轮扫（5 倍余量），
# 既跟得住又能继续回补历史欠账。调快的原因见 sweep_local_index 的注释：
# 本地索引是自动入库的唯一输入，它多滞后一分钟，库里地址的新往来就晚一分钟
# 入库。
LOCAL_AUTO_SCAN_SECONDS = 180
# 链在 300 秒内约长 25 块；取 75 块（≈3 倍余量）既能跟住，又能以约
# 50 块/轮的速度回补历史欠账（7,541 块 ≈ 2.5 小时追平）。
LOCAL_AUTO_SCAN_BLOCKS = 75
# 单轮扫描的时间上限：这些 RPC 是本地节点直连，不算 Taostats 额度，
# 但也不能让一次扫描把调度器阻塞太久。
LOCAL_AUTO_SCAN_MAX_SECONDS = 240


async def _auto_sync_exchanges() -> None:
    """启动后台同步官方交易所名单，失败只记状态，不打扰启动。"""

    try:
        await asyncio.to_thread(SERVICE.auto_sync_exchange_addresses)
    except Exception as exc:  # noqa: BLE001 - 后台同步失败不影响服务
        print(f"[exchange-sync] 官方交易所名单同步失败：{exc}", file=sys.stderr)


def _seed_default_event_subnet_watch() -> dict[str, Any] | None:
    """把现有 SN1-SN128 地址库一次性加入事件监控。"""

    global DEFAULT_SUBNET_WATCH_RESULT
    if not EVENT_MONITOR.running or EVENT_MONITOR.context is None:
        return None
    try:
        DEFAULT_SUBNET_WATCH_RESULT = seed_default_subnet_watch(
            EVENT_MONITOR.context.store,
            SERVICE.library(),
        )
        if DEFAULT_SUBNET_WATCH_RESULT.get("new_books"):
            print(
                f"[event-monitor] 默认监控新增 {len(DEFAULT_SUBNET_WATCH_RESULT['new_books'])} 个 "
                f"SN 地址库、{DEFAULT_SUBNET_WATCH_RESULT.get('added', 0)} 个地址",
                file=sys.stderr,
            )
        return DEFAULT_SUBNET_WATCH_RESULT
    except Exception as exc:  # noqa: BLE001 - 默认监控失败不影响主服务
        DEFAULT_SUBNET_WATCH_RESULT = {"seeded": False, "error": str(exc)}
        print(f"[event-monitor] SN 默认监控地址库写入失败：{exc}", file=sys.stderr)
        return DEFAULT_SUBNET_WATCH_RESULT


def _reconcile_event_monitor_watch() -> dict[str, Any] | None:
    """移除已解散地址库的旧监听地址，并把迁移地址更新到当前库名。"""

    global EVENT_WATCH_RECONCILE_RESULT
    if not EVENT_MONITOR.running or EVENT_MONITOR.context is None:
        return None
    try:
        shared_items = SERVICE.library()
        shared_by_address = {
            str(item.get("address") or "").strip(): item
            for item in shared_items
            if str(item.get("address") or "").strip()
        }
        current_books = {
            str(item.get("book") or "").strip()
            for item in shared_items
            if str(item.get("book") or "").strip()
        }
        store = EVENT_MONITOR.context.store
        watched = store.list_watched_addresses("finney")
        removed = 0
        updated = 0
        unchanged = 0
        removed_books: dict[str, int] = {}
        updated_books: dict[str, int] = {}
        for row in watched:
            watched_book = str(row.get("library_book") or "").strip()
            if not watched_book:
                continue
            address = str(row.get("address") or "").strip()
            current = shared_by_address.get(address)
            current_book = str((current or {}).get("book") or "").strip()
            if current is not None and current_book in current_books:
                label = str(current.get("label") or current_book)[:120]
                role = "project_owner" if current.get("source") == SUBNET_OWNER_SOURCE else ""
                if (
                    current_book != watched_book
                    or label != str(row.get("label") or "")
                    or role != str(row.get("address_role") or "")
                ):
                    changed = store.update_watched_address(
                        int(row["address_id"]),
                        label=label,
                        library_book=current_book,
                        address_role=role,
                    )
                    if changed is not None:
                        updated += 1
                        key = (
                            f"{watched_book} → {current_book}"
                            if watched_book != current_book else current_book
                        )
                        updated_books[key] = updated_books.get(key, 0) + 1
                else:
                    unchanged += 1
                continue
            if store.delete_watched_address(int(row["address_id"])):
                removed += 1
                removed_books[watched_book] = removed_books.get(watched_book, 0) + 1
        EVENT_WATCH_RECONCILE_RESULT = {
            "removed": removed,
            "updated": updated,
            "unchanged": unchanged,
            "checked": len(watched),
            "removed_books": removed_books,
            "updated_books": updated_books,
        }
        if removed or updated:
            print(
                f"[event-monitor] 地址库对账：移除旧监听 {removed} 个、更新归属 {updated} 个",
                file=sys.stderr,
            )
        return EVENT_WATCH_RECONCILE_RESULT
    except Exception as exc:  # noqa: BLE001 - 对账失败不应影响主服务
        EVENT_WATCH_RECONCILE_RESULT = {"error": str(exc)}
        print(f"[event-monitor] 地址库对账失败：{exc}", file=sys.stderr)
        return EVENT_WATCH_RECONCILE_RESULT


def _sync_event_monitor_library_state() -> dict[str, Any] | None:
    """Seed new SN books, then reconcile stale/moved addresses with detail."""

    seed = _seed_default_event_subnet_watch()
    reconcile = _reconcile_event_monitor_watch()
    if reconcile is None:
        return None
    result = {
        **reconcile,
        "added": int((seed or {}).get("added") or 0),
        "added_books": dict((seed or {}).get("new_book_counts") or {}),
        "updated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
    }
    global EVENT_WATCH_RECONCILE_RESULT
    EVENT_WATCH_RECONCILE_RESULT = result
    return result


def _watched_subnet_netuids() -> set[int]:
    if not EVENT_MONITOR.running or EVENT_MONITOR.context is None:
        return set()
    result: set[int] = set()
    for item in EVENT_MONITOR.context.store.list_watched_addresses("finney"):
        match = re.match(r"^SN(\d+) ", str(item.get("library_book") or ""))
        if match:
            result.add(int(match.group(1)))
    return result


def _subnet_has_watched_addresses(netuid: int) -> bool:
    return int(netuid) in _watched_subnet_netuids()


def _ensure_subnet_owner_watched(netuid: int, *, source_items: list[dict[str, Any]] | None = None) -> int:
    if not EVENT_MONITOR.running or EVENT_MONITOR.context is None:
        return 0
    prefix = f"SN{int(netuid)} "
    owners = [
        item for item in (source_items if source_items is not None else SERVICE.library())
        if str(item.get("book") or "").startswith(prefix)
        and item.get("source") == SUBNET_OWNER_SOURCE
    ]
    store = EVENT_MONITOR.context.store
    added = 0
    for item in owners:
        address = str(item.get("address") or "").strip()
        book = str(item.get("book") or "").strip()
        if not address or not book:
            continue
        try:
            store.add_watched_address(
                "finney",
                address,
                str(item.get("label") or f"SN{int(netuid)} 项目方")[:120],
                0,
                library_book=book,
                address_role="project_owner",
                clear_exclusion=True,
            )
            added += 1
        except Exception:
            continue
    return added


def _process_subnet_lifecycle_event(item: dict[str, Any]) -> dict[str, Any]:
    global SUBNET_LIFECYCLE_RESULT
    rule_id = str(item.get("rule_id") or "")
    netuid = int(item.get("netuid"))
    watched_before = _subnet_has_watched_addresses(netuid)
    if rule_id == "network_removed":
        with _SUBNET_LIFECYCLE_LOCK:
            _SUBNET_LIFECYCLE_WATCH_STATE[netuid] = watched_before
        result = SERVICE.archive_subnet_owner_library(
            netuid, reason="network_removed_event",
        )
    elif rule_id == "network_registered":
        result = SERVICE.sync_subnet_owner_addresses()
        with _SUBNET_LIFECYCLE_LOCK:
            restore_watch = _SUBNET_LIFECYCLE_WATCH_STATE.pop(netuid, False) or watched_before
    else:
        raise ValueError(f"unsupported subnet lifecycle event: {rule_id}")
    reconcile = _sync_event_monitor_library_state()
    if rule_id == "network_registered" and restore_watch:
        result["watch_restored"] = _ensure_subnet_owner_watched(netuid)
    else:
        result["watch_restored"] = 0
    SUBNET_LIFECYCLE_RESULT = {
        "event": rule_id,
        "netuid": netuid,
        "event_id": str(item.get("event_id") or ""),
        "result": result,
        "reconcile": reconcile,
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
    }
    return SUBNET_LIFECYCLE_RESULT


def _drain_subnet_lifecycle_queue() -> None:
    global _SUBNET_LIFECYCLE_RUNNING, SUBNET_LIFECYCLE_RESULT
    while True:
        with _SUBNET_LIFECYCLE_LOCK:
            if not _SUBNET_LIFECYCLE_QUEUE:
                _SUBNET_LIFECYCLE_RUNNING = False
                return
            item = _SUBNET_LIFECYCLE_QUEUE.pop(0)
        try:
            _process_subnet_lifecycle_event(item)
        except Exception as exc:  # noqa: BLE001 - 单次事件失败不能阻塞后续事件
            SUBNET_LIFECYCLE_RESULT = {
                **item,
                "error": str(exc),
                "processed_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z"),
            }


def _request_subnet_lifecycle_sync(rule_id: str, netuid: int, event: dict[str, Any]) -> dict[str, Any]:
    global _SUBNET_LIFECYCLE_RUNNING
    item = {
        "rule_id": str(rule_id),
        "netuid": int(netuid),
        "event_id": str(event.get("event_id") or ""),
    }
    with _SUBNET_LIFECYCLE_LOCK:
        _SUBNET_LIFECYCLE_QUEUE.append(item)
        should_start = not _SUBNET_LIFECYCLE_RUNNING
        if should_start:
            _SUBNET_LIFECYCLE_RUNNING = True
    if should_start:
        threading.Thread(
            target=_drain_subnet_lifecycle_queue,
            name="subnet-lifecycle-sync",
            daemon=True,
        ).start()
    return {"queued": True, **item}


async def _auto_sync_subnet_owners() -> None:
    """每周检查一次链上子网项目方；失败只记录状态，不影响服务启动。"""

    try:
        await asyncio.to_thread(
            SERVICE.auto_sync_subnet_owners,
            min_interval_minutes=SUBNET_OWNER_SYNC_INTERVAL_SECONDS // 60,
        )
        await asyncio.to_thread(_sync_event_monitor_library_state)
    except Exception as exc:  # noqa: BLE001 - 后台同步失败不影响服务
        print(f"[subnet-owner-sync] 子网项目方同步失败：{exc}", file=sys.stderr)


async def _subnet_owner_scheduler() -> None:
    """每小时检查一次，服务连续运行跨过 7 天时也会自动更新。"""

    while True:
        await _auto_sync_subnet_owners()
        await asyncio.sleep(SUBNET_OWNER_SYNC_CHECK_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global BUILTIN_STARTUP_RESULT, _EVENT_PROXY_CLIENT
    DATABASE.initialize()
    configure_shared_chain(CONFIG.rpc_url)
    # 外发包自带的预置地址库（app/seed_books/*.txt，文件名即标签组名）：
    # 第一次启动时装进已知地址库，装过就记在 seed_books 表里不再重复导入。
    try:
        if SEED_BOOKS_ENABLED:
            install_seed_books(DATABASE)
    except Exception as exc:  # noqa: BLE001 - 预置库导入失败不应拦住服务启动
        print(f"[seed_books] 预置地址库导入失败：{exc}", file=sys.stderr)
    # 内置固定名单（人工确认的交易所/验证者/公共地址）：每次启动幂等补进
    # 地址库，升级后新增的地址自动生效，不依赖 Taostats 是否配置了 key。
    try:
        if BUILTIN_ADDRESSES_ENABLED:
            BUILTIN_STARTUP_RESULT = SERVICE.ensure_builtin_addresses()
            # 启动时打一行到控制台：远端只改了代码没重启时，这行不会出现。
            snapshot_count = diagnostics.snapshot_status()["count"]
            print(
                f"[builtin] 版本 {VERSION_INFO['version']} 预置验证者 {snapshot_count} 条；"
                f"本次启动新增 {BUILTIN_STARTUP_RESULT['inserted']} 条、补标记 "
                f"{BUILTIN_STARTUP_RESULT['updated']} 条",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - 内置名单写入失败不应拦住服务启动
        print(f"[builtin] 内置固定名单写入失败：{exc}", file=sys.stderr)
    # 上次保存过 Taostats key 就自动装回内存，避免每次重启都要重填。
    # 多把 key 时先装池：池里第一把同时充当「单 key 兼容路径」的那一把，
    # 这样诊断、导出等只认单个 key 的地方行为不变。
    # 每分钟额度：升级套餐后额度会变，Taostats 没有接口能让本地问出"这把 key
    # 是什么档、每分钟多少"，所以只能由用户在页面上填。启动时读回上次保存的值。
    saved_rate = settings_store.load_requests_per_minute()
    if saved_rate:
        taostats.set_requests_per_minute(saved_rate)

    saved_keys = settings_store.load_api_keys()
    if saved_keys:
        taostats.set_key_pool(saved_keys)
        if not taostats_is_configured():
            configure_taostats_api_key(saved_keys[0]["key"])
    elif not taostats_is_configured():
        saved_key = settings_store.load_api_key()
        if saved_key:
            configure_taostats_api_key(saved_key)
    # 远程模式下把访问口令准备好。``run.py --remote`` 已经打过完整面板，
    # 这里只补一句——直接 uvicorn 起服务（没走 run.py）时才需要完整提示。
    if access_mod.remote_enabled():
        access_token, token_created = access_mod.ensure_token()
        if not os.getenv("WALLET_LINK_BANNER_PRINTED"):
            print(
                f"[access] 远程访问已开启，访问口令：{access_token}"
                f"{'（本次新生成）' if token_created else ''}",
                file=sys.stderr,
            )
    if EVENT_MONITOR_ENABLED:
        try:
            EVENT_MONITOR.start(event_monitor_settings(chain_rpc=CONFIG.rpc_url))
            print(
                f"[event-monitor] 事件监控已启动：内部端口 {EVENT_MONITOR.port}，"
                f"事件库 {EVENT_MONITOR.settings.database if EVENT_MONITOR.settings else ''}",
                file=sys.stderr,
            )
            if EVENT_MONITOR.context is not None:
                EVENT_MONITOR.context.request_subnet_lifecycle_sync = _request_subnet_lifecycle_sync
            _sync_event_monitor_library_state()
        except Exception as exc:  # noqa: BLE001 - 事件模块失败不应拖垮地址工具
            print(f"[event-monitor] 事件监控启动失败：{exc}", file=sys.stderr)
    else:
        EVENT_MONITOR.stop()
    _EVENT_PROXY_CLIENT = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        trust_env=False,
    )
    scheduler = asyncio.create_task(_job_scheduler())
    exchange_sync = (
        asyncio.create_task(_auto_sync_exchanges()) if AUTO_EXCHANGE_SYNC_ENABLED else None
    )
    subnet_owner_sync = (
        asyncio.create_task(_subnet_owner_scheduler())
        if AUTO_SUBNET_OWNER_SYNC_ENABLED
        else None
    )
    watch_loop = asyncio.create_task(_watch_scheduler())
    local_scan = (
        asyncio.create_task(_local_scan_scheduler()) if LOCAL_AUTO_SCAN_ENABLED else None
    )
    try:
        yield
    finally:
        scheduler.cancel()
        if exchange_sync is not None:
            exchange_sync.cancel()
        if subnet_owner_sync is not None:
            subnet_owner_sync.cancel()
        watch_loop.cancel()
        if local_scan is not None:
            local_scan.cancel()
        for task in (scheduler, exchange_sync, subnet_owner_sync, watch_loop, local_scan):
            if task is None:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭时不再关心
                pass
        proxy_client = _EVENT_PROXY_CLIENT
        _EVENT_PROXY_CLIENT = None
        if proxy_client is not None:
            await proxy_client.aclose()
        EVENT_MONITOR.stop()
        shutdown_shared_chain()
        DATABASE.engine.dispose()


app = FastAPI(
    title="Bittensor Wallet Link Intel",
    version=VERSION_INFO["version"],
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
SERVICE = LinkService(DATABASE, CONFIG.rpc_url)
JOBS = RelationJobManager()
WATCH = WatchService(DATABASE)
# 定期本地索引扫描的并发保护（真正的互斥在数据库扫描锁里）。
LOCAL_SCAN_LOCK = asyncio.Lock()
# 30 秒巡检与“本地扫描完成后立即巡检”共用一把锁，避免同一进程并发累计。
WATCH_SWEEP_LOCK = asyncio.Lock()

# 勾了「使用 Taostats 历史索引」但没有 key 时直接报错，不再静默退回本地索引：
# 以前那种回退会返回 0 个关联地址，用户只看到一张空表，以为是工具坏了。
TAOSTATS_KEY_MISSING = (
    "未配置 Taostats API key：请先在页面「Taostats API key」里粘贴并点「保存 API key」，再发起查询。"
    "如果只想查本地索引，请取消勾选「使用 Taostats 历史索引」。"
)

# 出站探测结果缓存：探测要发 2~3 个请求，自检连点时不能每次重跑。
_OUTBOUND_PROBE: dict[str, Any] = {"at": 0.0, "payload": None}
_OUTBOUND_PROBE_TTL = 60.0
_OUTBOUND_PROBE_LOCK = threading.Lock()


def _outbound_probe_cached(force: bool = False) -> dict[str, Any] | None:
    """出一份出站可达性报告（带 60 秒缓存）。探测异常不影响自检本身。"""

    now = time.monotonic()
    with _OUTBOUND_PROBE_LOCK:
        cached = _OUTBOUND_PROBE.get("payload")
        if not force and cached and now - float(_OUTBOUND_PROBE["at"]) < _OUTBOUND_PROBE_TTL:
            return cached
        try:
            active = taostats.outbound_proxy_status()
            payload = taostats.probe_outbound()
            payload["active"] = active
        except Exception as exc:  # noqa: BLE001 - 探测失败不能让自检报错
            payload = {
                "results": [],
                "active": {},
                "error": f"{type(exc).__name__}: {exc}",
            }
        _OUTBOUND_PROBE["at"] = now
        _OUTBOUND_PROBE["payload"] = payload
        return payload

# 未激活时也要能打开的路径：首页显示激活页，健康检查和授权接口用于自检/激活。
PUBLIC_PATHS = {
    "/",
    "/api/health",
    "/api/version",
    "/api/license",
    "/api/license/activate",
    # 口令接口同样不受授权状态影响：远程设备要先过口令，才有机会看到激活页。
    "/api/access/status",
    "/api/access/login",
}

# 远程模式下不用口令也能访问的路径：首页（要能弹出输入框）、健康检查、
# 版本号，以及口令接口本身。其余一律要求先通过口令。
ACCESS_PUBLIC_PATHS = {
    "/",
    "/api/health",
    "/api/version",
    "/api/access/status",
    "/api/access/login",
}


@app.middleware("http")
async def request_guard(request: Request, call_next):
    """两道门：先远程访问口令，再离线授权。

    顺序不能反：未授权的远程访客也应该先被口令挡住，否则「这台机器有没有
    激活、机器码是多少」这类信息会直接漏给同网段的任何人。
    """

    path = request.url.path
    if path.startswith("/static/"):
        return await call_next(request)
    # 第一道：只在远程模式下生效；只监听 127.0.0.1 时整段跳过。
    if (
        access_mod.remote_enabled()
        and path not in ACCESS_PUBLIC_PATHS
        and not access_mod.allowed(request)
    ):
        return JSONResponse(
            status_code=401,
            content={
                "detail": "需要访问口令：请输入主机页面上显示的访问口令",
                "code": "need_access_token",
            },
        )
    if path in PUBLIC_PATHS:
        return await call_next(request)
    if not license_mod.is_licensed():
        return JSONResponse(
            status_code=403,
            content={
                "detail": "工具未激活或授权已失效，请在首页粘贴管理员提供的授权码",
                "code": "unlicensed",
                "machine": license_mod.machine_code(),
            },
        )
    return await call_next(request)



async def _job_scheduler() -> None:
    """定时推进到期的分轮作业。

    每轮最多花掉 5 次 Taostats 请求，因此放在线程里执行，避免阻塞事件循环；
    同一时刻只推进一轮，和本机 5 次/分钟的限速保持一致。
    """

    while True:
        await asyncio.sleep(2)
        try:
            for job in JOBS.due():
                await asyncio.to_thread(JOBS.run_round, job)
            JOBS.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 定时器不因单次失败退出
            continue


def _reconcile_stake_transfer_events() -> int:
    """Repair stake-transfer events after the local index catches up.

    A runtime may emit only StakeRemoved before the local index records the
    destination. Once the precise stake_transfer row exists, the source event
    stays a red sell event and a deterministic green buy event is added for the
    destination coldkey.
    """

    if not EVENT_MONITOR.running or EVENT_MONITOR.context is None:
        return 0
    store = EVENT_MONITOR.context.store
    hub = EVENT_MONITOR.context.hub
    candidates = [
        *store.list_events(
            {"network": "finney", "source": "address", "q": "StakeRemoved"},
            limit=500,
            offset=0,
        ),
        *store.list_events(
            {"network": "finney", "source": "address", "event_type": "代币转移"},
            limit=500,
            offset=0,
        ),
    ]
    rows = list({event["event_id"]: event for event in candidates}.values())
    recent = store.list_events(
        {"network": "finney", "source": "address"},
        limit=1000,
        offset=0,
    )
    watched = store.list_watched_addresses("finney", enabled_only=True)
    updated = 0
    for event in rows:
        raw_data = event.get("raw_data") or {}
        stored_module, _, stored_name = str(event.get("event_name") or "").partition(".")
        raw_module = str(raw_data.get("module") or stored_module)
        raw_name = str(raw_data.get("event_name") or stored_name)
        if raw_module != "SubtensorModule" or raw_name != "StakeRemoved":
            continue
        block_hash = str(event.get("block_hash") or raw_data.get("block_hash") or "")
        if not block_hash:
            continue
        raw = {
            "raw_id": f"finney:{block_hash}:{event.get('event_index')}",
            "event_index": event.get("event_index"),
            "module": "SubtensorModule",
            "event_name": "StakeRemoved",
            "raw": raw_data,
        }
        source_event = address_event_record(
            raw,
            network="finney",
            watched_addresses=watched,
            block_hash=block_hash,
            height=int(event.get("block_number") or raw_data.get("height") or 0),
            spec_version=event.get("runtime_spec_version"),
            event_at=str(event.get("event_at") or ""),
        )
        if source_event is None:
            continue
        source_patch = {
            "event_type": source_event["event_type"],
            "event_name": source_event["event_name"],
            "summary": source_event["summary"],
            "details": source_event["details"],
            "enrichment_status": "complete",
        }
        changed = any(event.get(key) != value for key, value in source_patch.items())
        if changed:
            updated_event = store.update_event(
                event["event_id"], "stake_transfer_source_split", source_patch,
            )
            if updated_event is not None:
                hub.publish(updated_event, "stake_transfer_source_split")
                updated += 1

        derived_raw = derived_stake_transfer_buy_raw(raw)
        if derived_raw is None:
            continue
        buy_event = address_event_record(
            derived_raw,
            network="finney",
            watched_addresses=watched,
            block_hash=block_hash,
            height=int(event.get("block_number") or raw_data.get("height") or 0),
            spec_version=event.get("runtime_spec_version"),
            event_at=str(event.get("event_at") or ""),
        )
        if buy_event is None:
            continue
        buy_details = buy_event.get("details") or {}
        duplicate = False
        for candidate in recent:
            if candidate.get("block_number") != event.get("block_number"):
                continue
            details = candidate.get("details") or {}
            if (
                str(candidate.get("event_type") or "") in {"质押", "零号子网质押"}
                and str(details.get("coldkey") or "") == str(buy_details.get("coldkey") or "")
                and str(details.get("hotkey") or "") == str(buy_details.get("hotkey") or "")
                and int(details.get("netuid") if details.get("netuid") is not None else -1)
                == int(buy_details.get("netuid") if buy_details.get("netuid") is not None else -2)
            ):
                duplicate = True
                break
        if duplicate:
            continue
        stored, created = store.create_event(buy_event)
        if created:
            hub.publish(stored, "discovered")
            updated += 1
    return updated


def _sync_auto_import_rows_to_event(result: dict[str, Any]) -> dict[str, Any] | None:
    """把自动入库本轮新增地址同步到已经确认监控的地址库。"""

    global EVENT_WATCH_AUTO_SYNC_RESULT
    rows = result.get("address_rows") or []
    if not rows or not EVENT_MONITOR.running or EVENT_MONITOR.context is None:
        return None
    store = EVENT_MONITOR.context.store
    watched = store.list_watched_addresses("finney")
    monitored_books = {
        str(item.get("library_book") or "").strip()
        for item in watched
        if item.get("enabled") and str(item.get("library_book") or "").strip()
    }
    if not monitored_books:
        return None
    library = {str(item.get("address") or ""): item for item in SERVICE.library()}
    watched_by_address = {str(item.get("address") or ""): item for item in watched}
    added = 0
    updated = 0
    skipped = 0
    synced_books: set[str] = set()
    for row in rows:
        address = str(row.get("address") or "").strip()
        book = str(row.get("book") or "").strip()
        if not address or not book or book not in monitored_books:
            continue
        item = library.get(address) or {}
        label = str(item.get("label") or book)[:120]
        role = "project_owner" if item.get("source") == SUBNET_OWNER_SOURCE else ""
        existing = watched_by_address.get(address)
        try:
            if existing is not None:
                if (
                    str(existing.get("library_book") or "") != book
                    or str(existing.get("address_role") or "") != role
                ):
                    changed = store.update_watched_address(
                        int(existing["address_id"]),
                        label=label,
                        library_book=book,
                        address_role=role,
                    )
                    if changed is not None:
                        watched_by_address[address] = changed
                        updated += 1
                        synced_books.add(book)
                continue
            store.add_watched_address(
                "finney", address, label, 0, library_book=book,
                address_role=role,
            )
            added += 1
            synced_books.add(book)
        except ValueError:
            skipped += 1
    EVENT_WATCH_AUTO_SYNC_RESULT = {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "books": sorted(synced_books),
    }
    if added or updated:
        print(
            f"[event-monitor] 自动入库同步新增 {added} 个、改库 {updated} 个地址",
            file=sys.stderr,
        )
    return EVENT_WATCH_AUTO_SYNC_RESULT


async def _watch_scheduler() -> None:
    """地址库自动入库：每 30 秒把本地索引里的新块拿去撞一次地址库。

    0.9.0 起这里只有本地索引流水线这一条路：它不发任何 Taostats 请求，
    所以不需要「给手动查询让路」、不需要每日预算、也不会和用户抢额度。
    逐地址问 API 的巡检已整条移除——用户的额度全部留给主动发起的关联扫描。

    一次 tick 处理多少行由 ``SWEEP_MAX_ROWS`` 夹住（4 千行），宁可小步快跑，
    也不让一次扫描把 SQLite 锁住几十秒。
    """

    while True:
        await asyncio.sleep(WATCH_TICK_SECONDS)
        try:
            if WATCH_SWEEP_LOCK.locked():
                continue
            async with WATCH_SWEEP_LOCK:
                result = await asyncio.to_thread(WATCH.sweep_local_index)
                await asyncio.to_thread(_sync_auto_import_rows_to_event, result)
                await asyncio.to_thread(_reconcile_stake_transfer_events)
                await asyncio.to_thread(_sync_event_monitor_library_state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 流水线失败不能拖垮服务
            print(f"[watch] 本地索引流水线异常：{exc}", file=sys.stderr)


async def _local_scan_scheduler() -> None:
    """本地索引定期自动扫描：只补启动之后的新块，不消耗 Taostats 额度。"""

    while True:
        await asyncio.sleep(LOCAL_AUTO_SCAN_SECONDS)
        try:
            if LOCAL_SCAN_LOCK.locked():
                continue
            async with LOCAL_SCAN_LOCK:
                result = await asyncio.to_thread(
                    SERVICE.scan,
                    blocks=LOCAL_AUTO_SCAN_BLOCKS,
                    advance=True,
                    catch_up=True,
                )
                if result.get("status") == "busy":
                    continue
                # 新块刚提交就立即撞一次地址库，不等自动入库调度器下一拍。
                if not WATCH_SWEEP_LOCK.locked():
                    async with WATCH_SWEEP_LOCK:
                        result = await asyncio.to_thread(WATCH.sweep_local_index)
                        await asyncio.to_thread(_sync_auto_import_rows_to_event, result)
                        await asyncio.to_thread(_reconcile_stake_transfer_events)
        except asyncio.CancelledError:
            raise
        except ScanBusyError:
            continue
        except Exception as exc:  # noqa: BLE001 - 节点不可用时不打断服务
            print(f"[local-scan] 本地索引扫描异常：{exc}", file=sys.stderr)


def service() -> LinkService:
    return SERVICE


class AddressInput(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    # 分类是用户自定义的（大V / 群友 / 其他 + 自建），不再限定枚举。
    category: str = Field(default="其他", max_length=32)
    label: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=2000)


class BulkDeleteInput(BaseModel):
    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=5000)


class NoteInput(BaseModel):
    note: str = Field(default="", max_length=2000)


class LabelInput(BaseModel):
    label: str = Field(default="", max_length=160)


class RenameLabelInput(BaseModel):
    old_label: str = Field(min_length=1, max_length=160)
    new_label: str = Field(min_length=1, max_length=160)


class RenameBookInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class CategoriesInput(BaseModel):
    categories: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(
        default_factory=list, max_length=10
    )


class GroupCategoriesInput(BaseModel):
    categories: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(
        default_factory=list, max_length=10
    )
    book: str = Field(default="", max_length=60)
    label: str = Field(default="", max_length=160)


class ExchangeConfirmationInput(BaseModel):
    confirmed: bool = True
    rejected: bool = False
    decision: str = Field(default="", pattern="^(|confirm|reject|reset)$")


class ExchangeAddressConfirmationInput(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    # 留空表示沿用地址原有的组名；只有空名或系统自动名才会被替换成默认名。
    label: str = Field(default="", max_length=160)
    decision: str = Field(default="confirm", pattern="^(confirm|reject|reset)$")


class GroupAddressInput(BaseModel):
    """往某个标签组里手动加地址：支持多行 / 逗号 / 空格分隔。"""

    addresses: str = Field(min_length=1, max_length=20000)
    book: str = Field(default="", max_length=60)
    label: str = Field(default="", max_length=160)
    category: str = Field(default="", max_length=32)
    note: str = Field(default="", max_length=2000)
    # 已在别组的地址是否移到本组（默认只新增，已存在的跳过）。
    move_existing: bool = False


class MergeGroupsInput(BaseModel):
    """把几个地址库 / 标签组合并成一个库。"""

    sources: list[dict[str, str]] = Field(min_length=2, max_length=50)
    target: str = Field(min_length=1, max_length=60)


class InterlinkInput(BaseModel):
    """地址 × 地址库 / 地址 × 地址 的转账明细检索。"""

    source: str = Field(min_length=1, max_length=64)
    target: str = Field(default="", max_length=64)
    group_kind: str = Field(default="", max_length=8)
    group_name: str = Field(default="", max_length=160)
    max_hops: int = Field(default=DEFAULT_RELATION_DEPTH, ge=1, le=MAX_RELATION_DEPTH)
    max_pages: int = Field(default=DEFAULT_RELATION_DEPTH, ge=1, le=MAX_RELATION_DEPTH)
    min_tao: float = Field(default=0.0, ge=0)
    use_local: bool = True
    use_taostats: bool = True


class AddressNoteInput(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=2000)


class RemovedRelationInput(BaseModel):
    """标记一个「干扰地址」：永久排除，不进结果、连带剪下游。

    与 `AddressNoteInput` 分开是因为语义不同：note 是描述"这个地址是谁"，
    这里是下一个否定结论"这条线索别看了"。
    """

    address: str = Field(min_length=1, max_length=64)
    seed: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=2000)


class ScanThresholdInput(BaseModel):
    """扫描阈值：单边 / 累计 / 1 级关联最低 TAO，存到本机 settings.json。

    三个字段都允许省略：省略就保持上次保存的值，避免前端空输入框把用户
    已保存的设置悄悄冲成出厂值（0.9.7 及以前会把空值硬塞成 "1"）。
    """

    min_edge_tao: str | None = Field(default=None, max_length=32)
    min_total_tao: str | None = Field(default=None, max_length=32)
    min_direct_tao: str | None = Field(default=None, max_length=32)


class WatchConfigInput(BaseModel):
    """地址库自动入库设置：阈值、候选保留期、自动入库和流水线开关。

    0.9.0 起逐地址问 API 的巡检已移除，所以这里不再有开关/间隔/每日预算/
    每轮推进量/是否含代币转移——那些都只对「问 API」有意义。
    """

    threshold_tao: float | None = Field(default=None, ge=0, le=1_000_000)
    candidate_window_days: int | None = Field(default=None, ge=1, le=365)
    auto_import: bool | None = None
    # 本地索引流水线开关（零 API 的那条采集链路）。
    sweep_enabled: bool | None = None


class RelationInput(BaseModel):
    seed: str = Field(min_length=1, max_length=64)
    depth: int = Field(
        default=DEFAULT_RELATION_DEPTH, ge=1, le=MAX_RELATION_DEPTH
    )
    min_edge_tao: str = Field(default="1")
    min_total_tao: str = Field(default="1")
    min_direct_tao: str = Field(default="0")
    include_incoming: bool = True
    include_outgoing: bool = True
    data_source: str = Field(default="local", pattern="^(local|taostats)$")
    filter_exchanges: bool = False
    filter_validators: bool = False
    filter_systems: bool = False
    filter_publics: bool = False
    # 手动「移除关联」的地址：不进结果，也不再作为中转跳点（其线索整体剪除）。
    removed_addresses: list[str] = Field(default_factory=list, max_length=100)
    # 扫描开始前，前端已经用 /api/seed-profile 花掉的请求数：从本轮额度里扣。
    prefetched_requests: int = Field(default=0, ge=0, le=10)


class RelationJobInput(BaseModel):
    seed: str = Field(min_length=1, max_length=64)
    depth: int = Field(
        default=DEFAULT_RELATION_DEPTH, ge=1, le=MAX_RELATION_DEPTH
    )
    min_edge_tao: str = Field(default="1")
    min_total_tao: str = Field(default="1")
    min_direct_tao: str = Field(default="0")
    include_incoming: bool = True
    include_outgoing: bool = True
    filter_exchanges: bool = False
    filter_validators: bool = False
    filter_systems: bool = False
    filter_publics: bool = False
    removed_addresses: list[str] = Field(default_factory=list, max_length=100)
    round_interval_seconds: int | None = Field(default=None, ge=10, le=600)
    max_rounds: int | None = Field(default=None, ge=1, le=100)
    prefetched_requests: int = Field(default=0, ge=0, le=10)


class SeedProfileInput(BaseModel):
    """起始地址画像：扫描一开始先查这个，左栏立刻能显示。"""

    seed: str = Field(min_length=1, max_length=64)
    data_source: str = Field(default="taostats", pattern="^(local|taostats)$")


class RelationSyncInput(BaseModel):
    seed: str = Field(default="", max_length=64)
    addresses: list[str] = Field(default_factory=list, max_length=5000)
    include_seed: bool = True
    category: str = Field(default="其他", max_length=32)
    label: str = Field(default="", max_length=160)
    # 合并导入到自建地址库：新地址直接挂上库名，不另起标签组。
    book: str = Field(default="", max_length=60)
    note: str = Field(default="", max_length=2000)
    # 只同步「已经命中地址库的关联地址 + 发起地址」，用于把发起地址补进库。
    only_library_matched: bool = False


class HistoryDeleteInput(BaseModel):
    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=5000)


class AddressBookInput(BaseModel):
    """新建地址库：给一次查询结果起个库名，把可导入地址批量入库。"""

    name: str = Field(min_length=1, max_length=60)
    seed: str = Field(default="", max_length=64)
    addresses: list[str] = Field(default_factory=list, max_length=5000)
    include_seed: bool = True
    category: str = Field(default="其他", max_length=32)
    note: str = Field(default="", max_length=2000)


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class LibraryExportInput(BaseModel):
    """按选中地址导出：不传 addresses 时等价于“导出全部”。"""

    format: str = Field(default="csv", pattern="^(csv|json)$")
    addresses: list[str] = Field(default_factory=list, max_length=5000)


class PublicAddressInput(BaseModel):
    """人工标记 / 取消「公共地址」（与交易所同性质，查到即截止）。"""

    address: str = Field(min_length=1, max_length=64)
    marked: bool = True


class RevokeAndMarkInput(BaseModel):
    """从入库日志一键「撤回并标为系统角色」。"""

    role: str = Field(default="exchange", max_length=16)
    # 前端把条目里的地址带上，免得撤回之后再去猜；留空则由后端从日志取。
    address: str = Field(default="", max_length=64)


class ReconcileInput(BaseModel):
    """按当前地址库校正历史结果（交易所/验证者/公共地址产生的下游关联）。"""

    # 只预演、不落库：界面先给用户看「会移除多少条」再确认。
    dry_run: bool = False
    # 留空 = 校正全部历史记录；指定则只处理这几条。
    archive_ids: list[int] | None = Field(default=None, max_length=500)


class TidyInput(BaseModel):
    """一键整理地址库：把跨机同步 / 多次导入后的常见「污染」规整掉。"""

    # 只预演、不落库：界面先给用户看「会改哪些」再确认。
    dry_run: bool = False


class LicenseActivateInput(BaseModel):
    token: str = Field(min_length=10, max_length=4000)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/event-monitor/floating", include_in_schema=False)
def event_monitor_floating():
    """事件监控浮窗页面，走主服务的鉴权与许可证中间件。"""

    return FileResponse(
        EVENT_MONITOR_STATIC / "floating.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/event-monitor", include_in_schema=False)
@app.get("/event-monitor/{path:path}", include_in_schema=False)
def event_monitor_page(path: str = ""):
    """事件监控单页入口；详情页刷新也回到同一 HTML，由前端路由接管。"""

    return FileResponse(
        EVENT_MONITOR_STATIC / "index.html",
        headers={"Cache-Control": "no-store"},
    )


_EVENT_PROXY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


@app.api_route(
    "/event-api/{upstream_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def event_monitor_api_proxy(upstream_path: str, request: Request):
    """把事件监控 API 代理到仅回环可访问的内部 HTTP 服务。

    这样浏览器只看到主服务 origin，访问口令和离线授权中间件无需复制；
    内部事件服务也不会直接暴露到局域网或公网。
    """

    if not EVENT_MONITOR.running or not EVENT_MONITOR.port:
        raise HTTPException(503, "事件监控尚未就绪，请稍后刷新或查看服务日志")
    client = _EVENT_PROXY_CLIENT
    if client is None:
        raise HTTPException(503, "事件监控代理尚未初始化")
    query = f"?{request.url.query}" if request.url.query else ""
    target = f"http://127.0.0.1:{EVENT_MONITOR.port}/api/{upstream_path}{query}"
    body = await request.body()
    headers = {
        name: value for name, value in request.headers.items()
        if name.casefold() not in _EVENT_PROXY_HOP_HEADERS
        and name.casefold() != "accept-encoding"
    }
    upstream_request = client.build_request(
        request.method,
        target,
        headers=headers,
        content=body,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"事件监控内部服务不可用：{exc}") from exc
    response_headers = {
        name: value for name, value in upstream_response.headers.items()
        if name.casefold() not in _EVENT_PROXY_HOP_HEADERS
        and name.casefold() not in {"content-encoding"}
    }
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_response.aclose),
    )


@app.post("/api/event-monitor/chain/reset")
def reset_event_monitor_chain():
    """Force the shared Finney WebSocket and event chain collector to reconnect."""

    if not EVENT_MONITOR.running:
        raise HTTPException(503, "事件监控尚未就绪，请稍后重试")
    collector = EVENT_MONITOR.force_chain_reconnect()
    try:
        shared = reset_shared_chain(
            CONFIG.rpc_url,
            reason="event monitor manual reset",
        )
    except Exception as exc:
        raise HTTPException(502, f"主链连接复位失败：{exc}") from exc
    return {
        "ok": True,
        "collector": collector,
        "shared_chain": shared,
        "message": "主链连接已复位，采集器将重新建立连接",
    }


@app.post("/api/event-monitor/watch/reconcile")
def reconcile_event_monitor_watch():
    """按当前共享地址库清理已解散库的旧监听地址并修正迁移归属。"""

    result = _sync_event_monitor_library_state()
    if result is None:
        raise HTTPException(503, "事件监控尚未就绪，请稍后重试")
    if result.get("error"):
        raise HTTPException(500, result["error"])
    return {"ok": True, **result}


@app.get("/api/event-monitor/runtime")
def event_monitor_runtime_status():
    """合并服务里事件采集线程和内部端口的状态，便于排障。"""

    return EVENT_MONITOR.health()


@app.get("/api/event-monitor/library")
def event_monitor_shared_library(
    svc: Annotated[LinkService, Depends(service)],
):
    """事件监控选择器读取的唯一地址库来源。

    该接口只提供候选地址；事件监控是否真正开始监控，仍由用户在监控地址页
    勾选并提交决定，``watched_addresses`` 默认不会自动导入。
    """

    items = svc.library()
    books = [item for item in items if str(item.get("book") or "").strip()]
    project_books = {
        str(item.get("book") or "").strip()
        for item in books
        if item.get("source") == SUBNET_OWNER_SOURCE
    }

    def book_order(item: dict[str, Any]) -> tuple[int, int, str]:
        name = str(item.get("book") or "").strip()
        match = re.match(r"^SN\s*(\d+)\b", name, re.IGNORECASE)
        if name in project_books and match:
            return (0, int(match.group(1)), name)
        if name in project_books:
            return (0, 10**9, name)
        return (1, 0, name)

    books.sort(key=book_order)
    return {
        "items": books,
        # 未归入命名地址库的单条资料不直接作为监控对象；用户应先建立地址库。
        "unbooked_count": len(items) - len(books),
    }


# -- 远程访问口令 ----------------------------------------------------------
class AccessLoginInput(BaseModel):
    """远程设备输入访问口令。"""

    token: str = Field(min_length=4, max_length=64)


class AccessLaunchInput(BaseModel):
    """主机本机切换访问模式；只允许 remote / tunnel 两种。"""

    mode: str = Field(pattern="^(remote|tunnel)$")


@app.post("/api/access/launch")
def access_launch(payload: AccessLaunchInput, request: Request):
    """远程访问组件上的两个按钮：运行时开启远程访问或公网隧道。

    只认主机本机发起的请求。0.8.48 起不再重启服务——监听地址是启动时定的，
    但远程开关、隧道来源判定都是运行时读的环境变量，公网入口也只是额外一个
    cloudflared 进程。服务绑在回环上时会明确提示先用对应启动脚本启动一次。
    """

    if not access_mod.is_local(request):
        raise HTTPException(403, "只有主机本机可以切换访问模式")
    try:
        return remote_launch.start_launch(payload.mode, _request_port(request))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/access/status")
def access_status(request: Request):
    """前端据此判断要不要弹口令框、要不要显示「远程访问」标识。

    额外带回「这台主机该怎么连」的地址清单与访问口令，供页面顶部展示。
    **这些只在请求确实来自主机本机（回环）时才给**——远程设备即使已经登录，
    也不该看到别人的局域网地址，更不该看到访问口令（知道口令就等于拿到
    一份可转发的通行证）。
    """

    payload = access_mod.status(request)
    if payload.get("local"):
        payload["endpoints"] = _access_endpoints(_request_port(request))
        # 启动按钮的最近一次结果（idle / launching / ready / error）。
        # 服务重启期间接口短暂不可用，新服务起来后靠它把结果补回页面。
        payload["launch"] = remote_launch.read_state()
    # 服务自己上报的启动时间。前端据此判断「服务确实重启过了」，
    # 不再把启动器能否写回 ready 当成唯一成功判据——启动器被系统终止时
    # 状态文件会永远停在 launching，页面就会一直转圈（0.8.45 实测踩到）。
    payload["server_started_at"] = diagnostics.STARTED_AT.isoformat(timespec="seconds")
    return payload


def _request_port(request: Request) -> int:
    """从请求里问出当前服务端口。

    比自己猜一个默认值靠谱：端口冲突时启动脚本会把 8766 顺延成 8767，
    写死 8766 就会给用户一个连不上的地址。
    """

    server = request.scope.get("server") or ()
    if len(server) >= 2 and isinstance(server[1], int) and server[1] > 0:
        return int(server[1])
    host = request.headers.get("host") or ""
    if ":" in host:
        try:
            return int(host.rsplit(":", 1)[1])
        except ValueError:
            pass
    return 8766


def _access_endpoints(port: int) -> dict[str, Any]:
    """主机本机看到的一组连接信息（地址 + 口令，都带说明）。"""

    tailscale = netinfo.tailscale_addresses()
    primary, others = netinfo.lan_addresses()
    tunnel = netinfo.read_tunnel_url(ROOT_DIR / "logs" / netinfo.URL_FILE_NAME, 0)

    items: list[dict[str, str]] = [
        {"kind": "本机", "value": f"http://127.0.0.1:{port}", "hint": "这台电脑自己打开"},
    ]
    for address in tailscale:
        items.append(
            {
                "kind": "Tailscale",
                "value": f"http://{address}:{port}",
                "hint": "地址固定，手机 / 别的电脑（装了 Tailscale 且已开启）",
            }
        )
    if primary:
        items.append(
            {
                "kind": "局域网",
                "value": f"http://{primary}:{port}",
                "hint": "同一个 Wi-Fi 下可用",
            }
        )
    if tunnel:
        items.append(
            {
                "kind": "公网隧道",
                "value": tunnel,
                "hint": "任意网络、无需装软件；网址每次重启都换",
            }
        )
    token = settings_store.load_access_token() or ""
    remote = access_mod.remote_enabled()
    return {
        "port": port,
        "remote": remote,
        "items": items,
        # 口令只在远程模式下有意义（本机模式整套校验是跳过的）。
        "token": token if remote else "",
        "others": others,
    }


@app.post("/api/access/login")
def access_login(payload: AccessLoginInput, response: Response):
    if not access_mod.verify(payload.token):
        raise HTTPException(401, "访问口令不正确")
    token = settings_store.load_access_token() or ""
    response.set_cookie(
        key=access_mod.COOKIE_NAME,
        value=token,
        max_age=access_mod.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@app.get("/api/access/token")
def access_token(request: Request):
    """在主机本机查看完整口令，方便复制到手机或另一台电脑。

    远程请求一律拒绝——能远程读到口令，这道门就等于不存在。
    """

    if not access_mod.is_local(request):
        raise HTTPException(403, "只能在运行服务的那台电脑上查看访问口令")
    token, created = access_mod.ensure_token()
    return {"token": token, "created": created, "hint": access_mod.hint()}


@app.post("/api/access/token/rotate")
def access_token_rotate(request: Request):
    """重置口令：旧口令连同所有已登录设备一起失效。"""

    if not access_mod.is_local(request):
        raise HTTPException(403, "只能在运行服务的那台电脑上重置访问口令")
    token = access_mod.rotate()
    return {"token": token, "hint": access_mod.hint()}


@app.get("/api/health")
def health(svc: Annotated[LinkService, Depends(service)]):
    status = license_mod.status()
    # 预置名单是判断「新版本是否真的生效」的最快依据：
    # 远端只 pull 代码没重启时，这里会一直显示 0 条。
    snapshot = diagnostics.snapshot_status()
    return {
        "ok": True,
        "service": "bittensor-wallet-link-intel",
        "version": VERSION_INFO["version"],
        "rpc_url": svc.rpc_url,
        "licensed": status["licensed"],
        "team": status["team"],
        # pid/root 用来区分“同一个服务”还是“另一个副本占着同一个端口”：
        # 把整个文件夹拷到别的磁盘再启动时，端口若被原副本占用，启动脚本据此提示换端口。
        "pid": os.getpid(),
        "root": str(ROOT_DIR),
        "started_at": diagnostics.STARTED_AT.isoformat(timespec="seconds"),
        "validator_snapshot": snapshot["count"],
        "validator_snapshot_mtime": snapshot["mtime"],
        "event_monitor": {
            "running": EVENT_MONITOR.running,
            "error": EVENT_MONITOR.error,
        },
        "shared_chain": shared_chain_stats(),
        "default_subnet_watch": DEFAULT_SUBNET_WATCH_RESULT,
        "event_watch_auto_sync": EVENT_WATCH_AUTO_SYNC_RESULT,
        "event_watch_reconcile": EVENT_WATCH_RECONCILE_RESULT,
        "subnet_lifecycle": SUBNET_LIFECYCLE_RESULT,
    }


@app.get("/api/diagnose")
def diagnose(svc: Annotated[LinkService, Depends(service)]):
    """环境自检：版本 / 预置名单文件 / 落库情况 / API key，一屏定位问题。"""

    return diagnostics.build_report(
        DATABASE,
        # 磁盘上的版本 vs 进程内的版本：不一致说明代码更新了但没重启。
        version=read_version_info()["version"],
        running_version=VERSION_INFO["version"],
        root=ROOT_DIR,
        db_path=DATABASE.path,
        taostats_configured=taostats_is_configured(),
        startup_builtin=BUILTIN_STARTUP_RESULT,
        # 界面上会重复点，给 5 秒缓存；管理端改了地址库也能很快看到。
        cache_ttl=5.0,
        # 出站可达性单独缓存：探测要发 2~3 个请求，不能每次点自检都跑一遍。
        outbound_probe=_outbound_probe_cached(),
    )


@app.get("/api/license")
def license_status():
    """当前授权状态；未激活时也开放，用于在激活页显示本机机器码。"""

    return license_mod.status()


@app.post("/api/license/activate")
def license_activate(payload: LicenseActivateInput):
    try:
        row = license_mod.save_token(payload.token)
    except license_mod.LicenseError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "team": row.team, "status": license_mod.status()}


@app.get("/api/version")
def version_info():
    """Return the version and structured release notes shown in the UI.

    ``version`` 是**磁盘上** VERSION 文件的内容，``running_version`` 是**当前进程**
    开始运行时的版本。只 git pull 不重启服务时两者会不一样——这种情况以前
    完全看不出来：页面照旧显示新版本号，功能却还是旧的，于是「升级了但预置
    名单还是没出现」。这里把差异明确回报给前端。
    """

    disk = read_version_info()
    running = VERSION_INFO["version"]
    disk["running_version"] = running
    disk["stale"] = str(disk["version"]) != str(running)
    return disk


@app.get("/api/overview")
def overview(request: Request, svc: Annotated[LinkService, Depends(service)]):
    local = access_mod.is_host(request)
    return {
        "index": svc.index_summary(),
        "default_seed": svc.seed_address(),
        "rpc_url": svc.rpc_url,
        # 「有没有配 key」远程需要知道（决定 Taostats 数据源能不能用），
        # 但「怎么保存的」（DPAPI / 明文）属于主机管理信息，远程不给。
        "taostats_configured": taostats_is_configured(),
        "taostats_key_storage": settings_store.storage_kind() if local else "",
        "api_usage": svc.api_usage_summary(),
        # 页面初始化时一并带回上次保存的扫描阈值，省一次请求。
        "scan_thresholds": settings_store.load_scan_thresholds(),
        "max_depth": MAX_RELATION_DEPTH,
        "default_depth": DEFAULT_RELATION_DEPTH,
    }


@app.post("/api/settings/taostats")
async def save_taostats_key(request: Request):
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(413, "API key 请求内容过大")
    try:
        import json

        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "API key 请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "API key 请求格式无效")
    candidate = payload.get("api_key")
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 1024:
        raise HTTPException(422, "API key 不能为空且不得超过 1024 个字符")
    _require_local(request, "保存 API key")
    key = candidate.strip()
    configure_taostats_api_key(key)
    # 同时也存到本机（DPAPI 加密），重启服务后自动装回内存。
    storage = settings_store.save_api_key(key)
    return {"configured": True, "storage": storage}


@app.delete("/api/settings/taostats")
def delete_taostats_key(request: Request):
    _require_local(request, "清除 API key")
    clear_taostats_api_key()
    settings_store.clear_api_key()
    return {"configured": False, "storage": ""}


def _require_local(request: Request, what: str) -> None:
    """管理类接口只允许主机本机调用。

    以前只有 ``/api/access/token`` 做了这道检查，key 池的读写没有——远程
    设备（哪怕已过口令）都能看到还剩多少额度、换掉 key、改每分钟限速。
    已登录的远程用户本来就能正常查询，不需要这些管理能力；把管理面留在
    主机上，「知道口令」就不等于「能改配置」。

    判据用 :func:`access.is_host`：未开远程模式时服务只监听 127.0.0.1，
    一律算主机（否则只监听回环的部署和测试客户端会全部 403）。
    """

    if not access_mod.is_host(request):
        raise HTTPException(403, f"{what}只能在运行服务的那台电脑上操作")


# -- Taostats API key 池（多把 key）----------------------------------------
#
# 单把 key 时每分钟只有 5 次额度，多人同时查会互相挤——先到的人占满窗口，
# 后到的人直接吃 429。多把 key 是唯一能线性扩容的办法：每把各自 5 次/分钟，
# 池子按「剩余额度最多 + 轮转」调度。详见 app/taostats.py 的 key 池注释。


class ApiKeyEntry(BaseModel):
    key: str = Field(default="", max_length=1024)
    label: str = Field(default="", max_length=40)
    # 已保存过的 key 用 id 引用（服务端从不回显明文，前端拿不到原文）。
    # 新加的 key 才带 key 字段。
    id: str = Field(default="", max_length=32)


class ApiKeyPoolInput(BaseModel):
    keys: list[ApiKeyEntry] = Field(default_factory=list, max_length=20)


@app.get("/api/settings/taostats/keys")
def get_taostats_keys(request: Request):
    """当前 key 池状态。**永不回显明文**，只给尾四位、剩余额度和冷却时间。"""

    _require_local(request, "查看 API key 池")
    return {
        "items": taostats.key_pool_status(),
        "storage": settings_store.api_keys_storage_kind(),
        "configured": taostats_is_configured(),
        # 每把 key 各自的每分钟额度，前端据此算"池子总容量"。
        "per_key_rate": taostats.requests_per_minute(),
    }


class RateLimitInput(BaseModel):
    per_minute: int = Field(ge=1, le=600)


@app.post("/api/settings/taostats/rate")
def set_taostats_rate(payload: RateLimitInput, request: Request):
    """设置「每把 key 每分钟」的本地限速。

    为什么需要手动填：Taostats 没有提供任何接口能让本地查询出「这把 key 属于
    哪个套餐、每分钟多少额度」，服务端只在超限时回 429。所以本地保护窗口只能
    按用户声明的额度来配——免费档 5，升级后按实际填。

    配高了只会多吃几次 429（该 key 会被自动冷却一段时间），不会写坏数据；
    配低了会白白浪费额度。改完立即生效，不需要重启。
    """

    _require_local(request, "修改每分钟额度")
    limit = taostats.set_requests_per_minute(payload.per_minute)
    settings_store.save_requests_per_minute(limit)
    return {
        "saved": True,
        "per_minute": limit,
        "items": taostats.key_pool_status(),
    }


@app.post("/api/settings/taostats/keys")
def save_taostats_keys(payload: ApiKeyPoolInput, request: Request):
    """整体替换 key 池。

    整体替换而不是逐条增删：页面上就是一张表，用户改完点保存，服务端照单
    全收。已存在的 key 会保留运行时状态（窗口计数与冷却），所以重复保存
    不会把刚被限流的 key 立刻"复活"。

    **已保存的 key 前端拿不到明文**（服务端从不回显），所以它们按 ``id``
    引用；只有新加的那几把才带 ``key`` 字段。``id`` 对不上任何现存 key 时
    直接忽略——宁可少存，也不能凭空造出一把空 key。
    """

    _require_local(request, "管理 API key")
    existing = {item["id"]: item for item in taostats.key_pool_status()}
    # 明文只在服务端内存里有；按 id 从这里取回，避免让明文穿过浏览器。
    secrets_by_id = {state.key_id: state.secret for state in taostats._key_pool}

    entries: list[dict[str, str]] = []
    for item in payload.keys:
        label = item.label.strip()
        if item.key.strip():
            entries.append({"key": item.key.strip(), "label": label})
            continue
        if item.id and item.id in secrets_by_id:
            entries.append({"key": secrets_by_id[item.id], "label": label or existing[item.id].get("label", "")})
        # id 无效且没有明文：跳过，不报错（前端可能传了过期状态）

    if len(entries) > 20:
        raise HTTPException(422, "最多支持 20 把 API key")
    for item in entries:
        if len(item["key"]) > 1024:
            raise HTTPException(422, "API key 不得超过 1024 个字符")

    storage = settings_store.save_api_keys(entries)
    taostats.set_key_pool(entries)
    # 池里第一把同时作为「单 key 兼容路径」的那一把：诊断、导出等只认单个
    # key 的地方行为不变；池被清空时退回未配置。
    if entries:
        configure_taostats_api_key(entries[0]["key"])
    else:
        clear_taostats_api_key()
    return {
        "saved": True,
        "storage": storage,
        "items": taostats.key_pool_status(),
        "configured": taostats_is_configured(),
    }


@app.get("/api/settings/scan-thresholds")
def get_scan_thresholds():
    """上次保存的三把金额尺；没保存过返回出厂值 1 / 1 / 0。"""

    return settings_store.load_scan_thresholds()


@app.post("/api/settings/scan-thresholds")
def save_scan_thresholds(payload: ScanThresholdInput):
    """把扫描阈值存到本机 settings.json，下次打开页面自动填回。

    层级放到 4～10 级时，用户通常会把阈值抬到 10 / 50 这种量级；不落盘的话
    每开一次页面都要重填。省略/非法值不报错，保持上次保存的值并在返回值里
    体现；1 级关联门槛省略时也保持原值，不会把用户设好的防蹭关联门槛清掉。
    """

    return settings_store.save_scan_thresholds(
        payload.min_edge_tao,
        payload.min_total_tao,
        payload.min_direct_tao,
    )


@app.get("/api/library")
def library(svc: Annotated[LinkService, Depends(service)]):
    return {"items": svc.library()}


@app.get("/api/library/export")
def export_library(
    svc: Annotated[LinkService, Depends(service)],
    format: str = Query(default="json", pattern="^(csv|json)$"),
):
    content, media_type = svc.export_library(format)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="bittensor-address-library.{format}"'
            )
        },
    )


@app.post("/api/library/export")
def export_selected_library(payload: LibraryExportInput, svc: Annotated[LinkService, Depends(service)]):
    """导出勾选的地址；``addresses`` 为空时退化成导出全部。"""

    try:
        content, media_type = svc.export_library(
            payload.format, payload.addresses or None
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc
    count = len(payload.addresses or [])
    suffix = f"-selected-{count}" if count else ""
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="bittensor-address-library{suffix}.{payload.format}"'
            )
        },
    )


@app.post("/api/library")
def create_address(payload: AddressInput, svc: Annotated[LinkService, Depends(service)]):
    if not payload.address.strip():
        raise HTTPException(422, "address must not be blank")
    return svc.import_rows([payload.model_dump()], "manual")


@app.post("/api/interlink")
def interlink_search(
    payload: InterlinkInput, svc: Annotated[LinkService, Depends(service)]
):
    """查两个地址（或一个地址和整个地址库）之间的转账明细与中间链路。"""

    try:
        return svc.interlink_search(
            source=payload.source,
            target=payload.target,
            group_kind=payload.group_kind,
            group_name=payload.group_name,
            max_hops=payload.max_hops,
            max_pages=payload.max_pages,
            min_tao=payload.min_tao,
            use_local=payload.use_local,
            use_taostats=payload.use_taostats,
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/relations/removed")
def list_removed_relations(svc: Annotated[LinkService, Depends(service)]):
    """被人工判定为「干扰」的关联地址清单（永久移除，结果页不再显示）。"""

    items = svc.removed_relations()
    return {"items": items, "total": len(items)}


@app.post("/api/relations/removed")
def add_removed_relation(
    payload: RemovedRelationInput, svc: Annotated[LinkService, Depends(service)]
):
    """把地址永久标记为干扰：以后任何一次扫描都不再显示它，也连带剪掉下游。

    用户原话：「手动移除关联的地址是不方便分类，但人工能确定是干扰地址。」
    这个标记**不进地址库、不占分类**，只是一条"这条线索是噪音"的否定结论。
    """

    try:
        return svc.remove_relation(
            payload.address, seed=payload.seed, note=payload.note
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/relations/removed/{address}")
def delete_removed_relation(
    address: str, svc: Annotated[LinkService, Depends(service)]
):
    """取消某个地址的干扰标记，让它重新可以出现在结果里。"""

    return svc.restore_relation(address)


@app.post("/api/relations/removed/clear")
def clear_removed_relations(svc: Annotated[LinkService, Depends(service)]):
    """清空全部干扰标记（判错了想全部恢复）。"""

    return svc.clear_removed_relations()


@app.get("/api/library/books")
def list_address_books(svc: Annotated[LinkService, Depends(service)]):
    """自建地址库清单（按库名归组，含地址数与分类统计）。"""

    return {"items": svc.address_books()}


@app.get("/api/library/categories")
def list_categories(svc: Annotated[LinkService, Depends(service)]):
    """地址库分类清单：大V / 群友 / 其他 + 用户自建，含各分类地址数。"""

    return {"items": svc.categories()}


@app.post("/api/library/categories")
def create_category(payload: CategoryInput, svc: Annotated[LinkService, Depends(service)]):
    try:
        return svc.add_category(payload.name)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/library/categories/{name}")
def delete_category(name: str, svc: Annotated[LinkService, Depends(service)]):
    try:
        return svc.delete_category(name)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/books")
def create_address_book(
    payload: AddressBookInput, svc: Annotated[LinkService, Depends(service)]
):
    """新建地址库并导入：关联地址 + 发起地址，排除交易所和验证者。"""

    try:
        return svc.create_address_book(
            name=payload.name,
            seed=payload.seed.strip(),
            addresses=payload.addresses,
            include_seed=payload.include_seed,
            category=payload.category,
            note=payload.note,
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/library/books/{name}/export")
def export_address_book(
    name: str,
    svc: Annotated[LinkService, Depends(service)],
    format: str = Query(default="txt", pattern="^(txt|csv)$"),
):
    try:
        content, media_type, filename = svc.export_address_book(name, format)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@app.delete("/api/library/books/{name}")
def dissolve_address_book(name: str, svc: Annotated[LinkService, Depends(service)]):
    """解散地址库：库名和库里的地址一起删除（可用「撤回上次导入」恢复）。"""

    try:
        result = svc.dissolve_address_book(name)
        _sync_event_monitor_library_state()
        return result
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/library/{row_id:int}")
def delete_address(row_id: int, svc: Annotated[LinkService, Depends(service)]):
    try:
        if not svc.remove_address(row_id):
            raise HTTPException(404, "address not found")
    except InputError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.patch("/api/library/{row_id:int}")
def update_address_note(
    row_id: int,
    payload: NoteInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        return svc.update_library_note(row_id, payload.note)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/library/{row_id:int}/label")
def update_address_label(
    row_id: int,
    payload: LabelInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """人工修改地址标签；留空表示恢复默认（地址后四位）。"""

    try:
        return svc.update_library_label(row_id, payload.label)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/library/rename-label")
def rename_label_group(
    payload: RenameLabelInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """整组改名：把该标签组下的地址一起改名。"""

    try:
        return svc.rename_label_group(payload.old_label, payload.new_label)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/library/seed-books")
def seed_books_info(svc: Annotated[LinkService, Depends(service)]):
    """外发预置地址库概况：有几个组、合计多少个地址。"""

    return svc.seed_books_info()


@app.post("/api/library/seed-books/reinstall")
def reinstall_seed_books(svc: Annotated[LinkService, Depends(service)]):
    """重新导入预置地址库：补齐预置组里地址库中缺失的地址。"""

    try:
        return svc.reinstall_seed_books()
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/library/last-import")
def last_import_batch(svc: Annotated[LinkService, Depends(service)]):
    """最近一次批量导入（同步 / 建库 / 文件导入）的摘要。"""

    return {"item": svc.last_import_batch()}


@app.post("/api/library/undo-last-import")
def undo_last_import(svc: Annotated[LinkService, Depends(service)]):
    """撤回上一次批量导入，防止无效关联地址被错误导入。"""

    try:
        return svc.undo_last_import()
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/library/books/{name}/rename")
def rename_address_book(
    name: str,
    payload: RenameBookInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """自建地址库改名（库名 + 库内地址的标签）。"""

    try:
        return svc.rename_address_book(name, payload.name)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/group-categories")
def update_group_categories(
    payload: GroupCategoriesInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """整组改分类：把一个自建库或人工标签组里所有地址的分类一起改掉。"""

    if not payload.categories:
        raise HTTPException(422, "请至少选一个分类")
    try:
        return svc.update_group_categories(
            payload.categories, book=payload.book, label=payload.label
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/library/{row_id:int}/categories")
def update_address_categories(
    row_id: int,
    payload: CategoriesInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """人工修改地址分类，一个地址可同时属于多个分类。"""

    try:
        return svc.update_library_categories(row_id, payload.categories)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/library/{row_id:int}/confirm-exchange")
def confirm_exchange(
    row_id: int,
    payload: ExchangeConfirmationInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        decision = payload.decision or (
            "reject" if payload.rejected else ("confirm" if payload.confirmed else "reset")
        )
        return svc.decide_exchange(row_id, decision)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/library/confirm-exchange-address")
def confirm_exchange_address(
    payload: ExchangeAddressConfirmationInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        return svc.decide_exchange_address(payload.address, payload.decision, payload.label)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/mark-public")
def mark_public_address(
    payload: PublicAddressInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """把地址标为 / 取消「公共地址」。

    公共地址和交易所同性质：会和大量不确定钱包往来，查询时作为关联终点，
    不再向下扩展。
    """

    try:
        return svc.decide_public_address(payload.address, payload.marked)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/mark-validator")
def mark_validator_address(
    payload: PublicAddressInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """把地址标为 / 取消「验证者」。

    与交易所、公共地址同性质：查询时作为关联终点，不再向下扩展；
    下结论即移出用户自建库，以后关联到别的库再单独加回去。
    """

    try:
        return svc.decide_validator_address(payload.address, payload.marked)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


class NoteOnlyAdoptInput(BaseModel):
    ids: list[int] = Field(default_factory=list)
    book: str = Field(default="", max_length=80)


@app.post("/api/library/note-only-adopt")
def adopt_note_only(
    payload: NoteOnlyAdoptInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """把「仅备注」的地址归入地址库（已有库或新建）。

    这些地址只写过备注、从未入库；建库时若不在当时的关联结果里就不会被
    自动带入，这里给一个显式的归库入口。
    """

    try:
        return svc.adopt_note_only(payload.ids, payload.book)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/reconcile")
def reconcile_history(
    payload: ReconcileInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """按当前地址库校正历史查询结果。

    交易所/验证者/公共地址是「关联终点」：查到即截止。但以前查询时这些地址
    还没被确认，工具把它们当普通钱包继续往下走，结果里就混进一大片下游噪音。
    人工确认（或官方名单同步）之后调用这里，用同一套口径重剪一遍历史快照。
    """

    return svc.reconcile_history(
        dry_run=payload.dry_run,
        archive_ids=payload.archive_ids,
    )


@app.post("/api/library/tidy")
def tidy_library(
    payload: TidyInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """一键整理地址库：规整多分类脏数据、把系统角色地址移出用户自建库、清掉残留交易所标记。"""

    try:
        return svc.tidy_library(dry_run=payload.dry_run)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/group-addresses")
def add_group_addresses(
    payload: GroupAddressInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """地址库清单展开后「手动添加地址」用：把地址加进当前这一组。"""

    try:
        return svc.add_group_addresses(
            payload.addresses,
            book=payload.book,
            label=payload.label,
            category=payload.category,
            note=payload.note,
            move_existing=payload.move_existing,
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/merge-groups")
def merge_groups(
    payload: MergeGroupsInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """合并群组：把几个地址库/标签组归到一个库里，可撤回。"""

    try:
        return svc.merge_address_groups(payload.sources, payload.target)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/library")
def delete_addresses(
    payload: BulkDeleteInput,
    svc: Annotated[LinkService, Depends(service)],
):
    return {"deleted": svc.remove_addresses(payload.ids)}


@app.post("/api/library/import")
async def import_library(
    request: Request,
    svc: Annotated[LinkService, Depends(service)],
):
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "upload exceeds 2 MB")
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        boundary = content_type.split("boundary=", 1)
        if len(boundary) != 2:
            raise HTTPException(422, "multipart boundary is missing")
        try:
            import email

            message = email.message_from_bytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
            )
            part = next(
                (
                    item
                    for item in message.walk()
                    if item.get_filename() is not None
                ),
                None,
            )
            if part is None:
                raise HTTPException(422, "no uploaded file")
            filename = part.get_filename() or "upload.txt"
            payload = part.get_payload(decode=True) or b""
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(422, f"invalid upload: {exc}") from exc
    else:
        filename = request.query_params.get("filename", "upload.txt")
        payload = raw
    # 目标库与分类从查询参数取（multipart 里再塞字段要改前端 FormData，
    # 查询参数更简单，也方便用 curl 复现）。
    book = request.query_params.get("book", "")
    category = request.query_params.get("category", "")
    note = request.query_params.get("note", "")
    try:
        return svc.import_upload(
            filename,
            payload.decode("utf-8-sig"),
            book=book,
            category=category,
            note=note,
        )
    except (UnicodeDecodeError, InputError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/import/preview")
async def preview_library_import(
    request: Request,
    svc: Annotated[LinkService, Depends(service)],
):
    """只解析、不入库：弹窗里先告诉用户「这份文件有多少条、要导到哪儿去」。

    ``book`` 传了的话额外算出「其中多少条已经在那个库里」——用户要的
    「导入已有地址库时只导入新增部分」得先能看到这个数，才敢点确认。
    """

    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "upload exceeds 2 MB")
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        boundary = content_type.split("boundary=", 1)
        if len(boundary) != 2:
            raise HTTPException(422, "multipart boundary is missing")
        try:
            import email

            message = email.message_from_bytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
            )
            part = next(
                (item for item in message.walk() if item.get_filename() is not None),
                None,
            )
            if part is None:
                raise HTTPException(422, "no uploaded file")
            payload = part.get_payload(decode=True) or b""
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(422, f"invalid upload: {exc}") from exc
    else:
        payload = raw

    book = request.query_params.get("book", "").strip()
    try:
        return svc.preview_import(
            payload.decode("utf-8-sig"), book=book
        )
    except (UnicodeDecodeError, InputError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.api_route("/api/library/import-exchanges", methods=["POST", "GET"])
def import_exchange_addresses(svc: Annotated[LinkService, Depends(service)]):
    try:
        return svc.sync_exchange_addresses()
    except TaostatsAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except TaostatsRateLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except TaostatsUnavailableError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.api_route("/api/library/import-validators", methods=["POST", "GET"])
def import_validator_addresses(svc: Annotated[LinkService, Depends(service)]):
    """同步 Taostats 当前活跃验证者名单：进库后作为关联终点，不再向下扩展。"""

    try:
        return svc.sync_validator_addresses()
    except TaostatsAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except TaostatsRateLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except TaostatsUnavailableError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/library/exchange-sync")
def exchange_sync_status(svc: Annotated[LinkService, Depends(service)]):
    """官方交易所名单同步状态：官方条数、最近同步时间、待提示的新增地址。"""

    return svc.exchange_sync_status()


@app.post("/api/library/exchange-sync/ack")
def exchange_sync_ack(svc: Annotated[LinkService, Depends(service)]):
    """前端已提示过新增地址，清掉待提示列表，避免每次刷新都弹。"""
    return svc.acknowledge_exchange_notice()


@app.post("/api/library/import-subnet-owners")
def import_subnet_owner_addresses(svc: Annotated[LinkService, Depends(service)]):
    """手动同步当前子网项目方地址；注销子网自动归档。"""

    try:
        watched_before = _watched_subnet_netuids()
        result = svc.sync_subnet_owner_addresses()
        changed_netuids = {
            int(item["netuid"])
            for key in ("updated_items", "restored_items")
            for item in (result.get(key) or [])
            if item.get("netuid") is not None
        }
        if EVENT_MONITOR.running and EVENT_MONITOR.context is not None:
            result["event_watch_reconcile"] = _sync_event_monitor_library_state()
            library_items = svc.library()
            restored: dict[str, int] = {}
            for netuid in sorted(changed_netuids & watched_before):
                count = _ensure_subnet_owner_watched(
                    netuid, source_items=library_items,
                )
                if count:
                    restored[str(netuid)] = count
            result["owner_watch_restored"] = restored
        return result
    except Exception as exc:
        raise HTTPException(502, f"子网项目方同步失败：{exc}") from exc


@app.get("/api/library/subnet-owner-sync")
def subnet_owner_sync_status(svc: Annotated[LinkService, Depends(service)]):
    """项目方同步状态与活跃/归档子网数量。"""

    return svc.subnet_owner_sync_status()


@app.get("/api/library/subnet-owners")
def subnet_owner_entries(
    svc: Annotated[LinkService, Depends(service)],
    include_archived: bool = Query(default=False),
):
    return {"items": svc.subnet_owner_entries(include_archived=include_archived)}


# -- 地址库自动入库（本地索引流水线） ---------------------------------------
@app.get("/api/watch/status")
def watch_status():
    """自动入库统计、待入库候选、本地索引流水线的实时状态。"""

    return WATCH.status()


@app.post("/api/watch/config")
def watch_config(payload: WatchConfigInput):
    """保存自动入库设置：阈值（默认 30 TAO）、自动入库、流水线开关。"""

    values = payload.model_dump(exclude_none=True)
    if not values:
        return WATCH.config()
    return WATCH.update_config(values)


@app.get("/api/library/auto-import-log")
def auto_import_log(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str = Query(default=""),
    book: str = Query(default=""),
    keyword: str = Query(default=""),
):
    """自动入库流水：谁触发的、多少钱、为什么归到这个库。"""

    return WATCH.logs(
        limit=limit, offset=offset, status=status, book=book, keyword=keyword
    )


@app.post("/api/library/auto-import-log/{log_id}/revoke")
def revoke_auto_import(log_id: int):
    """撤回一条自动入库：删掉自动写入的地址，并让巡检以后不再收它。"""

    try:
        return WATCH.revoke(log_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/library/auto-import-log/{log_id}/revoke-and-mark")
def revoke_and_mark(
    log_id: int,
    payload: RevokeAndMarkInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """撤回一条自动入库，并把该地址标成交易所 / 验证者 / 公共地址。

    用户原话：「点击这三个按钮默认撤回刚自动入的库并移入对应的系统库」。

    这两步必须一起做：只撤回不下结论，地址下次巡检还会被收回来；只下结论
    不撤回，`auto_import_log` 里会留一条 active 记录，看起来像「它还在库
    里」。所以合成一个接口，顺序固定为「先撤回、后下结论」——反过来会先
    被系统分类逻辑移走，再撤回时「删掉自动写入记录」那步就找不到目标。
    """

    role = (payload.role or "").strip().lower()
    if role not in {"exchange", "validator", "public"}:
        raise HTTPException(422, "role 只能是 exchange、validator 或 public")
    try:
        revoked = WATCH.revoke(log_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    address = payload.address or revoked.get("address") or ""
    if not address:
        raise HTTPException(422, "缺少地址，无法下结论")
    try:
        if role == "exchange":
            decision = svc.decide_exchange_address(address, "confirm")
        elif role == "validator":
            decision = svc.decide_validator_address(address, True)
        else:
            decision = svc.decide_public_address(address, True)
    except InputError as exc:
        # 撤回已经生效（地址已删/已拉黑），这里要如实告知，不能假装整体失败。
        raise HTTPException(
            422, f"已撤回自动入库，但标记失败：{exc}"
        ) from exc
    return {
        "ok": True,
        "role": role,
        "address": address,
        "revoked": revoked,
        "item": decision.get("item"),
        "correction": decision.get("correction"),
    }


@app.post("/api/library/auto-import-log/revoke-address")
def revoke_auto_import_address(payload: AddressNoteInput):
    """按地址撤回（该地址的所有自动入库记录）。"""

    try:
        return WATCH.revoke_address(payload.address)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/notes")
def save_address_note(
    payload: AddressNoteInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        return svc.save_note(payload.address, payload.note)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/scan")
def scan(
    svc: Annotated[LinkService, Depends(service)],
    blocks: int = Query(default=2, ge=1, le=16),
    advance: bool = Query(default=False),
):
    try:
        return svc.scan(blocks=blocks, advance=advance)
    except ScanBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        # TimeoutError 的 str() 是空串，直接拼 {exc} 会得到
        # "RPC scan failed: "，用户完全看不出原因。带上异常类型。
        raise HTTPException(502, f"RPC scan failed: {type(exc).__name__}: {exc}") from exc


@app.post("/api/relations")
def relations(payload: RelationInput, svc: Annotated[LinkService, Depends(service)]):
    if not payload.seed.strip():
        raise HTTPException(422, "seed must not be blank")
    if payload.data_source == "taostats" and not taostats_is_configured():
        raise HTTPException(400, TAOSTATS_KEY_MISSING)
    try:
        return svc.relations(
            seed=payload.seed.strip(),
            depth=payload.depth,
            min_edge_tao=payload.min_edge_tao,
            min_total_tao=payload.min_total_tao,
            min_direct_tao=payload.min_direct_tao,
            include_incoming=payload.include_incoming,
            include_outgoing=payload.include_outgoing,
            data_source=payload.data_source,
            filter_exchanges=payload.filter_exchanges,
            filter_validators=payload.filter_validators,
            filter_systems=payload.filter_systems,
            filter_publics=payload.filter_publics,
            removed_addresses=[value.strip() for value in payload.removed_addresses if value.strip()],
            prefetched_requests=payload.prefetched_requests,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/seed-profile")
def seed_profile(
    payload: SeedProfileInput, svc: Annotated[LinkService, Depends(service)]
):
    """只查起始地址画像（注册日期 / 质押次数 / 日均频次 / 余额）。

    扫描一开始先调它，左栏立刻能显示，不用等关联地址跑完。固定 2 次
    Taostats 请求（命中缓存则 0 次），返回的 ``api_requests`` 前端要回传给
    ``/api/relations``，从本轮额度里扣掉。
    """

    if not payload.seed.strip():
        raise HTTPException(422, "seed must not be blank")
    try:
        return svc.seed_profile_snapshot(
            seed=payload.seed.strip(),
            data_source=payload.data_source,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/relations/jobs")
def start_relation_job(
    request: Request,
    payload: RelationJobInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """创建分轮查询作业，进入队列按发起先后排队执行。

    多人同时点「扫描关联」时，**不再各自抢额度一起跑**：每个作业拿到一个
    排队序号，同一时刻只有队首那个在推进。否则两个作业各分一半预算、都要
    多等几轮，用户感受就是"抢额度"和"等太久"。
    """

    if not payload.seed.strip():
        raise HTTPException(422, "seed must not be blank")
    if not taostats_is_configured():
        raise HTTPException(400, TAOSTATS_KEY_MISSING)
    try:
        # 用 create_async：只入队就返回，首轮交给后台线程。
        # 否则这个接口要等第一轮的真实 API 请求跑完（十几秒到几十秒），
        # 前端点了按钮很久没反应，多人同时发起还会超时。
        job = JOBS.create_async(
            svc,
            seed=payload.seed.strip(),
            depth=payload.depth,
            min_edge_tao=payload.min_edge_tao,
            min_total_tao=payload.min_total_tao,
            min_direct_tao=payload.min_direct_tao,
            include_incoming=payload.include_incoming,
            include_outgoing=payload.include_outgoing,
            filter_exchanges=payload.filter_exchanges,
            filter_validators=payload.filter_validators,
            filter_systems=payload.filter_systems,
            filter_publics=payload.filter_publics,
            removed_addresses=[value.strip() for value in payload.removed_addresses if value.strip()],
            round_interval=payload.round_interval_seconds,
            max_rounds=payload.max_rounds,
            prefetched_requests=payload.prefetched_requests,
            # 谁发起的：决定这条查询记录归谁看（地址库仍是全机共享）。
            owner=access_mod.client_owner(request),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JOBS.payload(job)


@app.get("/api/relations/jobs/{job_id}")
def get_relation_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JOBS.payload(job)


@app.post("/api/relations/jobs/{job_id}/cancel")
def cancel_relation_job(job_id: str):
    job = JOBS.cancel(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JOBS.payload(job)


@app.get("/api/relations/queue")
def relation_queue(request: Request):
    """当前队列总览：谁在跑、谁在等、大概要等多久。

    多人共用一台主机时，用户最想知道的是「我在第几位、还要多久」。
    只靠自己的工单 payload 也能算，但前端要额外请求一次队列才能显示
    "前面还有几个人"，所以单开一个轻量接口。
    """

    me = access_mod.client_owner(request)
    return JOBS.queue_overview(me)


@app.get("/api/relations/history")
def relation_history(
    request: Request,
    svc: Annotated[LinkService, Depends(service)],
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    all_users: bool = Query(default=False),
):
    """查询记录列表：每次关联查询（含中途停止）都会留下一条。

    **默认只返回自己发起的**（外加没标过人的老记录）。多人共用一台主机时，
    别人查了什么都看得见会很别扭；要看全部（比如排查）可以显式加
    ``?all_users=true``。
    """

    owner = None if all_users else access_mod.client_owner(request)
    return svc.relation_history(limit=limit, offset=offset, owner=owner)


@app.get("/api/relations/history/{archive_id}")
def relation_history_detail(
    archive_id: int, svc: Annotated[LinkService, Depends(service)]
):
    try:
        return svc.relation_history_detail(archive_id)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/relations/history/{archive_id}/export")
def export_relation_history(
    archive_id: int,
    svc: Annotated[LinkService, Depends(service)],
    format: str = Query(default="txt", pattern="^(txt|json)$"),
    depth: int | None = Query(default=None, ge=1, le=MAX_RELATION_DEPTH),
):
    try:
        content, media_type, filename = svc.export_relation_history(
            archive_id, format, depth=depth
        )
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/relations/history/{archive_id}")
def delete_relation_history(
    archive_id: int, svc: Annotated[LinkService, Depends(service)]
):
    deleted = svc.delete_relation_history([archive_id])
    if not deleted:
        raise HTTPException(404, "history record not found")
    return {"deleted": deleted}


@app.delete("/api/relations/history")
def delete_relation_history_batch(
    payload: HistoryDeleteInput, svc: Annotated[LinkService, Depends(service)]
):
    return {"deleted": svc.delete_relation_history(payload.ids)}


@app.post("/api/relations/history/clear")
def clear_relation_history(svc: Annotated[LinkService, Depends(service)]):
    return {"deleted": svc.clear_relation_history()}


@app.post("/api/relations/sync-library")
def sync_relation_library(
    payload: RelationSyncInput, svc: Annotated[LinkService, Depends(service)]
):
    """把关联地址和发起地址去重写入地址库，已存在的地址不覆盖。"""

    try:
        return svc.sync_relation_addresses(
            seed=payload.seed.strip(),
            addresses=payload.addresses,
            include_seed=payload.include_seed,
            category=payload.category,
            label=payload.label,
            note=payload.note,
            book_name=payload.book,
            only_library_matched=payload.only_library_matched,
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.exception_handler(ValueError)
async def validation_error(_request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})
