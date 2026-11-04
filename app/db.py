"""SQLite persistence for public transfers, library labels and index jobs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
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


# 新建库时预置的分类；用户可以再加自己的。
DEFAULT_CATEGORY_NAMES = ("大V", "群友", "其他")


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
    exchange_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    exchange_rejected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


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
