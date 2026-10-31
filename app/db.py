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


class Base(DeclarativeBase):
    pass


class AddressLibrary(Base):
    __tablename__ = "address_library"
    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(160), default="")
    category: Mapped[str] = mapped_column(String(16), default="WATCH", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(255), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
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
        )
        for row in transfers
    ]
