"""巡检自动入库、强度分级、结果排序的行为验证。"""

from decimal import Decimal
from pathlib import Path

import pytest

import app.taostats as taostats
import app.watch as watch_module
from app.db import AddressLibrary, AutoImportLog, BookPairStat, Database, WatchCandidate
from app.relation import discover
from app.relation_config import LibraryEntry, LinkPolicy, TransferRecord
from app.service import LinkService
from app.settings_store import DEFAULT_WATCH_CONFIG
from app.taostats import TransferPage
from app.watch import WatchService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
LIB_ADDRESS = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
NEW_ADDRESS = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
OTHER_LIB_ADDRESS = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"
SECOND_NEW = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def build(db: Database, address: str, book: str) -> None:
    with db.session() as session:
        session.add(
            AddressLibrary(
                address=address,
                label="",
                category="其他",
                note="",
                source="manual",
                book=book,
                categories="[]",
            )
        )


def row(source, target, block, amount_tao, tx="0x1"):
    return {
        "id": tx,
        "block_number": block,
        "from": source,
        "to": target,
        "amount": str(int(Decimal(str(amount_tao)) * Decimal(1_000_000_000))),
        "transaction_hash": tx,
    }


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "watch.db")
    db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture()
def config():
    values = dict(DEFAULT_WATCH_CONFIG)
    values.update(
        {
            "threshold_tao": 30.0,
            "include_stake": False,
            "daily_budget": 10_000,
            "auto_import": True,
            "follow_auto_imported": False,
        }
    )
    return values


def fake_pages(monkeypatch, pages):
    """pages: {address: [rows...]}，每个地址每次调用都返回同一页。"""

    def fake_query(*, address=None, from_address=None, to_address=None, page=1, limit=200):
        return TransferPage(list(pages.get(address, [])), None)

    monkeypatch.setattr(watch_module, "get_transfer_page_query", fake_query)
    monkeypatch.setattr(taostats, "is_configured", lambda: True)


def library_rows(database):
    with database.session() as db:
        return db.query(AddressLibrary).all()


