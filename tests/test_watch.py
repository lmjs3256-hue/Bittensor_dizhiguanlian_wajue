"""自动入库（本地索引流水线）的行为验证 + 强度分级排序。

0.9.0 起不再逐地址问 API：地址的新往来一律由本地索引（``transfers`` 表，
RPC 直采、零 Taostats 额度）撞出来。所以这里所有用例都走同一条路径：
先跑一轮 sweep 把游标定在当前位置，再往 ``transfers`` 里塞新转账，再跑一轮。

被删掉的那批用例（``next_targets`` / 每日预算 / 让路 / 进度窗口）测的是
已经不存在的东西，不再保留；它们的行为断言（阈值、累计、归属库、撤回、
公共地址不触发）都搬到了本地索引这条链路上，覆盖面没有丢。
"""

from decimal import Decimal

import pytest

import app.watch as watch_module
from app.db import (
    AddressLibrary,
    BookPairStat,
    Database,
    Transfer,
    WatchCandidate,
)
from app.relation import discover
from app.relation_config import LibraryEntry, LinkPolicy, TransferRecord
from app.service import LinkService
from app.settings_store import DEFAULT_WATCH_CONFIG
from app.watch import WatchService

SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
LIB_ADDRESS = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
NEW_ADDRESS = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
OTHER_LIB_ADDRESS = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"
SECOND_NEW = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def build(db: Database, address: str, book: str, category: str = "其他") -> None:
    with db.session() as session:
        session.add(
            AddressLibrary(
                address=address,
                label="",
                category=category,
                note="",
                source="manual",
                book=book,
                categories=f'["{category}"]',
            )
        )


def _transfer(db: Database, index: int, frm: str, to: str, amount, block: int) -> None:
    with db.session() as session:
        session.add(
            Transfer(
                event_key=f"k{index}",
                tx_hash=f"0x{index}",
                block_number=block,
                block_hash="h",
                event_index=index,
                from_address=frm,
                to_address=to,
                amount_rao=int(Decimal(str(amount)) * 10**9),
                amount_tao=Decimal(str(amount)),
                edge_type="tao_transfer",
            )
        )


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "watch.db")
    db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture()
def config():
    values = dict(DEFAULT_WATCH_CONFIG)
    values.update({"threshold_tao": 30.0, "auto_import": True, "sweep_enabled": True})
    return values


def library_rows(database):
    with database.session() as db:
        return db.query(AddressLibrary).all()


def library_books(database):
    return {item.address: item.book for item in library_rows(database)}


def test_first_sweep_only_sets_cursor(database, config):
    """第一轮只把游标定在当前位置，不回扫历史——用户明确说历史不重要。"""

    build(database, LIB_ADDRESS, "6NCK")
    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 50, 900)
    service = WatchService(database)

    result = service.sweep_local_index(config)

    assert result["initialized"] is True
    assert result["inserted"] == 0
    assert len(library_rows(database)) == 1  # 没有把历史对手方灌进库
    assert service.sweep_status(config)["last_block"] == 900


def test_transfer_above_threshold_imports_partner(database, config):
    build(database, LIB_ADDRESS, "6NCK")
    service = WatchService(database)
    service.sweep_local_index(config)  # 定游标

    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 45, 1000)
    result = service.sweep_local_index(config)

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


def test_small_transfers_accumulate(database, config):
    """小额多次也要能攒到门槛：每笔都低于阈值，累计过了就入库。

    注意「刚好到阈值就入库」是预期行为：入库后这个对手方已在地址库里，
    后续转账不再重复累计（否则每笔都会再进一次候选判断）。
    """

    build(database, LIB_ADDRESS, "6NCK")
    service = WatchService(database)
    service.sweep_local_index(config)

    for index, block in enumerate((1000, 1010, 1020)):
        _transfer(database, index, LIB_ADDRESS, NEW_ADDRESS, 10, block)
        service.sweep_local_index(config)

    with database.session() as db:
        candidate = db.query(WatchCandidate).filter_by(address=NEW_ADDRESS).first()
    assert Decimal(candidate.cumulative_tao) == Decimal("30")
    assert NEW_ADDRESS in library_books(database)


