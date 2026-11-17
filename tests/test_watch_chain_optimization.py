"""自动入库多级传播、复合游标和状态一致性优化。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from pathlib import Path

import pytest

import app.watch as watch_module
from app.db import (
    AddressBook,
    AddressLibrary,
    AutoImportLog,
    BookPairStat,
    ChainCursor,
    Database,
    Transfer,
    WatchCandidate,
    WatchSweepState,
)
from app.service import LinkService
from app.settings_store import DEFAULT_WATCH_CONFIG
from app.watch import AUTO_REVIEWED_SOURCE, SWEEP_KEY, WatchService


OWNER = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
LEVEL1 = "5CF6Ruq6u7DoF6TYxyaFzEuviWi7tjvJtz19XgPS2b7XDCFM"
LEVEL2 = "5HCT4AarReToT1BKyLtJXJfSLs4zRS7dENnZ7iysqrqxXyV7"
LEVEL3 = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
LEVEL4 = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
OTHER = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "watch-chain.db")
    db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture()
def config():
    values = dict(DEFAULT_WATCH_CONFIG)
    values.update(
        {
            "threshold_tao": 30.0,
            "auto_import": True,
            "sweep_enabled": True,
            "candidate_window_days": 30,
        }
    )
    return values


def _library(db, address, book="链式库", source="manual", category="其他"):
    with db.session() as session:
        session.add(
            AddressLibrary(
                address=address,
                label=book,
                category=category,
                categories=f'["{category}"]',
                book=book,
                source=source,
            )
        )


def _cursor(db, block=1000):
    with db.session() as session:
        session.add(ChainCursor(rpc_url="wss://test", last_block=block))


def _transfer(db, index, frm, to, amount, block=1001, event_index=None):
    with db.session() as session:
        session.add(
            Transfer(
                event_key=f"chain-{index}",
                tx_hash=f"0xchain{index}",
                block_number=block,
                block_hash="h",
                event_index=index if event_index is None else event_index,
                from_address=frm,
                to_address=to,
                amount_rao=int(Decimal(str(amount)) * 10**9),
                amount_tao=Decimal(str(amount)),
                edge_type="tao_transfer",
            )
        )


def _library_addresses(db):
    with db.session() as session:
        return {address for (address,) in session.query(AddressLibrary.address).all()}


def test_same_sweep_propagates_a_b_c_d_chain(database, config):
    _library(database, OWNER)
    _cursor(database)
    service = WatchService(database)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, LEVEL1, "31", event_index=1)
    _transfer(database, 2, LEVEL1, LEVEL2, "32", event_index=2)
    _transfer(database, 3, LEVEL2, LEVEL3, "33", event_index=3)

    result = service.sweep_local_index(config)

    assert result["inserted"] == 3
    with database.session() as session:
        imported = {
            row.address: row.book
            for row in session.query(AddressLibrary).filter(
                AddressLibrary.address.in_([LEVEL1, LEVEL2, LEVEL3])
            )
        }
    assert imported == {
        LEVEL1: "链式库",
        LEVEL2: "链式库",
        LEVEL3: "链式库",
    }


def test_ingest_transfers_bulk_insert_is_idempotent(database):
    record = {
        "event_key": "bulk-1",
        "tx_hash": "0xbulk1",
        "block_number": 2000,
        "block_hash": "h2000",
        "event_index": 1,
        "from_address": OWNER,
        "to_address": LEVEL1,
        "amount_rao": 1_000_000_000,
        "amount_tao": Decimal("1"),
        "edge_type": "tao_transfer",
    }
    assert database.ingest_transfers([record, record]) == 1
    assert database.ingest_transfers([record]) == 0
    with database.session() as session:
        assert session.query(Transfer).count() == 1


def test_composite_cursor_keeps_remaining_events_in_same_block(
    database, config, monkeypatch
):
    monkeypatch.setattr(watch_module, "SWEEP_MAX_ROWS", 2)
    _library(database, OWNER)
    _cursor(database)
    service = WatchService(database)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, LEVEL1, "31", event_index=1)
    _transfer(database, 2, OWNER, LEVEL2, "31", event_index=2)
    _transfer(database, 3, OWNER, LEVEL3, "31", event_index=3)

    first = service.sweep_local_index(config)
    second = service.sweep_local_index(config)

    assert first["inserted"] == 2
    assert second["inserted"] == 1
    with database.session() as session:
        state = session.get(WatchSweepState, SWEEP_KEY)
        assert (state.last_block, state.last_event_index) == (1001, 3)
        assert session.query(AddressLibrary).filter(
            AddressLibrary.address.in_([LEVEL1, LEVEL2, LEVEL3])
        ).count() == 3


def test_old_sweep_cursor_migrates_to_event_aware_cursor(tmp_path):
    path = tmp_path / "old-watch.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE watch_sweep_state ("
        "key VARCHAR(40) PRIMARY KEY, last_block INTEGER NOT NULL DEFAULT 0, "
        "last_sweep_at DATETIME, scanned_total INTEGER NOT NULL DEFAULT 0, "
        "hits_total INTEGER NOT NULL DEFAULT 0, inserted_total INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO watch_sweep_state "
        "(key,last_block,scanned_total,hits_total,inserted_total) VALUES ('local-index',900,1,2,3)"
    )
    conn.commit()
    conn.close()

    database = Database(path)
    database.initialize()
    try:
        with database.session() as session:
            state = session.get(WatchSweepState, SWEEP_KEY)
            assert state.last_block == 900
            assert state.last_event_index == 2_147_483_647
    finally:
        database.engine.dispose()


def test_local_scan_triggers_immediate_sweep_with_shared_lock():
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    start = source.index("async def _local_scan_scheduler()")
    end = source.index("def service()", start)
    body = source[start:end]
    assert "WATCH_SWEEP_LOCK" in body
    assert "WATCH.sweep_local_index" in body
    assert "新块刚提交就立即撞一次地址库" in body


def test_auto_imported_address_can_be_reassigned_to_larger_book_across_ticks(
    database, config
):
    _library(database, OWNER, book="库A")
    _library(database, OTHER, book="库B")
    _cursor(database)
    service = WatchService(database)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, LEVEL1, "31", block=1001)
    service.sweep_local_index(config)
    with database.session() as session:
        assert session.query(AddressLibrary).filter_by(address=LEVEL1).one().book == "库A"

    _transfer(database, 2, OTHER, LEVEL1, "100", block=1002)
    service.sweep_local_index(config)

    with database.session() as session:
        assert session.query(AddressLibrary).filter_by(address=LEVEL1).one().book == "库B"
        log = session.query(AutoImportLog).filter_by(address=LEVEL1).one()
        assert log.book == "库B"
        assert "改判" in log.reason


def test_lowering_threshold_rechecks_existing_candidates(database, config):
    _library(database, OWNER)
    _cursor(database)
    service = WatchService(database)
    service.sweep_local_index(config)
    _transfer(database, 1, OWNER, LEVEL1, "5")
    service.sweep_local_index(config)
    assert LEVEL1 not in _library_addresses(database)

    service.update_config({"threshold_tao": 1})

    assert LEVEL1 in _library_addresses(database)


def test_candidate_window_expires_stale_pending_records(database, config):
    _library(database, OWNER)
    _cursor(database)
    service = WatchService(database)
    service.sweep_local_index(config)
    old = datetime.now(timezone.utc) - timedelta(days=31)
    with database.session() as session:
        session.add(
            WatchCandidate(
                address=LEVEL1,
                book="链式库",
                cumulative_tao=Decimal("5"),
                tx_count=1,
                last_block=999,
                trigger_address=OWNER,
                updated_at=old,
            )
        )

    result = service.sweep_local_index(config)

    assert result["expired_candidates"] == 1
    with database.session() as session:
        assert session.query(WatchCandidate).count() == 0


def test_rename_book_rewrites_candidate_log_and_pair_references(database):
    with database.session() as session:
        session.add_all(
            [
                AddressBook(name="旧库", note=""),
                AddressBook(name="目标库", note=""),
                AddressLibrary(
                    address=OWNER,
                    label="旧库",
                    category="其他",
                    categories='["其他"]',
                    book="旧库",
                    source="manual",
                ),
                WatchCandidate(
                    address=LEVEL1,
                    book="旧库",
                    cumulative_tao=Decimal("5"),
                    tx_count=1,
                    trigger_address=OWNER,
                ),
                AutoImportLog(
                    address=LEVEL1,
                    book="旧库",
                    trigger_book="旧库",
                    trigger_address=OWNER,
                    status="active",
                ),
                BookPairStat(book_a="旧库", book_b="目标库", total_tao=Decimal("3")),
            ]
        )

    LinkService(database, "wss://example.invalid").rename_address_book("旧库", "新库")

    with database.session() as session:
        assert session.query(WatchCandidate).one().book == "新库"
        log = session.query(AutoImportLog).one()
        assert (log.book, log.trigger_book) == ("新库", "新库")
        assert (session.query(BookPairStat).one().book_a, session.query(BookPairStat).one().book_b) == (
            "新库",
            "目标库",
        )


def test_dissolve_book_removes_its_pending_candidates_and_pair_stats(database):
    with database.session() as session:
        session.add_all(
            [
                AddressBook(name="旧库", note=""),
                AddressLibrary(
                    address=OWNER,
                    label="旧库",
                    category="其他",
                    categories='["其他"]',
                    book="旧库",
                    source="manual",
                ),
                WatchCandidate(
                    address=LEVEL1,
                    book="旧库",
                    cumulative_tao=Decimal("5"),
                    trigger_address=OWNER,
                ),
                BookPairStat(book_a="旧库", book_b="目标库", total_tao=Decimal("3")),
            ]
        )

    LinkService(database, "wss://example.invalid").dissolve_address_book("旧库")

    with database.session() as session:
        assert session.query(WatchCandidate).count() == 0
        assert session.query(BookPairStat).count() == 0


def test_merge_book_rewrites_pending_candidate_and_log(database):
    with database.session() as session:
        session.add_all(
            [
                AddressBook(name="库A", note=""),
                AddressBook(name="库B", note=""),
                AddressLibrary(
                    address=OWNER,
                    label="库A",
                    category="其他",
                    categories='["其他"]',
                    book="库A",
                    source="manual",
                ),
                AddressLibrary(
                    address=OTHER,
                    label="库B",
                    category="其他",
                    categories='["其他"]',
                    book="库B",
                    source="manual",
                ),
                WatchCandidate(
                    address=LEVEL1,
                    book="库A",
                    cumulative_tao=Decimal("5"),
                    trigger_address=OWNER,
                ),
                AutoImportLog(
                    address=LEVEL1,
                    book="库A",
                    trigger_book="库A",
                    trigger_address=OWNER,
                    status="active",
                ),
            ]
        )

    LinkService(database, "wss://example.invalid").merge_address_groups(
        [{"kind": "book", "name": "库A"}, {"kind": "book", "name": "库B"}],
        "合并库",
    )

    with database.session() as session:
        assert session.query(WatchCandidate).one().book == "合并库"
        log = session.query(AutoImportLog).one()
        assert (log.book, log.trigger_book) == ("合并库", "合并库")


def test_revoke_keeps_manually_changed_auto_record(database):
    _library(database, OWNER)
    with database.session() as session:
        row = AddressLibrary(
            address=LEVEL1,
            label="人工命名",
            category="大V",
            categories='["大V"]',
            book="人工库",
            source="auto-watch:taostats",
        )
        log = AutoImportLog(
            address=LEVEL1,
            book="链式库",
            trigger_book="链式库",
            trigger_address=OWNER,
            status="active",
        )
        session.add_all([row, log])
        session.flush()
        log_id = log.id

    result = WatchService(database).revoke(log_id)

    assert result["removed"] is False
    with database.session() as session:
        row = session.query(AddressLibrary).filter_by(address=LEVEL1).one()
        assert row.book == "人工库"
        assert row.category == "大V"
        assert row.source == AUTO_REVIEWED_SOURCE
        assert session.get(AutoImportLog, log_id).status == "revoked"