def test_first_check_only_sets_baseline(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    fake_pages(monkeypatch, {LIB_ADDRESS: [row(LIB_ADDRESS, NEW_ADDRESS, 900, 50)]})
    service = WatchService(database)

    result = service._check_address(LIB_ADDRESS, config)

    assert result["inserted"] == 0
    assert len(library_rows(database)) == 1  # 没有把历史对手方灌进库


def test_new_transfer_above_threshold_is_imported(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    pages = {LIB_ADDRESS: [row(LIB_ADDRESS, NEW_ADDRESS, 900, 5)]}
    fake_pages(monkeypatch, pages)
    service = WatchService(database)
    service._check_address(LIB_ADDRESS, config)  # 基线

    pages[LIB_ADDRESS] = [row(LIB_ADDRESS, NEW_ADDRESS, 1000, 45, "0xabc")]
    result = service._check_address(LIB_ADDRESS, config)

    assert result["inserted"] == 1
    rows = {item.address: item for item in library_rows(database)}
    assert NEW_ADDRESS in rows
    assert rows[NEW_ADDRESS].book == "6NCK"
    assert rows[NEW_ADDRESS].source.startswith("auto-watch")
    assert "45" in rows[NEW_ADDRESS].note

    logs = service.logs()["items"]
    assert len(logs) == 1
    assert logs[0]["address"] == NEW_ADDRESS
    assert logs[0]["trigger_address"] == LIB_ADDRESS
    assert logs[0]["book"] == "6NCK"
    assert "累计金额最多的库" in logs[0]["reason"]


def test_small_repeated_transfers_accumulate(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    pages = {LIB_ADDRESS: []}
    fake_pages(monkeypatch, pages)
    service = WatchService(database)
    service._check_address(LIB_ADDRESS, config)

    for index, block in enumerate((1000, 1010, 1020, 1030)):
        pages[LIB_ADDRESS] = [row(LIB_ADDRESS, NEW_ADDRESS, block, 10, f"0x{index}")]
        service._check_address(LIB_ADDRESS, config)
        # 每轮都要重扫，模拟同一批 10 TAO 逐笔进来

    with database.session() as db:
        candidate = db.query(WatchCandidate).filter_by(address=NEW_ADDRESS).first()
    assert Decimal(candidate.cumulative_tao) == Decimal("40")
    assert NEW_ADDRESS in {item.address for item in library_rows(database)}


def test_below_threshold_never_imports(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    pages = {LIB_ADDRESS: []}
    fake_pages(monkeypatch, pages)
    service = WatchService(database)
    service._check_address(LIB_ADDRESS, config)

    pages[LIB_ADDRESS] = [row(LIB_ADDRESS, NEW_ADDRESS, 1000, 12)]
    service._check_address(LIB_ADDRESS, config)

    assert NEW_ADDRESS not in {item.address for item in library_rows(database)}


def test_import_goes_to_book_with_larger_total(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    build(database, OTHER_LIB_ADDRESS, "DSV")
    pages = {LIB_ADDRESS: [], OTHER_LIB_ADDRESS: []}
    fake_pages(monkeypatch, pages)
    service = WatchService(database)
    service._check_address(LIB_ADDRESS, config)
    service._check_address(OTHER_LIB_ADDRESS, config)

    # 6NCK 先给 40，DSV 再给 120：最终应该归 DSV。
    pages[LIB_ADDRESS] = [row(LIB_ADDRESS, NEW_ADDRESS, 1000, 40, "0xa")]
    service._check_address(LIB_ADDRESS, config)
    assert {item.address: item.book for item in library_rows(database)}[NEW_ADDRESS] == "6NCK"

    # 已入库的地址不再重复写；换一个地址验证「多库累计取最大」。
    pages[LIB_ADDRESS] = [row(LIB_ADDRESS, SECOND_NEW, 1100, 40, "0xb")]
    pages[OTHER_LIB_ADDRESS] = [row(OTHER_LIB_ADDRESS, SECOND_NEW, 1110, 120, "0xc")]
    service._check_address(LIB_ADDRESS, config)
    service._check_address(OTHER_LIB_ADDRESS, config)

    books = {item.address: item.book for item in library_rows(database)}
    assert books[SECOND_NEW] == "DSV"


def test_revoked_address_is_not_imported_again(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    pages = {LIB_ADDRESS: []}
    fake_pages(monkeypatch, pages)
    service = WatchService(database)
    service._check_address(LIB_ADDRESS, config)
    pages[LIB_ADDRESS] = [row(LIB_ADDRESS, NEW_ADDRESS, 1000, 50)]
    service._check_address(LIB_ADDRESS, config)

    log = service.logs()["items"][0]
    service.revoke(log["id"])
    assert NEW_ADDRESS not in {item.address for item in library_rows(database)}

    pages[LIB_ADDRESS] = [row(LIB_ADDRESS, NEW_ADDRESS, 1200, 80, "0xd")]
    service._check_address(LIB_ADDRESS, config)
    assert NEW_ADDRESS not in {item.address for item in library_rows(database)}


def test_existing_library_address_is_not_duplicated(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    build(database, OTHER_LIB_ADDRESS, "DSV")
    pages = {LIB_ADDRESS: []}
    fake_pages(monkeypatch, pages)
    service = WatchService(database)
    service._check_address(LIB_ADDRESS, config)

    pages[LIB_ADDRESS] = [row(LIB_ADDRESS, OTHER_LIB_ADDRESS, 1000, 500)]
    service._check_address(LIB_ADDRESS, config)

    assert len(library_rows(database)) == 2
    with database.session() as db:
        pairs = db.query(BookPairStat).all()
    assert len(pairs) == 1
    assert Decimal(pairs[0].total_tao) == Decimal("500")


def test_watch_status_reports_progress(database, monkeypatch, config):
    build(database, LIB_ADDRESS, "6NCK")
    fake_pages(monkeypatch, {LIB_ADDRESS: []})

    def fake_config():
        return config

    monkeypatch.setattr(watch_module.settings_store, "load_watch_config", fake_config)
    service = WatchService(database)
    service.run(manual=True, max_addresses=1, config=config)
    status = service.status()

    assert status["watchable"] == 1
    assert status["config"]["threshold_tao"] == 30.0
    assert status["today_requests"] >= 1


# -- 强度分级与排序 ---------------------------------------------------------
def test_strength_levels_and_sorting():
    library = [
        LibraryEntry(address=SEED, label="seed", category="其他", book="DSV"),
        LibraryEntry(
            address="5ExchangeAddr",
            label="Binance",
            category="EXCHANGE",
            exchange_confirmed=True,
            book="",
        ),
    ]
    transfers = [
        TransferRecord("0x1", 100, SEED, "5Strong", Decimal("1500")),
        TransferRecord("0x2", 101, SEED, "5Medium", Decimal("45")),
        TransferRecord("0x3", 102, SEED, "5Weak", Decimal("2")),
        TransferRecord("0x4", 103, SEED, "5ExchangeAddr", Decimal("5")),
    ]
    report = discover(
        SEED,
        transfers,
        library=library,
        max_depth=1,
        policy=LinkPolicy(min_edge_tao=Decimal("0.1"), min_total_tao=Decimal("0.1")),
    )
    by_address = {item.address: item for item in report.related}

    assert by_address["5Strong"].strength == "极强"
    assert by_address["5Strong"].strength_score == 100
    assert by_address["5Medium"].strength == "中"
    assert by_address["5Weak"].strength == "弱"
    # 交易所永远排第一，即使金额最小。
    assert report.related[0].address == "5ExchangeAddr"
    scores = [item.strength_score for item in report.related[1:]]
    assert scores == sorted(scores, reverse=True)


def test_indirect_relation_is_downgraded():
    library = [LibraryEntry(address=SEED, label="seed", category="其他")]
    transfers = [
        TransferRecord("0x1", 100, SEED, "5Hop", Decimal("1500")),
        TransferRecord("0x2", 101, "5Hop", "5Far", Decimal("1500")),
    ]
    report = discover(
        SEED,
        transfers,
        library=library,
        max_depth=2,
        policy=LinkPolicy(min_edge_tao=Decimal("0.1"), min_total_tao=Decimal("0.1")),
    )
    by_address = {item.address: item for item in report.related}

    assert by_address["5Hop"].strength == "极强"
    assert by_address["5Far"].strength == "强"  # 间接降一档
    assert "降一档" in by_address["5Far"].strength_reason


def test_book_pair_stats_boost_same_tier():
    library = [
        LibraryEntry(address=SEED, label="seed", category="其他", book="DSV"),
        LibraryEntry(address="5Partner", label="p", category="其他", book="6NCK"),
    ]
    transfers = [TransferRecord("0x1", 100, SEED, "5Partner", Decimal("40"))]
    plain = discover(
        SEED,
        transfers,
        library=library,
        max_depth=1,
        policy=LinkPolicy(min_edge_tao=Decimal("0.1"), min_total_tao=Decimal("0.1")),
    )
    boosted = discover(
        SEED,
        transfers,
        library=library,
        max_depth=1,
        policy=LinkPolicy(min_edge_tao=Decimal("0.1"), min_total_tao=Decimal("0.1")),
        book_pair_stats={("6NCK", "DSV"): Decimal("5000")},
    )
    assert plain.related[0].strength_score < boosted.related[0].strength_score
    assert plain.related[0].strength == boosted.related[0].strength
