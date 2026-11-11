"""SQLite persistence for public transfers, library labels and index jobs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    """把库里读回的 naive datetime 明确标注成 UTC。

    SQLite 不保留时区，存进去的 UTC 时间读出来是 naive 的；如果直接
    ``isoformat()`` 出去就没有 ``+00:00`` 尾巴，浏览器会按“本地时间”解析，
    导致查询记录里的时间比北京时间早 8 小时，看着像记录对不上。
    """

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def to_beijing(value: datetime | None) -> datetime | None:
    """把库里的 UTC 时间换成北京时间，供导出文件名等“按天”口径使用。"""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ)


# 新建库时预置的分类；用户可以再加自己的。
DEFAULT_CATEGORY_NAMES = ("大V", "群友", "其他")
# 「系统」是预置分类：和交易所一样可作为过滤项和关联终点，但允许人工选择/修改。
SYSTEM_CLASSIFICATION = "系统"


class Base(DeclarativeBase):
    pass


class AddressCategory(Base):
    """地址库分类：用户自己维护的关系分组（大V、群友、其他…）。

    ``address_library.category`` 存的是这里的名字；EXCHANGE / VALIDATOR 属于
    系统分类，只用于“过滤交易所/验证者”，不放进这张表供用户挑选。
    """

    __tablename__ = "address_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AddressBook(Base):
    """地址库台账：一次「新建地址库并导入」登记的库名。

    地址仍然统一存在 ``address_library`` 里，靠 ``book`` 字段归组；这张表只
    存库名、备注和来源发起地址，便于列表、导出和“解散”时保留元信息。
    """

    __tablename__ = "address_books"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    seed: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SeedBook(Base):
    """外发预置地址库的安装记录。

    ``app/seed_books/*.txt`` 每个文件是一个预置标签组（文件名就是组名），
    第一次启动时装进已知地址库，并在这里记一笔。用户之后解散、改名或删掉
    其中地址都不会再被自动装回来。
    """

    __tablename__ = "seed_books"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    addresses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AddressLibrary(Base):
    __tablename__ = "address_library"
    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(160), default="")
    category: Mapped[str] = mapped_column(String(32), default="其他", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(255), default="manual")
    # 所属地址库名称；空字符串表示未归入任何自建地址库。
    book: Mapped[str] = mapped_column(String(80), default="", index=True)
    # 同一个地址可以属于多个分类；这里存 JSON 数组，category 是主分类。
    categories: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    exchange_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    exchange_rejected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ImportBatch(Base):
    """最近一次批量导入（同步 / 新建地址库 / 文件导入）的撤销快照。

    只保留最新一条：每次批量导入都会覆盖上一条；“撤回上次导入”消费后删除。
    payload 里记录本次新插入的地址、被改写记录的原始字段快照和新建的库名。
    """

    __tablename__ = "import_batch"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="import")
    summary: Mapped[str] = mapped_column(String(255), default="")
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RelationArchive(Base):
    """一次关联查询的完整快照。

    分轮作业可能跑十几分钟，用户很可能来不及导出就被下一次查询覆盖界面。
    这里把最终结果（含中途停止的部分结果）落库，历史页可以回看、重新导出，
    并把关联地址补写进地址库。
    """

    __tablename__ = "relation_archives"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    kind: Mapped[str] = mapped_column(String(16), default="job", index=True)
    seed: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="complete", index=True)
    data_source: Mapped[str] = mapped_column(String(16), default="taostats")
    depth: Mapped[int] = mapped_column(Integer, default=3)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    related_count: Mapped[int] = mapped_column(Integer, default=0)
    library_hit_count: Mapped[int] = mapped_column(Integer, default=0)
    # 完整结果 JSON；历史页的“查看结果”和导出都从这里还原。
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Transfer(Base):
    __tablename__ = "transfers"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(240), unique=True, index=True)
    tx_hash: Mapped[str] = mapped_column(String(160), default="", index=True)
    block_number: Mapped[int] = mapped_column(Integer, index=True)
    block_hash: Mapped[str] = mapped_column(String(80), default="")
    event_index: Mapped[int] = mapped_column(Integer, default=0)
    from_address: Mapped[str] = mapped_column(String(64), index=True)
    to_address: Mapped[str] = mapped_column(String(64), index=True)
    amount_rao: Mapped[int] = mapped_column(Integer)
    amount_tao: Mapped[Decimal] = mapped_column(nullable=False)
    timestamp: Mapped[str] = mapped_column(String(32), default="")
    edge_type: Mapped[str] = mapped_column(
        String(24), default="tao_transfer", nullable=False, index=True
    )
    netuid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hotkey: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_transfers_from_block", "from_address", "block_number"),
        Index("ix_transfers_to_block", "to_address", "block_number"),
        UniqueConstraint("block_number", "event_index", name="uq_transfer_chain_event"),
    )


class ScanLock(Base):
    __tablename__ = "scan_lock"

    lock_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_job: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    rpc_url: Mapped[str] = mapped_column(String(255))
    head_block: Mapped[int] = mapped_column(Integer, default=0)
    start_block: Mapped[int] = mapped_column(Integer, default=0)
    end_block: Mapped[int] = mapped_column(Integer, default=0)
    scanned_blocks: Mapped[int] = mapped_column(Integer, default=0)
    transfer_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ChainCursor(Base):
    __tablename__ = "chain_cursors"
    rpc_url: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_block: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[str] = mapped_column(String(80), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WatchSweepState(Base):
    """本地索引流水线处理到哪一块（单行表，key 固定为 ``local-index``）。

    与 :class:`WatchState` 是两套独立游标，必须分开：

    - ``WatchState.last_block`` 是已删除的「用 API 逐个地址巡检」的地址级游标
      （0.9.0 起不再写入，仅为兼容旧库保留）；
    - 这张表是「拿本地索引里的新块去撞地址库」的块级游标，现在是唯一在用的。

    分开记的原因：本地流水线不吃 API 额度、可以高频跑；而旧的逐地址巡检受额度
    约束、可能几天才轮一遍。混用一个游标会让两边互相把对方的进度推飞。
    """

    __tablename__ = "watch_sweep_state"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_block: Mapped[int] = mapped_column(Integer, default=0)
    last_sweep_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scanned_total: Mapped[int] = mapped_column(Integer, default=0)
    hits_total: Mapped[int] = mapped_column(Integer, default=0)
    inserted_total: Mapped[int] = mapped_column(Integer, default=0)


class WatchState(Base):
    """已废弃：逐地址 API 巡检的地址级游标（0.9.0 起不再写入）。

    表保留只是为了老库能打开；现在的游标是 ``watch_sweep_state.last_block``，
    按「块」推进而不是按「地址」推进——本地索引采到什么块就撞什么块，
    不需要每个地址各记一个进度。
    """

    __tablename__ = "watch_state"
    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_block: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    checked_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(255), default="")


class WatchCandidate(Base):
    """本地索引撞出来的新地址，按「对端地址 × 归属库」累加转账金额。

    只有累计金额达到阈值才会写进地址库；未达阈值的也先记账，否则「10 笔
    5 TAO」这种小额多次永远攒不到 30 TAO 的门槛。
    """

    __tablename__ = "watch_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(64), index=True)
    book: Mapped[str] = mapped_column(String(80), default="", index=True)
    cumulative_tao: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    tx_count: Mapped[int] = mapped_column(Integer, default=0)
    stake_count: Mapped[int] = mapped_column(Integer, default=0)
    first_block: Mapped[int] = mapped_column(Integer, default=0)
    last_block: Mapped[int] = mapped_column(Integer, default=0)
    trigger_address: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("address", "book", name="uq_candidate_address_book"),)


class AutoImportLog(Base):
    """自动入库流水：谁触发的、多少钱、为什么归到这个库，都能回查和撤回。"""

    __tablename__ = "auto_import_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(64), index=True)
    book: Mapped[str] = mapped_column(String(80), default="", index=True)
    trigger_address: Mapped[str] = mapped_column(String(64), default="", index=True)
    trigger_book: Mapped[str] = mapped_column(String(80), default="")
    amount_tao: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    tx_count: Mapped[int] = mapped_column(Integer, default=0)
    stake_count: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="tao")
    tx_hash: Mapped[str] = mapped_column(String(160), default="")
    first_block: Mapped[int] = mapped_column(Integer, default=0)
    last_block: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    # active = 已自动入库；revoked = 用户撤回过（之后巡检不再自动入库）
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BookPairStat(Base):
    """两个地址库之间累计发生过多少转账，用于「没有具体金额时」的排序。"""

    __tablename__ = "book_pair_stats"
    id: Mapped[int] = mapped_column(primary_key=True)
    book_a: Mapped[str] = mapped_column(String(80), index=True)
    book_b: Mapped[str] = mapped_column(String(80), index=True)
    total_tao: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    tx_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("book_a", "book_b", name="uq_book_pair"),)


class WatchRun(Base):
    """已废弃：逐地址 API 巡检的轮次记录（0.9.0 起不再写入）。

    表保留只是为了老库能正常打开、导出的同步包还能读到历史；新的自动入库
    只写 ``auto_import_log`` 与 ``watch_sweep_state``。
    """

    __tablename__ = "watch_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # running / complete / budget / rate_limited / error / stopped
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    addresses_checked: Mapped[int] = mapped_column(Integer, default=0)
    new_found: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class ApiRequest(Base):
    __tablename__ = "api_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    endpoint: Mapped[str] = mapped_column(String(255), default="")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        event.listen(self.engine, "connect", self._sqlite_config)
        self.sessions = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )

    @staticmethod
    def _sqlite_config(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        # ``create_all`` does not add columns to an existing SQLite table.
        # Keep the application upgradeable for databases created by 0.1.0.
        with self.engine.begin() as connection:
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(address_library)"))
            }
            if "exchange_confirmed" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE address_library "
                        "ADD COLUMN exchange_confirmed BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "exchange_rejected" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE address_library "
                        "ADD COLUMN exchange_rejected BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
                # Records already classified as EXCHANGE before this field was
                # introduced were manually curated or provider-synced entries.
                # New suspected addresses are not inserted into this category
                # until the user confirms them.
                connection.execute(
                    text(
                        "UPDATE address_library SET exchange_confirmed = 1 "
                        "WHERE category = 'EXCHANGE'"
                    )
                )
            if "book" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE address_library "
                        "ADD COLUMN book VARCHAR(80) NOT NULL DEFAULT ''"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_address_library_book "
                        "ON address_library (book)"
                    )
                )
            if "categories" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE address_library "
                        "ADD COLUMN categories TEXT NOT NULL DEFAULT ''"
                    )
                )
            transfer_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(transfers)"))
            }
            if "edge_type" not in transfer_columns:
                connection.execute(
                    text(
                        "ALTER TABLE transfers ADD COLUMN edge_type "
                        "VARCHAR(24) NOT NULL DEFAULT 'tao_transfer'"
                    )
                )
            if "netuid" not in transfer_columns:
                connection.execute(text("ALTER TABLE transfers ADD COLUMN netuid INTEGER"))
            if "hotkey" not in transfer_columns:
                connection.execute(
                    text(
                        "ALTER TABLE transfers ADD COLUMN hotkey "
                        "VARCHAR(64) NOT NULL DEFAULT ''"
                    )
                )
            # 0.5.0：地址库分类改成用户自定义（大V / 群友 / 其他）。
            # 旧的 WATCH / SEED / BLACKLIST 语义已经没用了，统一归到“其他”，
            # EXCHANGE / VALIDATOR 是系统分类（过滤用），保持原样。
            seeded = connection.execute(
                text("SELECT COUNT(*) FROM address_categories")
            ).scalar()
            if not seeded:
                for order, name in enumerate(DEFAULT_CATEGORY_NAMES):
                    connection.execute(
                        text(
                            "INSERT INTO address_categories (name, sort_order, builtin, created_at) "
                            "VALUES (:name, :order, 1, :created)"
                        ),
                        {"name": name, "order": order, "created": utcnow().isoformat()},
                    )
            connection.execute(
                text(
                    "UPDATE address_library SET category = '其他' "
                    "WHERE category IN ('WATCH', 'SEED', 'BLACKLIST')"
                )
            )
            # 0.6.12：预置「系统」分类（builtin，用户可选但不可删）。
            connection.execute(
                text(
                    "INSERT INTO address_categories (name, sort_order, builtin, created_at) "
                    "SELECT :name, :order, 1, :created "
                    "WHERE NOT EXISTS (SELECT 1 FROM address_categories WHERE name = :name)"
                ),
                {
                    "name": SYSTEM_CLASSIFICATION,
                    "order": len(DEFAULT_CATEGORY_NAMES),
                    "created": utcnow().isoformat(),
                },
            )

            # 0.6.14：标签默认值改成「地址后四位」。旧版同步生成的
            # 「关联同步 · 地址前缀」从历史记录里找回完整发起地址，
            # 折算成后四位，避免地址库清单里挂着一长串前缀标签。
            legacy_labels = connection.execute(
                text(
                    "SELECT DISTINCT label FROM address_library "
                    "WHERE label LIKE '关联同步 · %'"
                )
            ).scalars().all()
            for label in legacy_labels:
                prefix = str(label).split("·", 1)[-1].strip()
                if not prefix:
                    continue
                seed = connection.execute(
                    text(
                        "SELECT seed FROM relation_archives "
                        "WHERE seed LIKE :prefix LIMIT 1"
                    ),
                    {"prefix": f"{prefix}%"},
                ).scalar()
                if not seed:
                    continue
                connection.execute(
                    text("UPDATE address_library SET label = :new WHERE label = :old"),
                    {"new": str(seed)[-4:], "old": label},
                )

        self.recover_interrupted_scans()

    def recover_interrupted_scans(self) -> int:
        """收尾上次进程被强杀 / 崩溃时遗留的扫描。

        启动这一刻不可能有本进程发起的扫描在跑，所以表里凡是 ``running``
        的 ScanJob 都是上一次进程的孤儿：它永远不会自己结束，而它占着的
        ScanLock 租约会让新扫描一直撞 409（租约 8 分钟，用户会以为"扫描坏了"）。

        返回被标记为中断的任务数，便于启动日志里说清楚。
        """

        now = datetime.now(timezone.utc)
        with self.session() as db:
            orphans = db.scalars(
                select(ScanJob).where(ScanJob.status == "running")
            ).all()
            for job in orphans:
                job.status = "failed"
                job.error = "服务重启，扫描被中断"
                job.finished_at = now
            if orphans:
                lock = db.get(ScanLock, "chain-scan")
                if lock is not None:
                    lock.owner_job = None
                    lock.lease_until = now
            return len(orphans)

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.sessions()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def index_summary(self) -> dict[str, Any]:
        with self.session() as db:
            transfer_count = db.scalar(select(Transfer.id).limit(1)) is not None
            transfer_count = db.query(Transfer).count() if transfer_count else 0
            first = db.query(Transfer).order_by(Transfer.block_number.asc()).first()
            last = db.query(Transfer).order_by(Transfer.block_number.desc()).first()
            cursor = db.query(ChainCursor).order_by(ChainCursor.last_block.desc()).first()
            return {
                "transfer_count": transfer_count,
                "first_block": first.block_number if first else None,
                "last_block": last.block_number if last else None,
                "cursor_block": cursor.last_block if cursor else None,
                "cursor_hash": cursor.last_hash if cursor else None,
                "coverage": "LOCAL_INDEX",
            }

    def record_api_request(
        self,
        endpoint: str,
        *,
        status_code: int | None = None,
        success: bool = False,
    ) -> None:
        with self.session() as db:
            db.add(
                ApiRequest(
                    endpoint=endpoint,
                    status_code=status_code,
                    success=success,
                )
            )

    def api_usage_summary(
        self,
        *,
        monthly_limit: int | None = None,
        per_minute_limit: int | None = None,
    ) -> dict[str, Any]:
        """Return persisted request counts in UTC calendar periods.

        The provider's account console remains authoritative for credits. The
        application counts actual network attempts made by this instance and
        exposes the public free-plan monthly reference when configured.
        """

        now = datetime.now(timezone.utc)
        today_key = now.strftime("%Y-%m-%d")
        month_key = now.strftime("%Y-%m")
        with self.session() as db:
            total = db.query(ApiRequest).count()
            today = db.query(ApiRequest).filter(
                text("strftime('%Y-%m-%d', requested_at) = :today_key")
            ).params(today_key=today_key).count()
            month = db.query(ApiRequest).filter(
                text("strftime('%Y-%m', requested_at) = :month_key")
            ).params(month_key=month_key).count()
        remaining = None
        if monthly_limit is not None:
            remaining = max(monthly_limit - month, 0)
        return {
            "today": today,
            "month": month,
            "total": total,
            "monthly_limit": monthly_limit,
            "monthly_remaining": remaining,
            "per_minute_limit": per_minute_limit,
            "counted_at": now.isoformat(),
            "counting_scope": "本机实际发起的 Taostats 网络请求；缓存命中不计数",
        }

    def ingest_transfers(self, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0
        inserted = 0
        with self.session() as db:
            for row in records:
                entity = Transfer(
                    event_key=row["event_key"],
                    tx_hash=row["tx_hash"],
                    block_number=row["block_number"],
                    block_hash=row["block_hash"],
                    event_index=row["event_index"],
                    from_address=row["from_address"],
                    to_address=row["to_address"],
                    amount_rao=row["amount_rao"],
                    amount_tao=row["amount_tao"],
                    timestamp=row.get("timestamp", ""),
                    edge_type=row.get("edge_type", "tao_transfer"),
                    netuid=row.get("netuid"),
                    hotkey=row.get("hotkey", ""),
                )
                if db.query(Transfer.id).filter(Transfer.event_key == entity.event_key).first():
                    continue
                db.add(entity)
                inserted += 1
            db.commit()
        return inserted


def to_relation_records(transfers: list[Transfer]):
    from .relation_config import TransferRecord

    return [
        TransferRecord(
            tx_hash=row.tx_hash,
            block_number=row.block_number,
            from_address=row.from_address,
            to_address=row.to_address,
            amount_tao=Decimal(str(row.amount_tao)),
            block_hash=row.block_hash,
            timestamp=row.timestamp,
            edge_type=row.edge_type,
            netuid=row.netuid,
            hotkey=row.hotkey,
        )
        for row in transfers
    ]


# -- 外发预置地址库 -------------------------------------------------------
# 随包分发的地址清单：`app/seed_books/<组名>.txt`，每行一个 SS58 地址。
# 文件名就是标签组名（同时也是自建库名），第一次启动时导入已知地址库。
SEED_BOOK_DIR = Path(__file__).resolve().parent / "seed_books"
SEED_BOOK_SOURCE = "预置地址库"


def read_seed_addresses(path: Path) -> list[str]:
    """读一个预置地址文件：忽略空行、注释和非法地址，按原顺序去重。"""

    from .validation import is_valid_ss58

    result: list[str] = []
    try:
        text_value = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return result
    for raw_line in text_value.splitlines():
        value = raw_line.strip().strip(",").strip().strip('"').strip()
        if not value or value.startswith("#"):
            continue
        if is_valid_ss58(value) and value not in result:
            result.append(value)
    return result


def list_seed_files(directory: Path | None = None) -> list[tuple[str, Path]]:
    """列出预置地址库文件：``[(组名, 文件路径), ...]``，按组名排序。"""

    folder = Path(directory) if directory is not None else SEED_BOOK_DIR
    if not folder.is_dir():
        return []
    return [(path.stem.strip(), path) for path in sorted(folder.glob("*.txt")) if path.stem.strip()]


def install_seed_books(
    database: "Database",
    directory: Path | None = None,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """把预置地址库装进已知地址库，返回每个库的导入结果。

    默认幂等：装过的库记在 ``seed_books`` 表里，之后不再重复导入——用户解散
    或删掉其中地址后也不会被它重新装回来。

    ``force=True`` 是工具栏「重新导入预置库」用的：无视安装记录，把每个预置
    组里**当前地址库里还没有的**地址补回来（解散过的库会重新建出来）。已经存在
    的地址一律跳过，与人工维护过的记录冲突时不覆盖分类 / 标签 / 备注。
    """

    results: list[dict[str, Any]] = []
    files = list_seed_files(directory)
    if not files:
        return results
    with database.session() as db:
        installed = {row.name for row in db.scalars(select(SeedBook)).all()}
        for name, path in files:
            if name in installed and not force:
                continue
            addresses = read_seed_addresses(path)
            if not addresses:
                continue
            existing: set[str] = set()
            for index in range(0, len(addresses), 400):
                chunk = addresses[index : index + 400]
                existing.update(
                    db.scalars(
                        select(AddressLibrary.address).where(
                            AddressLibrary.address.in_(chunk)
                        )
                    ).all()
                )
            missing = [address for address in addresses if address not in existing]
            book = db.scalar(select(AddressBook).where(AddressBook.name == name))
            created = book is None
            if created:
                db.add(AddressBook(name=name, note="外发包预置地址库"))
            elif not force:
                # 已有同名地址库（用户自己建的）不去改动它，只记一笔，避免以后重复导入。
                missing = []
            for address in missing:
                db.add(
                    AddressLibrary(
                        address=address,
                        label=name,
                        category=DEFAULT_CATEGORY_NAMES[-1],
                        note="",
                        source=SEED_BOOK_SOURCE,
                        book=name,
                        exchange_confirmed=False,
                        exchange_rejected=False,
                    )
                )
            record = db.get(SeedBook, name)
            if record is None:
                db.add(SeedBook(name=name, addresses=len(missing)))
            else:
                record.addresses = (0 if force else record.addresses) + len(missing)
                record.created_at = utcnow()
            db.flush()
            results.append(
                {
                    "name": name,
                    "total": len(addresses),
                    "inserted": len(missing),
                    "skipped": len(addresses) - len(missing),
                    "created": created,
                    # 供调用方写撤回快照用，接口层返回前会摘掉。
                    "new_addresses": missing,
                }
            )
    return results


def seed_book_status(directory: Path | None = None) -> dict[str, Any]:
    """预置地址库文件概况：有几个组、总共多少个地址。"""

    files = list_seed_files(directory)
    total = 0
    for _name, path in files:
        total += len(read_seed_addresses(path))
    return {"groups": len(files), "addresses": total}