def test_below_threshold_never_imports(database, config):
    build(database, LIB_ADDRESS, "6NCK")
    service = WatchService(database)
    service.sweep_local_index(config)

    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 12, 1000)
    result = service.sweep_local_index(config)

    assert result["inserted"] == 0
    assert NEW_ADDRESS not in library_books(database)


def test_import_uses_book_with_larger_total(database, config):
    """同一笔对手方被多个库累计时，归到累计金额最多的库。"""

    build(database, LIB_ADDRESS, "6NCK")
    build(database, OTHER_LIB_ADDRESS, "DSV")
    service = WatchService(database)
    service.sweep_local_index(config)

    # 6NCK 只有 20（不到阈值），DSV 给 120：最终归 DSV。
    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 20, 1000)
    _transfer(database, 2, OTHER_LIB_ADDRESS, NEW_ADDRESS, 120, 1010)
    service.sweep_local_index(config)

    assert library_books(database)[NEW_ADDRESS] == "DSV"
    log = service.logs()["items"][0]
    assert "其他库累计" in log["reason"]
    assert "6NCK" in log["reason"]


def test_revoked_address_is_not_imported_again(database, config):
    build(database, LIB_ADDRESS, "6NCK")
    service = WatchService(database)
    service.sweep_local_index(config)

    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 50, 1000)
    service.sweep_local_index(config)

    log = service.logs()["items"][0]
    service.revoke(log["id"])
    assert NEW_ADDRESS not in library_books(database)

    # 撤回过就永久不再自动入库（哪怕后来又有一笔更大的）
    _transfer(database, 2, LIB_ADDRESS, NEW_ADDRESS, 80, 1100)
    service.sweep_local_index(config)
    assert NEW_ADDRESS not in library_books(database)


def test_library_to_library_transfer_is_not_imported_but_bumps_pair_stat(database, config):
    """两边都在库：不重复入库，但要记「库对往来」，关联排序靠它加分。"""

    build(database, LIB_ADDRESS, "6NCK")
    build(database, OTHER_LIB_ADDRESS, "DSV")
    service = WatchService(database)
    service.sweep_local_index(config)

    _transfer(database, 1, LIB_ADDRESS, OTHER_LIB_ADDRESS, 500, 1000)
    result = service.sweep_local_index(config)

    assert result["inserted"] == 0
    assert len(library_rows(database)) == 2
    with database.session() as db:
        pairs = db.query(BookPairStat).all()
    assert len(pairs) == 1
    assert {pairs[0].book_a, pairs[0].book_b} == {"6NCK", "DSV"}
    assert Decimal(pairs[0].total_tao) == Decimal("500")


def test_system_role_owner_is_not_a_trigger(database, config):
    """交易所/验证者/公共地址不作为触发源：它们的对手方是噪音。"""

    build(database, LIB_ADDRESS, "6NCK")
    build(database, OTHER_LIB_ADDRESS, "", category="EXCHANGE")
    service = WatchService(database)
    service.sweep_local_index(config)

    _transfer(database, 1, OTHER_LIB_ADDRESS, NEW_ADDRESS, 500, 1000)
    result = service.sweep_local_index(config)

    assert result["inserted"] == 0
    assert NEW_ADDRESS not in library_books(database)


def test_auto_import_off_skips_but_keeps_heartbeat(database, config):
    """关掉自动入库只跳过写入，游标与心跳照常，界面才不会显示「卡住」。"""

    build(database, LIB_ADDRESS, "6NCK")
    service = WatchService(database)
    service.sweep_local_index(config)

    config["auto_import"] = False
    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 50, 1000)
    result = service.sweep_local_index(config)

    assert result["reason"] == "auto_import_off"
    assert NEW_ADDRESS not in library_books(database)
    status = service.sweep_status(config)
    assert status["tick_count"] == 2
    assert status["last_reason"] == "auto_import_off"


