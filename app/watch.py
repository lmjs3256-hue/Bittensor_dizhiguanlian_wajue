"""地址库自动入库：本地索引流水线把库里地址的新往来撞出来。

设计要点：

1. **零 API**：地址的往来数据全部来自本地索引（链上 RPC 直采进 ``transfers``
   表），这条链路不消耗任何 Taostats 额度。0.9.0 起逐地址问 API 的巡检已
   整条移除——用户的额度全部留给主动发起的关联扫描。
2. **只认「新发生」**：首次启用时从当前水位往后看，不回扫历史；之后只处理
   游标之后的新块。
3. **阈值双口径**：单笔 ≥ 阈值，或该「对手方 × 归属库」累计 ≥ 阈值，任一
   满足就入库。未达阈值的也先记账，否则小额多次永远攒不到门槛。
4. **归属库按累计金额取胜**：同一个新地址被多个库触发时，取累计金额最大
   的那个库；金额相同比笔数，仍相同则归「未归属」并在日志里写明。
5. **可撤回**：撤回过（``status='revoked'``）的地址之后不再自动入库，也不会
   被重复写进日志。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select

from . import settings_store
from .db import (
    AddressBook,
    AddressLibrary,
    AutoImportLog,
    BookPairStat,
    ChainCursor,
    Database,
    Transfer,
    WatchCandidate,
    WatchSweepState,
    iso_utc,
    utcnow,
)
from .validation import is_valid_ss58

# Subtensor 的系统地址前缀（与 relation 里的默认 policy 保持一致）。
SYSTEM_PREFIX = "5EYCAe5"
# 自动入库记录的来源标记；也是「这条是不是自动来的」的判断依据。
AUTO_SOURCE = "auto-watch:local-index"
AUTO_REVIEWED_SOURCE = "auto-watch:reviewed"
AUTO_CATEGORY = "其他"
CURSOR_END_EVENT_INDEX = 2_147_483_647
# 不处理的分类：交易所、验证者、公共地址、系统（它们要么是噪音源，要么会
# 把整库灌爆）。公共地址（PUBLIC）与交易所同性质——它和成千上万个不相关钱包
# 都有往来，顺着它扩散等于用噪音换库容量。service.py 的 SYSTEM_CATEGORIES
# 早就把它算作系统角色，这里原来漏了，导致标记为公共地址的记录仍被当成
# 触发源。
SKIP_CATEGORIES = {"EXCHANGE", "VALIDATOR", "PUBLIC", "系统", "SYSTEM"}
MAX_LOGS_PAGE = 200
# 本地索引流水线：每次最多处理的行数。宁可小步快跑——它和查询共用同一个
# SQLite 文件，一次拿太多会把库锁住几十秒。
SWEEP_MAX_ROWS = 4000
# 游标键名（WatchSweepState 只有一条记录）。
SWEEP_KEY = "local-index"
# 调度器一个 tick 的间隔（秒）。前端用它算「距下一轮还有几秒」，
# 所以必须和 main.py 的 WATCH_TICK_SECONDS 是同一个值（那边直接引用这里）。
SWEEP_TICK_SECONDS = 30


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


def _auto_row_manually_changed(row: AddressLibrary, log: AutoImportLog) -> bool:
    """自动记录是否已被人工改过库、分类、标签或系统角色。"""

    categories = {name for name in _category_names(row) if name}
    return bool(
        (row.book or "") != (log.book or "")
        or row.category != AUTO_CATEGORY
        or categories != {AUTO_CATEGORY}
        or (row.label or "").strip()
        or row.exchange_confirmed
        or row.exchange_rejected
    )


def _is_auto_owned(row: AddressLibrary | None) -> bool:
    """是否为尚未被人工接管、仍允许自动改判的自动入库记录。"""

    if row is None:
        return False
    source = str(row.source or "")
    return source.startswith("auto-watch") and source != AUTO_REVIEWED_SOURCE


class WatchService:
    """巡检引擎。所有方法都是同步的，调用方负责放进线程里跑。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        # 本地索引流水线的实时状态（进程内，供界面展示「正在跑」）。
        self._sweep_running = False
        self._sweep_started: datetime | None = None
        self._sweep_last_done: datetime | None = None
        # 心跳：一次流水线可能不到 1 秒就跑完，前端几秒轮询几乎抓不到
        # running；追平后累计数字也不涨，面板看起来像死的一样。这里记录
        # 「每一轮 tick 都来过」，让界面能持续显示轮次、最近一轮时间和
        # 距下一轮的倒计时，用户才知道它确实在工作。
        self._sweep_last_tick: datetime | None = None
        self._sweep_tick_count = 0
        self._sweep_last_duration_ms = 0
        self._sweep_last_scanned = 0
        self._sweep_last_reason = ""

    def config(self) -> dict[str, Any]:
        return settings_store.load_watch_config()

    def update_config(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(settings_store.DEFAULT_WATCH_CONFIG)
        clean = {key: value for key, value in (values or {}).items() if key in allowed}
        settings_store.save_watch_config(clean)
        config = self.config()
        if config.get("auto_import", True):
            # 阈值调低后，已有候选可能已经达标；不等下一笔转账，立即重检。
            self._recheck_pending_candidates(config)
        return config

    def _recheck_pending_candidates(self, config: dict[str, Any]) -> int:
        """按当前阈值重检已有候选，返回因此新入库的地址数。"""

        threshold = Decimal(str(config["threshold_tao"]))
        imported = 0
        with self.database.session() as db:
            library = db.scalars(select(AddressLibrary)).all()
            by_address = {row.address: row for row in library}
            valid_books = {row.book for row in library if row.book}
            valid_books.update(db.scalars(select(AddressBook.name)).all())
            candidates = db.scalars(
                select(WatchCandidate).order_by(
                    WatchCandidate.cumulative_tao.desc(),
                    WatchCandidate.id.asc(),
                )
            ).all()
            seen: set[str] = set()
            for candidate in candidates:
                if candidate.address in seen:
                    continue
                if candidate.address in by_address:
                    continue
                if candidate.book and candidate.book not in valid_books:
                    continue
                seen.add(candidate.address)
                created, row = self._import_candidate(
                    db, candidate, threshold, valid_books=valid_books
                )
                if created:
                    imported += 1
                if row is not None:
                    by_address[row.address] = row
        return imported

    def status(self) -> dict[str, Any]:
        """地址库自动入库的现状。

        0.9.0 起不再有「逐地址问 API」的巡检：地址的新往来全部由本地索引
        流水线从链上 RPC 采到的转账里撞出来，零 Taostats 额度。所以这里
        只留「入库了多少、还有哪些候选、流水线跑到哪」。
        """

        config = self.config()
        with self.database.session() as db:
            not_in_library = ~select(AddressLibrary.id).where(
                AddressLibrary.address == WatchCandidate.address
            ).exists()
            pending = db.scalar(
                select(func.count(WatchCandidate.id)).where(not_in_library)
            ) or 0
            imported = db.scalar(
                select(func.count(AutoImportLog.id)).where(AutoImportLog.status == "active")
            ) or 0
            revoked = db.scalar(
                select(func.count(AutoImportLog.id)).where(AutoImportLog.status == "revoked")
            ) or 0
            candidates = db.scalars(
                select(WatchCandidate)
                .where(not_in_library)
                .order_by(WatchCandidate.cumulative_tao.desc())
                .limit(5)
            ).all()
        return {
            "config": config,
            "pending_candidates": int(pending),
            "imported": int(imported),
            "revoked": int(revoked),
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
            # 本地索引流水线：不花 API 额度、按块撞地址库的那条路。
            "sweep": self.sweep_status(),
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
            return self._revoke_log(db, log)

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
            results = [self._revoke_log(db, row) for row in rows]
            return {
                "ok": True,
                "revoked": len(results),
                "removed": sum(1 for item in results if item["removed"]),
                "address": address,
            }

    def _revoke_log(self, db, log: AutoImportLog) -> dict[str, Any]:
        """在同一事务里撤回一条日志，并保护所有人工修改。"""

        already = log.status == "revoked"
        log.status = "revoked"
        log.revoked_at = utcnow()
        removed = False
        row = db.scalar(
            select(AddressLibrary).where(AddressLibrary.address == log.address)
        )
        # 只处理仍由自动入库持有的记录。人工改过库/分类/标签/角色的记录
        # 转成 reviewed 来源，停止自动改判，但绝不删除人工整理结果。
        if _is_auto_owned(row) and row is not None:
            manually_changed = _auto_row_manually_changed(row, log)
            if _has_manual_note(row) and not manually_changed:
                row.book = ""
                row.note = _manual_note_part(row)
                row.source = NOTE_ONLY_SOURCE
                removed = True
            elif manually_changed:
                row.source = AUTO_REVIEWED_SOURCE
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

    def sweep_local_index(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """拿本地索引里「新入库的块」去和地址库做交集，命中就直接自动入库。

        为什么需要这个：原来的巡检是**逐地址问 API**（每地址 1~2 次请求），
        754 个地址一轮约 1500 次请求，按每分钟 5~60 次要跑几十分钟到几小时。
        而本地索引是另一条完全独立的链路——由 RPC 直采，**不吃 Taostats
        API 额度**。既然链上每笔转账都已经落在 ``transfers`` 表里，就完全
        没必要为了"库里地址有新往来"再去问一次 API：直接在本地做反向查询，
        「这条新转账的 from/to 有没有在地址库里」一句话就能判断。

        用户的诉求正是这个：「巡检应该是当下地址库里已有地址，发生转账转移，
        立马入库，方便跟踪」。历史欠账（某地址几周前的旧转账）确实不重要——
        真需要时去扫描那个地址，自然会把关联挖出来。

        实现要点：

        - **游标独立**：用 ``WatchSweepState`` 记「本地索引已经处理到哪一块」，
          与逐地址巡检的 ``watch_state.last_block`` 互不干扰。两条路可以并存：
          想让某个冷门地址也过一遍，仍然可以走 API 那条。
        - **只处理新块**：首次启用时从「当前索引水位」开始，不回扫历史，
          避免把几万条旧转账一次性灌进地址库（用户明确说历史数据不重要）。
        - **同一笔只算一次**：转出方在库时按转出方归属的库累计；转入方在库时
          按转入方的库累计。两边都在库时不重复计数（选金额归属明确的一侧）。
        - **额度判断完全跳过**：这里零 API 请求，每日上限、每分钟限速都不适用。
        """

        config = config or self.config()
        # 每一轮 tick 都记心跳，包括被开关跳过的轮次：界面要能区分
        # 「关掉了」和「开着但这一轮没新块」。
        tick_started = utcnow()
        self._sweep_last_tick = tick_started
        self._sweep_tick_count += 1
        result: dict[str, Any] = {"inserted": 0}
        try:
            if not config.get("sweep_enabled", True):
                result = {"skipped": True, "reason": "sweep_disabled", "inserted": 0}
            else:
                # 「正在跑」的可见性：调度器每 30 秒调一次，一次可能处理几千行。
                # 界面靠这两个字段判断它是真在动，还是被关掉了/卡住了。
                self._sweep_running = True
                self._sweep_started = tick_started
                result = self._sweep_local_index(config)
            return result
        finally:
            self._sweep_running = False
            self._sweep_last_done = utcnow()
            self._sweep_last_duration_ms = max(
                0, int((self._sweep_last_done - tick_started).total_seconds() * 1000)
            )
            self._sweep_last_scanned = int(result.get("scanned") or 0)
            self._sweep_last_reason = str(result.get("reason") or "")

    def _sweep_local_index(self, config: dict[str, Any]) -> dict[str, Any]:
        """``sweep_local_index`` 的实际实现（外面那层只管开关与运行状态）。"""

        threshold = Decimal(str(config["threshold_tao"]))
        auto_import = bool(config.get("auto_import", True))
        with self.database.session() as db:
            state = db.get(WatchSweepState, SWEEP_KEY)
            if state is None:
                # 首次启用：只从当前水位往后再看，**不回扫历史**。
                #
                # 用户的判断是对的：「历史数据不那么重要，真需要时去扫那个
                # 地址自然会挖出来」。而全量回扫要处理 7 万多条历史转账，
                # 会把一堆几周前的旧对手方一次性灌进地址库，噪音远大于价值。
                #
                # 水位取两个来源的较大值：本地索引最大块（兜底）和
                # ``chain_cursors.last_block``（更权威——它表示"已扫到这里"，
                # 而 max(block_number) 只是"恰好有转账的最后一块"，中间可能
                # 有大段还没扫）。
                cursor = db.scalar(select(func.max(Transfer.block_number))) or 0
                for row in db.query(ChainCursor).all():
                    cursor = max(int(cursor), int(row.last_block or 0))
                state = WatchSweepState(
                    key=SWEEP_KEY,
                    last_block=int(cursor),
                    last_event_index=CURSOR_END_EVENT_INDEX,
                )
                db.add(state)
                db.flush()
                result = {
                    "initialized": True,
                    "last_block": int(cursor),
                    "scanned": 0,
                    "hits": 0,
                    "inserted": 0,
                }
                db.commit()
                return result

            since = int(state.last_block or 0)
            since_event = int(
                state.last_event_index
                if state.last_event_index is not None
                else CURSOR_END_EVENT_INDEX
            )
            window_days = max(1, int(config.get("candidate_window_days", 30)))
            cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
            expired_candidates = db.execute(
                delete(WatchCandidate).where(
                    WatchCandidate.updated_at < cutoff,
                    ~select(AddressLibrary.id)
                    .where(AddressLibrary.address == WatchCandidate.address)
                    .exists(),
                )
            ).rowcount or 0
            # 一次最多处理这么多块：调用方是 30 秒一次的调度器，宁可少而稳，
            # 也别让一次扫描把 SQLite 锁住几十秒（巡检/查询都要用同一个库）。
            rows = db.execute(
                select(
                    Transfer.from_address,
                    Transfer.to_address,
                    Transfer.amount_tao,
                    Transfer.block_number,
                    Transfer.event_index,
                    Transfer.edge_type,
                    Transfer.tx_hash,
                )
                .where(
                    or_(
                        Transfer.block_number > since,
                        (Transfer.block_number == since)
                        & (Transfer.event_index > since_event),
                    )
                )
                .order_by(Transfer.block_number.asc(), Transfer.event_index.asc())
                .limit(SWEEP_MAX_ROWS)
            ).all()
            if not rows:
                return {
                    "scanned": 0,
                    "hits": 0,
                    "inserted": 0,
                    "expired_candidates": int(expired_candidates),
                    "last_block": since,
                    "last_event_index": since_event,
                }

            if not auto_import:
                # 明确语义：关闭自动入库仍推进游标，只是不产生候选和写入。
                # 这样重新开启时不会把关闭期间积累的历史一次性灌进地址库。
                last = rows[-1]
                state.last_block = int(last.block_number)
                state.last_event_index = int(last.event_index)
                state.last_sweep_at = utcnow()
                state.scanned_total = int(state.scanned_total or 0) + len(rows)
                return {
                    "scanned": len(rows),
                    "hits": 0,
                    "inserted": 0,
                    "address_rows": [],
                    "last_block": int(last.block_number),
                    "last_event_index": int(last.event_index),
                    "reason": "auto_import_off",
                }

            library = db.scalars(select(AddressLibrary)).all()
            by_address = {row.address: row for row in library}
            valid_books = {row.book for row in library if row.book}
            valid_books.update(db.scalars(select(AddressBook.name)).all())
            candidate_map = {
                (row.address, row.book or ""): row
                for row in db.scalars(select(WatchCandidate)).all()
            }
            revoked = set(
                db.scalars(
                    select(AutoImportLog.address).where(AutoImportLog.status == "revoked")
                ).all()
            )

            inserted = 0
            hits = 0
            related_rows: dict[str, dict[str, str]] = {}
            for row in rows:
                # 库与库之间的往来统计：关联结果靠它给「同档对手方」加分。
                # 原来只有逐地址 API 巡检会写这张表，0.9.0 删掉那条链路后
                # 这里必须自己记，否则 table 永远停在旧数据上、加分失效。
                from_row = by_address.get(str(row.from_address or ""))
                to_row = by_address.get(str(row.to_address or ""))
                if from_row is not None and to_row is not None:
                    left = str(from_row.book or "")
                    right = str(to_row.book or "")
                    if left and right and left != right and from_row.address != to_row.address:
                        self._bump_pair(
                            db, left, right, Decimal(str(row.amount_tao or 0))
                        )
                sides = [
                    (str(row.from_address or ""), str(row.to_address or "")),
                    (str(row.to_address or ""), str(row.from_address or "")),
                ]
                for owner, partner in sides:
                    if not owner or not partner or owner == partner:
                        continue
                    owner_row = by_address.get(owner)
                    if owner_row is None:
                        continue  # 这一侧不在地址库，换另一侧判
                    if not partner or partner.startswith(SYSTEM_PREFIX):
                        continue
                    if not is_valid_ss58(partner):
                        continue
                    if _categories(owner_row) & SKIP_CATEGORIES:
                        continue  # 交易所/验证者/公共地址不作为触发源
                    if partner in revoked:
                        continue
                    partner_row = by_address.get(partner)
                    if partner_row is not None:
                        # 已在库的自动地址仍要观察新往来，才能跨轮改判到累计
                        # 金额更高的库；人工/系统地址则保持原结论不动。
                        if not _is_auto_owned(partner_row):
                            continue
                        if (owner_row.book or "") == (partner_row.book or ""):
                            continue
                    hits += 1
                    kind = "stake" if str(row.edge_type or "").startswith("stake") else "tao"
                    edge = {
                        "counterparty": partner,
                        "block": int(row.block_number),
                        "amount": Decimal(str(row.amount_tao or 0)),
                        "tx": str(row.tx_hash or ""),
                        "kind": kind,
                    }
                    imported, related_row = self._accumulate(
                        db,
                        partner,
                        owner_row.book or "",
                        edge,
                        threshold,
                        owner,
                        candidate_map=candidate_map,
                        valid_books=valid_books,
                    )
                    if imported:
                        inserted += 1
                    if related_row is not None:
                        # 本轮刚入库或改判的地址立刻进入已知集合，后续
                        # B→C、C→D 可在同一批内继续传播。
                        by_address[related_row.address] = related_row
                        related_rows[related_row.address] = {
                            "address": related_row.address,
                            "book": related_row.book or "",
                        }
                    # 一侧已经记过账就别再从另一侧重复累计同一笔。
                    break

            last = rows[-1]
            state.last_block = int(last.block_number)
            state.last_event_index = int(last.event_index)
            state.last_sweep_at = utcnow()
            state.scanned_total = int(state.scanned_total or 0) + len(rows)
            state.hits_total = int(state.hits_total or 0) + hits
            state.inserted_total = int(state.inserted_total or 0) + inserted
        return {
            "scanned": len(rows),
            "hits": hits,
            "inserted": inserted,
            "address_rows": list(related_rows.values()),
            "expired_candidates": int(expired_candidates),
            "last_block": int(last.block_number),
            "last_event_index": int(last.event_index),
        }

    def sweep_status(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """给界面看：本地流水线开没开、正在跑没有、处理到哪、还差多少块。"""

        cfg = config or self.config()
        enabled = bool(cfg.get("sweep_enabled", True))
        now = utcnow()
        # 距上一次 tick 的秒数 + 距下一轮的倒计时。前端据此每秒本地更新，
        # 不用等接口轮询，面板就一直有东西在动。
        if self._sweep_last_tick is None:
            heartbeat_age: float | None = None
            next_in = SWEEP_TICK_SECONDS
        else:
            heartbeat_age = max(0.0, (now - self._sweep_last_tick).total_seconds())
            next_in = max(0, int(SWEEP_TICK_SECONDS - heartbeat_age))

        with self.database.session() as db:
            state = db.get(WatchSweepState, SWEEP_KEY)
            newest = db.scalar(select(func.max(Transfer.block_number))) or 0
            base = {
                # 「开关打开了吗」与「有没有真的在动」是两件事，都要给：
                # 关了 → 界面显示"已关闭"；开着但长时间没动 → 显示"等下一轮"。
                "enabled": enabled,
                "running": bool(self._sweep_running),
                "started_at": iso_utc(self._sweep_started),
                "last_done_at": iso_utc(self._sweep_last_done),
                "index_block": int(newest),
                # 0.8.49 心跳：即使这一轮没有新块、累计数字不涨，界面也能
                # 靠轮次、最近一轮时间和倒计时证明调度器在按节奏工作。
                "last_tick_at": iso_utc(self._sweep_last_tick),
                "tick_count": self._sweep_tick_count,
                "last_duration_ms": self._sweep_last_duration_ms,
                "last_scanned": self._sweep_last_scanned,
                "last_reason": self._sweep_last_reason,
                "heartbeat_seconds_ago": (
                    round(heartbeat_age, 1) if heartbeat_age is not None else None
                ),
                "next_tick_in_seconds": next_in,
                "tick_seconds": SWEEP_TICK_SECONDS,
            }
            if state is None:
                return {
                    **base,
                    "initialized": False,
                    "last_block": 0,
                    "last_event_index": CURSOR_END_EVENT_INDEX,
                    "lag_blocks": 0,
                    "scanned_total": 0,
                    "hits_total": 0,
                    "inserted_total": 0,
                    "last_sweep_at": "",
                }
            last = int(state.last_block or 0)
            return {
                **base,
                "initialized": True,
                "last_block": last,
                "last_event_index": int(
                    state.last_event_index
                    if state.last_event_index is not None
                    else CURSOR_END_EVENT_INDEX
                ),
                "lag_blocks": max(int(newest) - last, 0),
                "scanned_total": int(state.scanned_total or 0),
                "hits_total": int(state.hits_total or 0),
                "inserted_total": int(state.inserted_total or 0),
                "last_sweep_at": iso_utc(state.last_sweep_at),
            }

    # -- 候选累计与入库 ---------------------------------------------------
    def _accumulate(
        self,
        db,
        partner: str,
        book: str,
        edge: dict[str, Any],
        threshold: Decimal,
        trigger_address: str,
        *,
        candidate_map: dict[tuple[str, str], WatchCandidate],
        valid_books: set[str],
    ) -> tuple[bool, AddressLibrary | None]:
        """累计一条新转账；返回 ``(是否新入库, 本次涉及地址行)``。"""

        key = (partner, book or "")
        candidate = candidate_map.get(key)
        if candidate is None:
            candidate = WatchCandidate(
                address=partner,
                book=book or "",
                cumulative_tao=Decimal("0"),
                first_block=edge["block"],
                last_block=edge["block"],
                trigger_address=trigger_address,
            )
            db.add(candidate)
            candidate_map[key] = candidate
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
            return False, None
        return self._import_candidate(
            db, candidate, threshold, valid_books=valid_books
        )

    def _import_candidate(
        self,
        db,
        candidate: WatchCandidate,
        threshold: Decimal,
        *,
        valid_books: set[str],
    ) -> tuple[bool, AddressLibrary | None]:
        address = candidate.address
        existing = db.scalar(select(AddressLibrary).where(AddressLibrary.address == address))
        all_rows = db.scalars(
            select(WatchCandidate).where(WatchCandidate.address == address)
        ).all()
        all_rows = [
            row
            for row in all_rows
            if not row.book or row.book in valid_books
        ]
        ranked = sorted(
            all_rows,
            key=lambda row: (
                Decimal(row.cumulative_tao or 0),
                int(row.tx_count or 0) + int(row.stake_count or 0),
                int(row.last_block or 0),
            ),
            reverse=True,
        )
        if not ranked:
            return False, None
        best = ranked[0]
        best_total = Decimal(best.cumulative_tao or 0)
        if existing is not None:
            # 已在库：人工记录一律不动；自动入库的记录允许「累计金额更高的库」改判。
            if not _is_auto_owned(existing):
                return False, None
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
            return False, existing
        if best_total < threshold:
            return False, None
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
        imported_row = AddressLibrary(
            address=address,
            label="",
            category=AUTO_CATEGORY,
            note=note,
            source=AUTO_SOURCE,
            book=best.book or "",
            categories=_encode_categories([AUTO_CATEGORY]),
        )
        db.add(imported_row)
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
        db.flush()
        return True, imported_row

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
