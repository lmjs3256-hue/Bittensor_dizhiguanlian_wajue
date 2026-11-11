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
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from . import settings_store
from .db import (
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
AUTO_SOURCE = "auto-watch:taostats"
AUTO_CATEGORY = "其他"
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
        return self.config()

    def status(self) -> dict[str, Any]:
        """地址库自动入库的现状。

        0.9.0 起不再有「逐地址问 API」的巡检：地址的新往来全部由本地索引
        流水线从链上 RPC 采到的转账里撞出来，零 Taostats 额度。所以这里
        只留「入库了多少、还有哪些候选、流水线跑到哪」。
        """

        config = self.config()
        with self.database.session() as db:
            pending = db.scalar(select(func.count(WatchCandidate.id))) or 0
            imported = db.scalar(
                select(func.count(AutoImportLog.id)).where(AutoImportLog.status == "active")
            ) or 0
            revoked = db.scalar(
                select(func.count(AutoImportLog.id)).where(AutoImportLog.status == "revoked")
            ) or 0
            candidates = db.scalars(
                select(WatchCandidate)
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
            elif not config.get("auto_import", True):
                result = {"skipped": True, "reason": "auto_import_off", "inserted": 0}
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
                state = WatchSweepState(key=SWEEP_KEY, last_block=int(cursor))
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
            # 一次最多处理这么多块：调用方是 30 秒一次的调度器，宁可少而稳，
            # 也别让一次扫描把 SQLite 锁住几十秒（巡检/查询都要用同一个库）。
            rows = db.execute(
                select(
                    Transfer.from_address,
                    Transfer.to_address,
                    Transfer.amount_tao,
                    Transfer.block_number,
                    Transfer.edge_type,
                    Transfer.tx_hash,
                )
                .where(Transfer.block_number > since)
                .order_by(Transfer.block_number.asc(), Transfer.event_index.asc())
                .limit(SWEEP_MAX_ROWS)
            ).all()
            if not rows:
                return {
                    "scanned": 0,
                    "hits": 0,
                    "inserted": 0,
                    "last_block": since,
                }

            library = db.scalars(select(AddressLibrary)).all()
            by_address = {row.address: row for row in library}
            revoked = set(
                db.scalars(
                    select(AutoImportLog.address).where(AutoImportLog.status == "revoked")
                ).all()
            )
            pair_counter = self._counterparty_map(db)

            inserted = 0
            hits = 0
            max_block = since
            for row in rows:
                max_block = max(max_block, int(row.block_number))
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
                    if partner in by_address or partner in revoked:
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
                    if self._accumulate(
                        db, partner, owner_row.book or "", edge, threshold, owner
                    ):
                        inserted += 1
                    # 一侧已经记过账就别再从另一侧重复累计同一笔。
                    break

            state.last_block = max(int(state.last_block or 0), max_block)
            state.last_sweep_at = utcnow()
            state.scanned_total = int(state.scanned_total or 0) + len(rows)
            state.hits_total = int(state.hits_total or 0) + hits
            state.inserted_total = int(state.inserted_total or 0) + inserted
        return {
            "scanned": len(rows),
            "hits": hits,
            "inserted": inserted,
            "last_block": max_block,
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
                "lag_blocks": max(int(newest) - last, 0),
                "scanned_total": int(state.scanned_total or 0),
                "hits_total": int(state.hits_total or 0),
                "inserted_total": int(state.inserted_total or 0),
                "last_sweep_at": iso_utc(state.last_sweep_at),
            }

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