def test_status_reports_import_counts(database, config):
    """状态接口只需要给出「入库了多少、还有哪些候选、流水线跑到哪」。"""

    build(database, LIB_ADDRESS, "6NCK")
    service = WatchService(database)
    service.sweep_local_index(config)
    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 50, 1000)
    service.sweep_local_index(config)

    status = service.status()
    assert status["imported"] == 1
    assert status["revoked"] == 0
    # 候选行留着（累计金额是判断依据），不再是「待入库」，但也不必清零。
    assert status["pending_candidates"] >= 1
    assert status["config"]["threshold_tao"] == 30.0
    assert status["sweep"]["initialized"] is True
    # 旧链路的字段不能再出现，前端已经不再读它们。
    assert "progress" not in status
    assert "pause" not in status
    assert "today_requests" not in status


# -- 入库日志直连操作：撤回并标为系统角色 -----------------------------------
def _import_one(database, config):
    """跑一轮流水线，产出恰好一条自动入库记录，返回 (service, log)。"""

    build(database, LIB_ADDRESS, "6NCK")
    service = WatchService(database)
    service.sweep_local_index(config)  # 定游标
    _transfer(database, 1, LIB_ADDRESS, NEW_ADDRESS, 45, 1000)
    service.sweep_local_index(config)
    logs = service.logs()["items"]
    assert len(logs) == 1
    return service, logs[0]


def test_revoke_returns_address_for_followup(database, config):
    """撤回要回传地址，调用方才能接着下结论。"""

    service, log = _import_one(database, config)
    result = service.revoke(log["id"])

    assert result["ok"] is True
    assert result["removed"] is True
    assert result["address"] == NEW_ADDRESS


def test_logs_carry_current_role(database, config):
    """日志条目要带地址「现在」的角色，界面据此决定给不给标记按钮。"""

    service, _log = _import_one(database, config)
    item = service.logs()["items"][0]
    assert item["in_library"] is True
    assert item["current_book"] == "6NCK"
    assert "EXCHANGE" not in [name.upper() for name in item["current_categories"]]

    LinkService(database, "wss://example.invalid").decide_public_address(NEW_ADDRESS, True)
    item = service.logs()["items"][0]
    assert "PUBLIC" in [name.upper() for name in item["current_categories"]]


def test_revoke_then_mark_sequence_moves_to_system_group(database, config):
    """撤回 → 标为验证者：地址离开原库，归到验证者。"""

    service, log = _import_one(database, config)
    svc = LinkService(database, "wss://example.invalid")

    revoked = service.revoke(log["id"])
    assert revoked["removed"] is True
    decision = svc.decide_validator_address(revoked["address"], True)

    with database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=NEW_ADDRESS).one()
        assert row.book == ""
        assert "VALIDATOR" in [name.upper() for name in watch_module._category_names(row)]
        assert row.exchange_confirmed is False
    assert decision["marked"] is True


def test_note_only_source_matches_service():
    """watch 里复制的 NOTE_ONLY_SOURCE 必须和 service 的一致。

    watch 不能 import service（会绕成环），所以常量复制了一份；这里守住。
    """

    from app import service as service_module

    assert watch_module.NOTE_ONLY_SOURCE == service_module.NOTE_ONLY_SOURCE


def test_revoke_keeps_manual_note(database, config):
    """撤回不能把用户手写的备注一起删掉：转成仅备注记录。"""

    service, log = _import_one(database, config)
    svc = LinkService(database, "wss://example.invalid")
    svc.save_note(NEW_ADDRESS, "某交易所热钱包")

    revoked = service.revoke(log["id"])
    assert revoked["removed"] is True

    with database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=NEW_ADDRESS).first()
        assert row is not None, "有人工备注的地址不该被整行删掉"
        assert row.book == ""
        assert row.note == "某交易所热钱包"
        assert not str(row.source or "").startswith("auto-watch")


def test_revoke_then_mark_keeps_manual_note(database, config):
    """撤回后再标为交易所，人工备注也还在（整条链路都不能抹掉它）。"""

    service, log = _import_one(database, config)
    svc = LinkService(database, "wss://example.invalid")
    svc.save_note(NEW_ADDRESS, "某交易所热钱包")

    revoked = service.revoke(log["id"])
    svc.decide_exchange_address(revoked["address"], "confirm")

    with database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=NEW_ADDRESS).one()
        assert row.category == "EXCHANGE"
        assert row.exchange_confirmed is True
        assert row.book == ""
        assert "热钱包" in (row.note or "")


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
