"""地址库巡检：定期检查库内地址的新转账，把新出现的对手方自动入库。

设计要点（对应 0.7.0 的需求）：

1. **只认「新发生」**：第一次巡检某个地址时只记基线块高（``last_block``），
   不把它的历史对手方一次性灌进地址库；之后每轮只取基线之后的转账。
2. **阈值双口径**：单笔 ≥ 阈值，或该「对手方 × 归属库」累计 ≥ 阈值，任一
   满足就入库。未达阈值的也先记账，否则小额多次永远攒不到门槛。
3. **归属库按累计金额取胜**：同一个新地址被多个库触发时，取累计金额最大
   的那个库；金额相同比笔数，仍相同则归「未归属」并在日志里写明。
4. **可撤回**：撤回过（``status='revoked'``）的地址之后不再自动入库，也不会
   被重复写进日志。
5. **额度优先**：每处理完一个地址就落一次进度，请求预算用完就停、第二天
   接着轮转；限速时安静退出本次 tick，不抛错给用户。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select

from . import settings_store, taostats
from .crawl import stake_counterparty
from .db import (
    AddressLibrary,
    AutoImportLog,
    BookPairStat,
    Database,
    WatchCandidate,
    WatchRun,
    WatchState,
    iso_utc,
    utcnow,
)
from .validation import is_valid_ss58
from .taostats import (
    TaostatsAuthError,
    TaostatsRateLimitError,
    TaostatsUnavailableError,
    get_stake_transfer_page,
    get_transfer_page_query,
    requests_per_minute,
)

# Subtensor 的系统地址前缀（与 relation 里的默认 policy 保持一致）。
SYSTEM_PREFIX = "5EYCAe5"
# 自动入库记录的来源标记；也是「这条是不是自动来的」的判断依据。
AUTO_SOURCE = "auto-watch:taostats"
AUTO_CATEGORY = "其他"
# 巡检不处理的分类：交易所、验证者、公共地址、系统（它们要么额度黑洞，要么
# 是噪音源）。公共地址（PUBLIC）与交易所同性质——它和成千上万个不相关钱包
# 都有往来，巡检它等于拿额度换噪音，且会把整库灌爆。service.py 的
# SYSTEM_CATEGORIES 早就把它算作系统角色，这里原来漏了，导致标记为公共地址的
# 记录仍被排进巡检队列。
SKIP_CATEGORIES = {"EXCHANGE", "VALIDATOR", "PUBLIC", "系统", "SYSTEM"}
MAX_LOGS_PAGE = 200
# 本进程启动时刻：用来判断一个 running 轮次是本进程在推，还是上次进程
# 被关掉后留下的（后者就是「断点续传」的状态，必须让用户看见）。
PROCESS_STARTED_AT = datetime.now(timezone.utc)


def _categories(row: AddressLibrary) -> set[str]:
    raw = getattr(row, "categories", "") or ""
    values: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                values = [str(item) for item in parsed]
        except (TypeError, ValueError):
            values = []
    return {str(row.category or "").upper(), *[value.upper() for value in values]}


def _encode_categories(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


# 自动入库时系统写进 note 的前缀。用户自己写的备注永远不会以它开头，
# 撤回时据此区分「系统说明」和「人工角色说明」。
AUTO_NOTE_PREFIX = "自动入库"
# 与 ``service.NOTE_ONLY_SOURCE`` 必须一致（那边是备注专用来源标记）。
# watch 不能 import service（service 里没有 watch，但 main 同时导入两者，
# 反向依赖会绕成环），所以这里复制一份常量并加测试守护。
NOTE_ONLY_SOURCE = "relation-note"


def _manual_note_part(row: AddressLibrary) -> str:
    """从 note 里挑出人工写的那部分（去掉系统自动写的说明）。

    自动入库的 note 形如「自动入库 · 与 6NCK 库累计 45 TAO（1 笔）」，
    用户后来补充的备注由 ``save_note`` 整体覆盖写入，所以多数情况下一行
    就够。这里只兜「系统行 + 人工行」混在一起的历史数据。
    """

    note = (row.note or "").strip()
    if not note:
        return ""
    lines = [line.strip() for line in note.splitlines() if line.strip()]
    kept = [line for line in lines if not line.startswith(AUTO_NOTE_PREFIX)]
    return "\n".join(kept)


def _has_manual_note(row: AddressLibrary) -> bool:
    return bool(_manual_note_part(row))


def _category_names(row: AddressLibrary) -> list[str]:
    """地址的全部分类（保留原始写法，主分类排最前），用于界面展示。

    ``_categories`` 返回的是大写集合（用于和 ``SKIP_CATEGORIES`` 比对），
    直接拿去显示会丢掉用户自建分类的原始写法（如「群友」）和顺序。
    """

    names: list[str] = []
    primary = str(row.category or "").strip()
    if primary:
        names.append(primary)
    raw = getattr(row, "categories", "") or ""
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            for item in parsed:
                name = str(item).strip()
                if name and name not in names:
                    names.append(name)
    return names


class WatchService:
    """巡检引擎。所有方法都是同步的，调用方负责放进线程里跑。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        # 最近一次 tick 的让路判定：调度器每 30 秒写一次，status() 读给前端。
        self._pause: dict[str, Any] = {
            "paused": False,
            "reason": "",
            "resume_in_seconds": 0,
            "job_id": "",
            "active_jobs": 0,
            "stalled_jobs": 0,
        }
        # 调度器心跳：进度窗口用它区分「正在推进」和「停在上次断点」。
        self._progress_heartbeat: dict[str, Any] = {"at": None, "paused": False}

    # -- 巡检让路 ---------------------------------------------------------
    def set_pause(self, state: dict[str, Any]) -> None:
        """记录调度器这一次的让路判定，供 ``/api/watch/status`` 展示。"""

        self._pause = dict(state)

    def pause_payload(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """给前端一份可直接显示的中文说明，文案只在这里维护。

        传入 ``state`` 就按它实时生成（status 路由用它，倒计时才准）；
        不传则用调度器上次 tick 记下的快照。
        """

        current = dict(self._pause if state is None else state)
        reason = current.get("reason", "")
        if reason == "running":
            detail = (
                f"手动查询进行中，巡检已让路（避免抢走每分钟 "
                f"{self._per_minute()} 次配额）"
            )
        elif reason == "cooldown":
            detail = (
                f"手动查询已结束，冷却 {current.get('resume_in_seconds', 0)} 秒后继续巡检"
            )
        elif reason == "stalled":
            detail = (
                f"有 {current.get('stalled_jobs', 1)} 个查询任务超过 10 分钟没有进度，"
                "已按僵死处理并恢复巡检"
            )
        else:
            detail = ""
        return {**current, "detail": detail}

    # -- 配置 -------------------------------------------------------------
    @staticmethod
    def _per_minute() -> int:
        """当前每分钟额度，仅用于拼提示文案；读不到就退回免费档 5。"""

        try:
            return int(requests_per_minute())
        except Exception:
            return 5

    def _rate_limit_note(self) -> str:
        return f"Taostats 限速（每分钟 {self._per_minute()} 次），本轮稍后自动继续。"

    def addresses_per_tick(self, config: dict[str, Any] | None = None) -> int:
        """一个 tick 推进几个地址。

        ``addresses_per_tick`` 为 0 时按每分钟额度自动算：每个地址约 2 次
        请求，30 秒一个 tick 即半个窗口，所以「每分钟额度的一半÷2」是安全上限。
        免费档 5 次/分钟 → 1 个；升级到 60 → 最多 15 个，但实际夹在 1~10，
        避免一次 tick 把额度全吃光、把手动查询挤掉（巡检本来就要让路）。
        """

        cfg = config if config is not None else self.config()
        try:
            fixed = int(cfg.get("addresses_per_tick") or 0)
        except (TypeError, ValueError):
            fixed = 0
        if fixed > 0:
            return max(1, min(fixed, 50))
        try:
            per_minute = int(requests_per_minute())
        except Exception:
            per_minute = 5
        # 半个窗口的额度 / 每地址 2 次请求
        auto = max(1, (per_minute // 2) // 2)
        return min(auto, 10)

    def config(self) -> dict[str, Any]:
        return settings_store.load_watch_config()

    def update_config(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(settings_store.DEFAULT_WATCH_CONFIG)
        clean = {key: value for key, value in (values or {}).items() if key in allowed}
        settings_store.save_watch_config(clean)
        return self.config()

    # -- 状态统计 ---------------------------------------------------------
    def _today_requests(self) -> int:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with self.database.session() as db:
            return int(
                db.scalar(
                    select(func.coalesce(func.sum(WatchRun.requests), 0)).where(
                        WatchRun.started_at >= start
                    )
                )
                or 0
            )

    def watchable_count(self) -> int:
        config = self.config()
        return len(self._watchable_addresses(config))

    @staticmethod
    def _should_watch(row: AddressLibrary, config: dict[str, Any]) -> bool:
        """这个地址该不该进巡检队列（唯一判据，别在别处另写一套）。

        三条排除：

        1. 系统角色（交易所 / 验证者 / 公共地址 / 系统）——汇聚型地址，
           查它等于拿额度换噪音；
        2. 仅备注记录（``relation-note``，不在任何库）——用户只是给扫描结果
           里某个地址写了句角色说明，那不是"入库"，自然不该被巡检。撤回自动
           入库时如果保留人工备注，记录会转成这种形态，靠这一条挡住；
        3. 自动入库的地址（默认）——否则会顺着转账一路扩散把库撑爆。
        """

        if _categories(row) & SKIP_CATEGORIES:
            return False
        if str(row.source or "").startswith(NOTE_ONLY_SOURCE):
            return False
        if not config["follow_auto_imported"] and str(row.source or "").startswith(
            "auto-watch"
        ):
            return False
        return True

    def _watchable_addresses(
        self, config: dict[str, Any], db: Any = None
    ) -> list[str]:
        """当前该巡检的地址集合。

        每次都从 ``address_library`` 现算，**不缓存**：库里新增地址、就地改
        分类（标成交易所/验证者/公共地址）、或撤回自动入库，下一轮立刻生效。
        """

        if db is not None:
            rows = db.scalars(select(AddressLibrary)).all()
        else:
            with self.database.session() as session:
                rows = session.scalars(select(AddressLibrary)).all()
        return [
            row.address for row in rows if self._should_watch(row, config)
        ]

    def _cycle_start(self, db: Any = None) -> datetime | None:
        """本轮的起点时间。

        一轮巡检会被拆成多条 ``watch_runs``：撞每日上限停一次、重启服务再
        起一条，每条都从 0 计数。若直接拿最新那条的 ``addresses_checked``
        当进度，进度条每被打断就归零一次——用户看到的就是「明明查了 700 个
        又变回 0%」。

        所以按「周期」算：从最新一条往回走，连续的非 ``complete`` 轮次都算
        同一周期，遇到第一条 ``complete`` 时把它的 ``started_at`` 当周期起点
        （那一条本身就是本轮的开头）。真正的完整一轮跑完后，下一条新轮次
        才会开启新周期，进度自然重置。
        """

        def _load(session):
            return session.scalars(
                select(WatchRun).order_by(WatchRun.id.desc()).limit(50)
            ).all()

        if db is not None:
            runs = _load(db)
        else:
            with self.database.session() as session:
                runs = _load(session)
        for row in runs:
            if row.status == "complete":
                started = row.started_at
                if started is not None and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                return started
        # 一条完整的都没有（刚装好、或一直在被打断）：取最早那条的起点。
        if not runs:
            return None
        oldest = runs[-1].started_at
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return oldest

    def _progress(
        self, config: dict[str, Any], run: WatchRun | None, db: Any = None
    ) -> dict[str, Any]:
        """本轮进度：应查多少、已查多少、还剩多少、预计多久。

        前端「进度窗口」只消费这个结构。以前界面上只有「已查 N 个」，
        分母缺失，用户无法判断是刚开工还是快完了。
        """

        addresses = self._watchable_addresses(config, db)
        total = len(addresses)
        # 「已查」按周期内的游标统计，而不是单条 run 的计数器——否则撞上限、
        # 重启之后进度会归零（见 _cycle_start 的说明）。
        cycle_start = self._cycle_start(db)
        checked = 0
        checked_once = 0
        if addresses:
            set_checked = select(func.count(WatchState.address)).where(
                WatchState.address.in_(addresses),
                WatchState.last_checked_at.is_not(None),
            )
            set_cycle = set_checked
            if cycle_start is not None:
                set_cycle = set_checked.where(
                    WatchState.last_checked_at >= cycle_start
                )
            if db is not None:
                checked = int(db.scalar(set_cycle) or 0)
                checked_once = int(db.scalar(set_checked) or 0)
            else:
                with self.database.session() as session:
                    checked = int(session.scalar(set_cycle) or 0)
                    checked_once = int(session.scalar(set_checked) or 0)
        remaining = max(total - min(checked, total), 0)
        per_tick = self.addresses_per_tick(config)
        try:
            per_minute = int(requests_per_minute())
        except Exception:
            per_minute = 5
        # 30 秒一个 tick，每 tick 推进 per_tick 个地址（每地址约 2 次请求）。
        seconds_per_address = 30 / max(per_tick, 1)
        percent = round(min(checked / total, 1) * 100, 1) if total else 0.0
        # -- 心跳：判断「真的在跑」还是「停在上一轮的断点上」 --------------
        # 手动查询让路时进度本来就会停几秒到一分钟，不能算异常；超过 3 个
        # tick（90 秒）没有任何推进、且不是因为让路，才是卡住。
        running = run is not None and run.status == "running"
        last_tick_at = self._progress_heartbeat.get("at")
        idle_seconds = 0
        stale = False
        if running and last_tick_at is not None:
            idle = (utcnow() - last_tick_at).total_seconds()
            idle_seconds = int(max(idle, 0))
            stale = idle > 90 and not self._progress_heartbeat.get("paused")
        resumed = False
        if running and run is not None:
            started = run.started_at
            if started is not None:
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                # 这一轮是上一次进程留下的 → 本轮就是「从断点接着跑」。
                resumed = started < PROCESS_STARTED_AT and checked > 0
        return {
            "total": total,
            "checked": checked,
            "remaining": remaining,
            "checked_once": checked_once,
            "never_checked": max(total - checked_once, 0),
            "per_tick": per_tick,
            "requests_per_minute": per_minute,
            "seconds_per_address": round(seconds_per_address, 1),
            "eta_seconds": int(round(remaining * seconds_per_address)),
            "percent": percent,
            "running": running,
            "resumed": resumed,
            "idle_seconds": idle_seconds,
            "stale": stale,
        }

    def mark_tick(self, *, paused: bool = False) -> None:
        """调度器每次尝试推进时打一个心跳，进度窗口据此判断「还活着」。"""

        self._progress_heartbeat = {"at": utcnow(), "paused": bool(paused)}

    def status(self) -> dict[str, Any]:
        config = self.config()
        with self.database.session() as db:
            last_run = db.scalars(select(WatchRun).order_by(WatchRun.id.desc()).limit(1)).first()
            pending = db.scalar(select(func.count(WatchCandidate.id))) or 0
            imported = db.scalar(
                select(func.count(AutoImportLog.id)).where(AutoImportLog.status == "active")
            ) or 0
            revoked = db.scalar(
                select(func.count(AutoImportLog.id)).where(AutoImportLog.status == "revoked")
            ) or 0
            checked = db.scalar(
                select(func.count(WatchState.address)).where(
                    WatchState.last_checked_at.is_not(None)
                )
            ) or 0
            candidates = db.scalars(
                select(WatchCandidate)
                .order_by(WatchCandidate.cumulative_tao.desc())
                .limit(5)
            ).all()
            recent = db.scalars(select(WatchRun).order_by(WatchRun.id.desc()).limit(8)).all()
            progress = self._progress(config, last_run, db)
        return {
            "config": config,
            "today_requests": self._today_requests(),
            # 巡检是否因手动查询而暂停：前端据此显示「已让路 / 冷却 N 秒」。
            "pause": self.pause_payload(),
            "watchable": progress["total"],
            "checked_once": int(checked),
            # 进度窗口的完整数据：分母、剩余、每地址耗时、ETA 都在这里。
            "progress": progress,
            "pending_candidates": int(pending),
            "imported": int(imported),
            "revoked": int(revoked),
            "last_run": self._run_dict(last_run),
            "runs": [self._run_dict(row) for row in recent],
            "top_candidates": [
                {
                    "address": row.address,
                    "book": row.book,
                    "cumulative_tao": str(row.cumulative_tao),
                    "tx_count": row.tx_count,
                    "stake_count": row.stake_count,
                }
                for row in candidates
            ],
            "api_configured": taostats.is_configured(),
        }

    @staticmethod
    def _run_dict(row: WatchRun | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "manual": bool(row.manual),
            "addresses_checked": row.addresses_checked,
            "new_found": row.new_found,
            "inserted": row.inserted,
            "requests": row.requests,
            "error": row.error,
            "started_at": iso_utc(row.started_at),
            "finished_at": iso_utc(row.finished_at),
        }

    # -- 入库日志 ---------------------------------------------------------
    def logs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        book: str = "",
        keyword: str = "",
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_LOGS_PAGE))
        offset = max(0, int(offset))
        stmt = select(AutoImportLog)
        if status in {"active", "revoked"}:
            stmt = stmt.where(AutoImportLog.status == status)
        if book:
            stmt = stmt.where(AutoImportLog.book == book)
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(
                (AutoImportLog.address.like(like))
                | (AutoImportLog.trigger_address.like(like))
            )
        with self.database.session() as db:
            total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            rows = db.scalars(
                stmt.order_by(AutoImportLog.id.desc()).limit(limit).offset(offset)
            ).all()
            # 顺带带上这批地址「现在」在地址库里的分类与结论，前端据此显示
            # 「当前角色」并避免对已下过结论的地址重复给按钮。
            wanted = [row.address for row in rows]
            current: dict[str, AddressLibrary] = {}
            if wanted:
                for entry in db.scalars(
                    select(AddressLibrary).where(AddressLibrary.address.in_(wanted))
                ).all():
                    current[entry.address] = entry
        return {
            "total": total,
            "items": [
                {
                    "id": row.id,
                    "address": row.address,
                    "book": row.book,
                    "trigger_address": row.trigger_address,
                    "trigger_book": row.trigger_book,
                    "amount_tao": str(row.amount_tao),
                    "tx_count": row.tx_count,
                    "stake_count": row.stake_count,
                    "kind": row.kind,
                    "tx_hash": row.tx_hash,
                    "first_block": row.first_block,
                    "last_block": row.last_block,
                    "reason": row.reason,
                    "status": row.status,
                    "created_at": iso_utc(row.created_at),
                    "revoked_at": iso_utc(row.revoked_at),
                    # 这个地址「现在」在地址库里是什么角色（可能已经被标成
                    # 交易所/验证者/公共地址，也可能已被手工删掉）。
                    "current_category": (
                        current[row.address].category
                        if row.address in current
                        else ""
                    ),
                    "current_categories": (
                        _category_names(current[row.address])
                        if row.address in current
                        else []
                    ),
                    "current_book": (
                        getattr(current[row.address], "book", "") or ""
                        if row.address in current
                        else ""
                    ),
                    "in_library": row.address in current,
                }
                for row in rows
            ],
        }

    def revoke(self, log_id: int) -> dict[str, Any]:
        """撤回一条自动入库：删掉自动写入的记录并把地址拉黑。"""

        with self.database.session() as db:
            log = db.get(AutoImportLog, int(log_id))
            if log is None:
                raise ValueError("入库日志不存在")
            already = log.status == "revoked"
            log.status = "revoked"
            log.revoked_at = utcnow()
            removed = False
            row = db.scalar(
                select(AddressLibrary).where(AddressLibrary.address == log.address)
            )
            # 只删「自动来的、没被人工改过分类」的记录；人工动过的保留，只拉黑。
            #
            # 例外：自动入库之后用户又写了备注的，不能整行删——那会把人工写的
            # 角色说明一起抹掉（撤回只是"不再自动收它"，不是"忘掉我写过什么"）。
            # 这种情况改成"转成仅备注记录"：清 book、清自动入库的 note 与来源，
            # 保留人工备注，界面上归到「仅备注（未归入地址库）」组。
            if row is not None and str(row.source or "").startswith("auto-watch"):
                if not row.exchange_confirmed and not row.exchange_rejected:
                    if _has_manual_note(row):
                        row.book = ""
                        row.note = _manual_note_part(row)
                        row.source = NOTE_ONLY_SOURCE
                        removed = True
                    else:
                        db.delete(row)
                        removed = True
            return {
                "ok": True,
                "already": already,
                "removed": removed,
                # 调用方（「撤回并标为交易所/验证者/公共地址」）要拿它继续下结论，
                # 否则还得再查一次库，撤完就查不到地址了。
                "address": log.address,
            }

    def revoke_address(self, address: str) -> dict[str, Any]:
        """按地址撤回：把该地址所有自动入库记录标成已撤回。"""

        address = (address or "").strip()
        if not is_valid_ss58(address):
            raise ValueError("地址格式不正确")
        with self.database.session() as db:
            rows = db.scalars(
                select(AutoImportLog).where(
                    AutoImportLog.address == address, AutoImportLog.status == "active"
                )
            ).all()
            for row in rows:
                row.status = "revoked"
                row.revoked_at = utcnow()
            return {"ok": True, "revoked": len(rows)}

    # -- 巡检主流程 -------------------------------------------------------
    def next_targets(
        self,
        limit: int,
        config: dict[str, Any] | None = None,
        *,
        before: datetime | None = None,
    ):
        """取本轮还没查过的地址，最久没查的排最前。

        ``before`` 是本轮开始时间：只挑 ``last_checked_at`` 早于它（或从未查过）
        的地址，这样一轮跑完自然收敛，不需要另外维护游标。
        """

        config = config or self.config()
        with self.database.session() as db:
            rows = db.scalars(select(AddressLibrary)).all()
            states = {
                state.address: state.last_checked_at
                for state in db.scalars(select(WatchState)).all()
            }
        cutoff = None
        if before is not None:
            cutoff = before if before.tzinfo else before.replace(tzinfo=timezone.utc)

        # 必须**先过滤再排序**。以前的写法是先按「最久没查的优先」取前 60 行，
        # 再在里面过滤掉交易所/验证者——而库里 1194 条验证者大多从未巡检过
        # （last_checked_at 为 NULL），恰好全排在最前面，60 个名额被它们占满
        # 后过滤干净，于是 next_targets 返回空，巡检每轮都「瞬间完成」。
        eligible: list[tuple[datetime | None, int, AddressLibrary]] = []
        for row in rows:
            if not self._should_watch(row, config):
                continue
            checked = states.get(row.address)
            if checked is not None and checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            if cutoff is not None and checked is not None and checked >= cutoff:
                # 本轮已经查过它了，让给别的地址，保证一轮收敛。
                continue
            eligible.append((checked, row.id, row))
        # 从未查过的排最前（None 最小），其余按最久没查升序。
        eligible.sort(key=lambda item: (item[0] is not None, item[0] or 0, item[1]))
        return [item[2] for item in eligible[: max(limit, 0)]]

    def run(
        self,
        *,
        manual: bool = False,
        max_addresses: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """跑一轮巡检：处理到「库内地址都过一遍」或「请求预算用完」为止。

        一轮可能跨几个小时（地址数 × 每地址 1~2 次请求 ÷ 每分钟额度）。
        进度实时写在 ``watch_runs`` 上，地址游标写在 ``watch_state`` 上，
        所以调度器下一个 tick、甚至服务重启后都能从断点接着推进，
        不会重头再来。
        """

        config = config or self.config()
        if not taostats.is_configured():
            return {
                "status": "no_api_key",
                "error": "未配置 Taostats API key，巡检未执行。",
                "addresses_checked": 0,
                "inserted": 0,
            }
        if not config["enabled"] and not manual:
            return {"status": "disabled", "addresses_checked": 0, "inserted": 0}

        run_id, counters, started = self._ensure_run(manual=manual, config=config)
        if run_id is None:
            return {
                "status": "waiting",
                "error": "",
                "addresses_checked": 0,
                "inserted": 0,
            }
        limit = max_addresses if max_addresses is not None else 10**9
        return self._advance(run_id, counters, started, limit, config)

    def _ensure_run(
        self, *, manual: bool, config: dict[str, Any]
    ) -> tuple[int | None, dict[str, int], datetime]:
        """取进行中的一轮；没有就按间隔决定是否开新一轮。"""

        with self.database.session() as db:
            running = db.scalars(
                select(WatchRun)
                .where(WatchRun.status == "running")
                .order_by(WatchRun.id.desc())
                .limit(1)
            ).first()
            if running is not None:
                counters = {
                    "checked": int(running.addresses_checked or 0),
                    "new_found": int(running.new_found or 0),
                    "inserted": int(running.inserted or 0),
                    "requests": int(running.requests or 0),
                }
                started = running.started_at or utcnow()
                return running.id, counters, started
        if not manual and self._interval_waiting(config):
            return None, {}, utcnow()
        with self.database.session() as db:
            run = WatchRun(manual=manual, status="running")
            db.add(run)
            db.flush()
            return (
                run.id,
                {"checked": 0, "new_found": 0, "inserted": 0, "requests": 0},
                run.started_at or utcnow(),
            )

    def _advance(
        self,
        run_id: int,
        counters: dict[str, int],
        started: datetime,
        limit: int,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        budget = int(config["daily_budget"])
        status = "running"
        error = ""
        done = 0
        try:
            # 每日上限对所有轮次生效——包括手动点的那次。以前这里是
            # ``not manual``（「用户点了就让他跑完」），但手动开启的轮次
            # ``manual=True`` 会被后续所有调度器 tick 继承，等于永久绕过每日
            # 上限，一次误点就能把整月额度打光。用户想多跑就去调高界面上的
            # 「每日请求上限」。
            while done < limit:
                if budget and self._today_requests() >= budget:
                    status = "budget"
                    break
                targets = self.next_targets(1, config, before=started)
                if not targets:
                    status = "complete"
                    break
                result = self._check_address(targets[0].address, config)
                counters["requests"] += result["requests"]
                if not result.get("rate_limited"):
                    # 被限速的地址游标没动（下次还会再查），不计入本轮进度。
                    counters["checked"] += 1
                counters["inserted"] += result["inserted"]
                counters["new_found"] += result["new_found"]
                done += 1
                self._save_progress(run_id, counters, "running")
                if result.get("rate_limited"):
                    status = "running" if done < limit else "rate_limited"
                    error = self._rate_limit_note()
                    break
        except TaostatsRateLimitError:
            status = "running"
            error = self._rate_limit_note()
        except (TaostatsAuthError, TaostatsUnavailableError) as exc:
            status = "error"
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 巡检失败不能影响服务
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        if status == "running" and not error:
            # 单步推进（调度器 tick）不结束本轮；只有跑完或出错才收尾。
            self._save_progress(run_id, counters, "running")
            return {
                "status": "running",
                "error": "",
                "addresses_checked": counters["checked"],
                "new_found": counters["new_found"],
                "inserted": counters["inserted"],
                "requests": counters["requests"],
                "run_id": run_id,
            }
        self._save_progress(run_id, counters, status, error=error)
        return {
            "status": status,
            "error": error,
            "addresses_checked": counters["checked"],
            "new_found": counters["new_found"],
            "inserted": counters["inserted"],
            "requests": counters["requests"],
            "run_id": run_id,
        }

    def tick(self, max_addresses: int = 1, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """调度器用的小步推进：只处理少量地址，限速就安静退出。"""

        config = config or self.config()
        if not config["enabled"]:
            return {"status": "disabled", "addresses_checked": 0}
        if not taostats.is_configured():
            return {"status": "no_api_key", "addresses_checked": 0}
        return self.run(manual=False, max_addresses=max_addresses, config=config)

    def _interval_waiting(self, config: dict[str, Any]) -> bool:
        """上一轮跑完还不到设定间隔时，先不开新一轮。"""

        with self.database.session() as db:
            last = db.scalars(
                select(WatchRun)
                .where(WatchRun.status.in_(("complete", "budget")))
                .order_by(WatchRun.id.desc())
                .limit(1)
            ).first()
            if last is None:
                return False
            finished = last.finished_at or last.started_at
            if finished is None:
                return False
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            interval = timedelta(minutes=int(config["interval_minutes"]))
            return utcnow() - finished < interval

    def _save_progress(
        self,
        run_id: int,
        counters: dict[str, int],
        status: str,
        *,
        error: str = "",
    ) -> None:
        try:
            with self.database.session() as db:
                run = db.get(WatchRun, run_id)
                if run is None:
                    return
                run.addresses_checked = counters["checked"]
                run.new_found = counters["new_found"]
                run.inserted = counters["inserted"]
                run.requests = counters["requests"]
                run.error = error
                if status != "running":
                    run.status = status
                    run.finished_at = utcnow()
        except Exception:  # noqa: BLE001 - 进度写失败不该打断巡检
            pass

    # -- 单个地址 ---------------------------------------------------------
    def _check_address(self, address: str, config: dict[str, Any]) -> dict[str, Any]:
        threshold = Decimal(str(config["threshold_tao"]))
        requests = 0
        new_found = 0
        inserted = 0
        edges: list[dict[str, Any]] = []
        rate_limited = False

        with self.database.session() as db:
            row = db.scalar(
                select(AddressLibrary).where(AddressLibrary.address == address)
            )
            book = (row.book if row is not None else "") or ""
            pair_counter = self._counterparty_map(db)
            revoked = set(
                db.scalars(
                    select(AutoImportLog.address).where(AutoImportLog.status == "revoked")
                ).all()
            )
            state = db.get(WatchState, address)
            baseline = state.last_block if state is not None else 0
            # 首次巡检（last_checked_at 为空）只建立基线，不把历史对手方灌进库。
            initialized = state is not None and state.last_checked_at is not None

        # 1) TAO 转账
        try:
            page = get_transfer_page_query(address=address, page=1, limit=200)
            requests += 1
            self.database.record_api_request("/v1/transfers", success=True)
            edges.extend(self._edges_from_transfers(page.rows, address))
        except TaostatsRateLimitError:
            rate_limited = True
        except (TaostatsAuthError, TaostatsUnavailableError):
            raise
        except Exception:  # noqa: BLE001 - 单地址失败不影响整轮
            pass

        # 2) 代币（stake）转移
        if config["include_stake"] and not rate_limited:
            try:
                stake_page = get_stake_transfer_page(address, 1)
                requests += 1
                self.database.record_api_request("/v1/subnets/stake-events", success=True)
                edges.extend(self._edges_from_stakes(stake_page.rows, address))
            except TaostatsRateLimitError:
                rate_limited = True
            except (TaostatsAuthError, TaostatsUnavailableError):
                raise
            except Exception:  # noqa: BLE001
                pass

        max_block = max((edge["block"] for edge in edges), default=0)
        with self.database.session() as db:
            state = db.get(WatchState, address)
            if state is None:
                state = WatchState(address=address)
                db.add(state)
            if rate_limited:
                # 被限速时数据不完整，两件事都不能做：
                # 1) 不能入库——下次重查时 baseline 没推进，同一批转账会被
                #    再累计一次，金额翻倍可能把不该入库的地址灌进来；
                # 2) 不能写 last_checked_at——那等于把没查完的地址标记成
                #    「已巡检」，要等下一整轮（约 2 天）才会再查，被静默跳过。
                # 只留个原因，游标原地不动，下一 tick 重试同一个地址。
                state.last_error = "限速暂停，稍后重试"
            else:
                if initialized and edges:
                    fresh = [edge for edge in edges if edge["block"] > baseline]
                    for edge in fresh:
                        partner = edge["counterparty"]
                        if not partner or partner == address:
                            continue
                        if partner.startswith(SYSTEM_PREFIX):
                            continue
                        if not is_valid_ss58(partner):
                            continue
                        in_library = partner in pair_counter
                        partner_book, partner_source = pair_counter.get(partner, ("", ""))
                        if not in_library and partner not in revoked:
                            new_found += 1
                        if in_library:
                            self._bump_pair(db, book, partner_book, edge["amount"])
                            # 自动入库的地址会继续累计：后面若另一个库的累计更大，
                            # 就按「累计金额最多的库」改判归属。
                            if (
                                config["auto_import"]
                                and str(partner_source or "").startswith("auto-watch")
                            ):
                                self._accumulate(
                                    db, partner, book, edge, threshold, address
                                )
                            continue
                        if partner in revoked:
                            continue
                        if config["auto_import"]:
                            if self._accumulate(
                                db, partner, book, edge, threshold, address
                            ):
                                inserted += 1
                state.checked_count = int(state.checked_count or 0) + 1
                state.last_checked_at = utcnow()
                state.last_error = ""
                # 首次巡检只建立基线；后续只推进到见过的最大块。
                state.last_block = max(int(state.last_block or 0), max_block)
        return {
            "requests": requests,
            "new_found": new_found,
            "inserted": inserted,
            "rate_limited": rate_limited,
        }

    @staticmethod
    def _edges_from_transfers(rows: list[dict[str, Any]], address: str) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for item in rows:
            try:
                source = str(item["from"]).strip()
                target = str(item["to"]).strip()
                block = int(item["block_number"])
                raw = Decimal(str(item["amount"]))
                amount = (raw / Decimal(1_000_000_000)).quantize(Decimal("0.000000001"))
                tx_hash = str(item.get("transaction_hash") or item.get("id") or "")
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            if not amount.is_finite() or amount < 0:
                continue
            partner = target if source == address else source if target == address else ""
            if not partner:
                continue
            edges.append(
                {"counterparty": partner, "block": block, "amount": amount, "tx": tx_hash, "kind": "tao"}
            )
        return edges

    @staticmethod
    def _edges_from_stakes(rows: list[dict[str, Any]], address: str) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for item in rows:
            if str(item.get("is_transfer") or "").lower() not in {"true", "1"}:
                continue
            try:
                coldkey = str(item["coldkey"]).strip()
                block = int(item["block_number"])
                raw = Decimal(str(item["amount"]))
                amount = (raw / Decimal(1_000_000_000)).quantize(Decimal("0.000000001"))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            partner = stake_counterparty(item)
            action = str(item.get("action") or "").lower()
            if not partner or action not in {"stake", "unstake"}:
                continue
            if coldkey != address:
                continue
            edges.append(
                {
                    "counterparty": partner,
                    "block": block,
                    "amount": amount,
                    "tx": str(item.get("extrinsic_id") or item.get("id") or ""),
                    "kind": "stake",
                }
            )
        return edges

    @staticmethod
    def _counterparty_map(db) -> dict[str, tuple[str, str]]:
        """地址 → (所属地址库, 来源)，用于判断是否已在库、是否为自动入库。"""

        rows = db.scalars(select(AddressLibrary)).all()
        return {row.address: ((row.book or ""), (row.source or "")) for row in rows}

    # -- 候选累计与入库 ---------------------------------------------------
    def _accumulate(
        self,
        db,
        partner: str,
        book: str,
        edge: dict[str, Any],
        threshold: Decimal,
        trigger_address: str,
    ) -> bool:
        """累计一条新转账；达到阈值就入库。返回是否新入库。"""

        candidate = db.scalar(
            select(WatchCandidate).where(
                WatchCandidate.address == partner, WatchCandidate.book == book
            )
        )
        if candidate is None:
            candidate = WatchCandidate(
                address=partner,
                book=book,
                cumulative_tao=Decimal("0"),
                first_block=edge["block"],
                last_block=edge["block"],
                trigger_address=trigger_address,
            )
            db.add(candidate)
        elif not candidate.trigger_address:
            candidate.trigger_address = trigger_address
        candidate.cumulative_tao = Decimal(candidate.cumulative_tao or 0) + edge["amount"]
        candidate.last_block = max(int(candidate.last_block or 0), edge["block"])
        if edge["kind"] == "stake":
            candidate.stake_count = int(candidate.stake_count or 0) + 1
        else:
            candidate.tx_count = int(candidate.tx_count or 0) + 1
        if candidate.first_block in (0, None):
            candidate.first_block = edge["block"]
        db.flush()
        total = Decimal(candidate.cumulative_tao or 0)
        if total < threshold:
            return False
        return self._import_candidate(db, candidate, threshold)

    def _import_candidate(self, db, candidate: WatchCandidate, threshold: Decimal) -> bool:
        address = candidate.address
        existing = db.scalar(select(AddressLibrary).where(AddressLibrary.address == address))
        all_rows = db.scalars(
            select(WatchCandidate).where(WatchCandidate.address == address)
        ).all()
        ranked = sorted(
            all_rows,
            key=lambda row: (Decimal(row.cumulative_tao or 0), int(row.tx_count or 0)),
            reverse=True,
        )
        best = ranked[0]
        best_total = Decimal(best.cumulative_tao or 0)
        if existing is not None:
            # 已在库：人工记录一律不动；自动入库的记录允许「累计金额更高的库」改判。
            if not str(existing.source or "").startswith("auto-watch"):
                return False
            if best.book and best.book != (existing.book or "") and best_total >= threshold:
                old_book = existing.book or "未归属"
                existing.book = best.book
                existing.note = (
                    f"自动入库 · 与 {best.book} 库累计 {best_total.normalize():f} TAO"
                    f"（{int(best.tx_count or 0) + int(best.stake_count or 0)} 笔）"
                )
                log = db.scalar(
                    select(AutoImportLog)
                    .where(
                        AutoImportLog.address == address,
                        AutoImportLog.status == "active",
                    )
                    .order_by(AutoImportLog.id.desc())
                    .limit(1)
                )
                if log is not None:
                    log.book = best.book
                    log.amount_tao = best_total
                    log.reason = (
                        f"{log.reason}；改判：{best.book} 累计 {best_total.normalize():f} TAO"
                        f" 已超过 {old_book}"
                    )
            return False
        if best_total < threshold:
            return False
        others = [row for row in ranked[1:] if Decimal(row.cumulative_tao or 0) > 0]
        reason_parts = [
            f"由 {best.book or '未归属'} 库的 {best.trigger_address} 触发",
            f"{best.book or '未归属'} 累计 {best_total.normalize():f} TAO"
            f"（{best.tx_count} 笔转账 / {best.stake_count} 笔代币转移）",
        ]
        if others:
            detail = "，".join(
                f"{row.book or '未归属'} {Decimal(row.cumulative_tao or 0).normalize():f} TAO"
                for row in others[:3]
            )
            reason_parts.append(f"其他库累计：{detail}")
        reason = "；".join(reason_parts) + " → 按累计金额最多的库入库"
        note = (
            f"自动入库 · 与 {best.book or '未归属'} 库累计 {best_total.normalize():f} TAO"
            f"（{best.tx_count + best.stake_count} 笔）"
        )
        db.add(
            AddressLibrary(
                address=address,
                label="",
                category=AUTO_CATEGORY,
                note=note,
                source=AUTO_SOURCE,
                book=best.book or "",
                categories=_encode_categories([AUTO_CATEGORY]),
            )
        )
        db.add(
            AutoImportLog(
                address=address,
                book=best.book or "",
                trigger_address=best.trigger_address,
                trigger_book=best.book or "",
                amount_tao=best_total,
                tx_count=int(best.tx_count or 0),
                stake_count=int(best.stake_count or 0),
                kind=self._kind(best),
                tx_hash="",
                first_block=int(best.first_block or 0),
                last_block=int(best.last_block or 0),
                reason=reason,
                status="active",
            )
        )
        return True

    @staticmethod
    def _kind(row: WatchCandidate) -> str:
        if int(row.stake_count or 0) and int(row.tx_count or 0):
            return "mixed"
        if int(row.stake_count or 0):
            return "stake"
        return "tao"

    @staticmethod
    def _bump_pair(db, book_a: str, book_b: str, amount: Decimal) -> None:
        if not book_a or not book_b:
            return
        left, right = sorted([book_a, book_b])
        stat = db.scalar(
            select(BookPairStat).where(
                BookPairStat.book_a == left, BookPairStat.book_b == right
            )
        )
        if stat is None:
            stat = BookPairStat(book_a=left, book_b=right, total_tao=Decimal("0"), tx_count=0)
            db.add(stat)
        stat.total_tao = Decimal(stat.total_tao or 0) + amount
        stat.tx_count = int(stat.tx_count or 0) + 1

    # -- 供关联排序使用 ---------------------------------------------------
    def pair_stats(self) -> dict[tuple[str, str], Decimal]:
        with self.database.session() as db:
            rows = db.scalars(select(BookPairStat)).all()
        return {(row.book_a, row.book_b): Decimal(row.total_tao or 0) for row in rows}
