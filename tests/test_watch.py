"""巡检自动入库、强度分级、结果排序的行为验证。"""

from decimal import Decimal
from pathlib import Path

import pytest

import app.taostats as taostats
import app.watch as watch_module
from app.db import (
    AddressLibrary,
    AutoImportLog,
    BookPairStat,
    Database,
    WatchCandidate,
    WatchState,
)
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


# -- 巡检范围：库变了，下一轮自动跟着变 -------------------------------------
def test_watch_scope_follows_library_changes(database, monkeypatch, config):
    """巡检不是固定清单：新入库的地址立刻进队列，被标成交易所的立刻出队。"""

    build(database, LIB_ADDRESS, "6NCK")
    fake_pages(monkeypatch, {LIB_ADDRESS: [], OTHER_LIB_ADDRESS: []})
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    service = WatchService(database)
    assert service.watchable_count() == 1

    # 1) 新增一个地址 → 自动进入巡检范围
    build(database, OTHER_LIB_ADDRESS, "DSV")
    assert service.watchable_count() == 2

    # 2) 原地把它标成验证者 → 立刻退出巡检范围
    with database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=OTHER_LIB_ADDRESS).one()
        row.category = "VALIDATOR"
        row.categories = '["VALIDATOR"]'
    assert service.watchable_count() == 1


def test_next_targets_survives_many_never_checked_skipped_rows(
    database, monkeypatch, config
):
    """大量「从未巡检过」的验证者不能把候选名额占满。

    历史 bug：先按「最久没查的优先」取前 60 行，再过滤掉交易所/验证者。
    真实库里 1194 条验证者大多 last_checked_at 为 NULL，恰好排在最前，
    60 个名额被占满后全部过滤掉 → next_targets 返回空 → 巡检每轮「瞬间
    完成」，界面上永远 0 个，还会被误认为「巡检正常运行」。
    """

    # 60 条从未查过的验证者（正好压满旧实现的名额）
    for index in range(60):
        address = f"5Val{index}".ljust(48, "v")
        build(database, address, "")
        with database.session() as db:
            row = db.query(AddressLibrary).filter_by(address=address).one()
            row.category = "VALIDATOR"
            row.categories = '["VALIDATOR"]'
    # 真正该巡检的地址排在后面
    build(database, LIB_ADDRESS, "6NCK")
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    service = WatchService(database)

    targets = service.next_targets(3, config)
    assert [item.address for item in targets] == [LIB_ADDRESS]


def test_next_targets_skips_already_checked_in_this_run(database, monkeypatch, config):
    """before 之前查过的要跳过，保证一轮能收敛，不会来回查同一个。"""

    from datetime import timedelta

    build(database, LIB_ADDRESS, "6NCK")
    build(database, OTHER_LIB_ADDRESS, "6NCK")
    fake_pages(monkeypatch, {})
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    service = WatchService(database)
    service.run(manual=True, max_addresses=1, config=config)

    with database.session() as db:
        run = db.query(watch_module.WatchRun).order_by(
            watch_module.WatchRun.id.desc()
        ).first()
        started = run.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=watch_module.timezone.utc)

    # 同一次 pass（before = 本轮开始）只应剩下没查过的那个
    rest = service.next_targets(5, config, before=started)
    assert len(rest) == 1
    # 下一轮（before 推后一小时）两个都会回来
    again = service.next_targets(5, config, before=started + timedelta(hours=1))
    assert len(again) == 2


def test_public_address_is_skipped(database, monkeypatch, config):
    """公共地址和交易所同性质，必须排除，否则巡检会被噪音灌满。"""

    build(database, LIB_ADDRESS, "6NCK")
    build(database, OTHER_LIB_ADDRESS, "")
    with database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=OTHER_LIB_ADDRESS).one()
        row.category = "PUBLIC"
        row.categories = '["PUBLIC"]'
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    service = WatchService(database)

    assert service.watchable_count() == 1
    targets = service.next_targets(10, config)
    assert [item.address for item in targets] == [LIB_ADDRESS]


def test_next_targets_never_picks_skipped_categories(database, monkeypatch, config):
    """next_targets 是实际下发的队列，光过滤 watchable_count 不够。"""

    build(database, LIB_ADDRESS, "6NCK")
    for index, category in enumerate(["EXCHANGE", "VALIDATOR", "PUBLIC"]):
        address = f"5{index}Skipped{'x' * 40}"[:48]
        build(database, address, "")
        with database.session() as db:
            row = db.query(AddressLibrary).filter_by(address=address).one()
            row.category = category
            row.categories = f'["{category}"]'
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    service = WatchService(database)

    assert [item.address for item in service.next_targets(10, config)] == [LIB_ADDRESS]


# -- 进度窗口与断点续跑 -----------------------------------------------------
def test_progress_has_denominator_and_remaining(database, monkeypatch, config):
    """界面上必须能算出「还剩多少」，所以 status 要给出完整进度结构。"""

    for index in range(4):
        address = f"5Progress{index}".ljust(48, "z")
        build(database, address, "6NCK")
    fake_pages(monkeypatch, {})
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    monkeypatch.setattr(watch_module, "requests_per_minute", lambda: 60)
    service = WatchService(database)

    progress = service.status()["progress"]
    assert progress["total"] == 4
    assert progress["checked"] == 0
    assert progress["remaining"] == 4
    assert progress["never_checked"] == 4
    assert progress["eta_seconds"] > 0
    assert progress["per_tick"] >= 1

    service.run(manual=True, max_addresses=2, config=config)
    progress = service.status()["progress"]
    assert progress["checked"] == 2
    assert progress["remaining"] == 2
    assert progress["percent"] == 50.0


def test_interrupted_run_resumes_from_cursor(database, monkeypatch, config):
    """中断（进程被杀 / 预算用完）后，新一轮从断点继续，不从头再查一遍。"""

    addresses = [f"5Resume{index}".ljust(48, "y") for index in range(3)]
    for address in addresses:
        build(database, address, "6NCK")
    fake_pages(monkeypatch, {})
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    service = WatchService(database)

    # 第一段：只推 1 个地址，模拟跑到一半被中断
    service.run(manual=True, max_addresses=1, config=config)
    with database.session() as db:
        first_checked = db.query(WatchState).filter(
            WatchState.last_checked_at.is_not(None)
        ).count()
    assert first_checked == 1

    # 第二段：同一轮还没结束，接着推，不能把刚查过的那个再查一遍
    result = service.run(manual=True, max_addresses=2, config=config)
    with database.session() as db:
        checked_addresses = [
            row.address
            for row in db.query(WatchState).all()
            if row.last_checked_at is not None
        ]
    assert result["status"] == "running"
    assert len(checked_addresses) == 3
    assert len(set(checked_addresses)) == 3


def test_progress_does_not_reset_when_run_is_split_by_budget(
    database, monkeypatch, config
):
    """被每日上限/重启拆成多条 run 时，进度不能归零。

    真实场景：754 个地址的一轮，撞上限停一次 → 新 run 从 0 计数；重启服务
    又开一条 run，还从 0 计数。若进度直接读最新那条 run 的 addresses_checked，
    用户看到的就是「明明查了几百个，又变回 0%」。
    """

    effective = dict(config)
    effective["daily_budget"] = 2  # 每个地址 1 次请求，够查 2 个就停
    for index in range(6):
        build(database, f"5Split{index}".ljust(48, "s"), "6NCK")
    fake_pages(monkeypatch, {})
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: effective
    )
    service = WatchService(database)

    first = service.run(manual=True, max_addresses=6, config=effective)
    assert first["status"] == "budget"
    progress_one = service.status()["progress"]
    assert progress_one["checked"] >= 1

    # 调高上限继续跑：会出现一条新的 run，计数器从 0 开始
    effective["daily_budget"] = 10_000
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: effective
    )
    service.run(manual=True, max_addresses=6, config=effective)
    progress_two = service.status()["progress"]

    # 关键断言：进度只增不减，没有因为新 run 而从 0 重新开始
    assert progress_two["checked"] >= progress_one["checked"]
    assert progress_two["checked"] >= 2


def test_daily_budget_also_caps_manual_run(database, monkeypatch, config):
    """手动点的轮次也受每日上限约束，否则一次误点就把整月额度打光。"""

    for index in range(4):
        build(database, f"5Budget{index}".ljust(48, "b"), "6NCK")
    fake_pages(monkeypatch, {})
    tight = dict(config)
    tight["daily_budget"] = 1  # 只允许 1 次请求
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: tight
    )
    service = WatchService(database)

    result = service.run(manual=True, max_addresses=4, config=tight)
    assert result["status"] == "budget"
    # 撞上限前最多查 1 个地址（首个地址发 1 次请求就正好用满）
    assert result["addresses_checked"] <= 1


def test_progress_marks_resumed_run(database, monkeypatch, config):
    """跨越进程重启的轮次要标出来，用户才知道是「接上上次」而不是新开局。"""

    from datetime import timedelta

    build(database, LIB_ADDRESS, "6NCK")
    fake_pages(monkeypatch, {LIB_ADDRESS: []})
    monkeypatch.setattr(
        watch_module.settings_store, "load_watch_config", lambda: config
    )
    service = WatchService(database)
    service.run(manual=True, max_addresses=1, config=config)

    # 把这一轮的开始时间推到本进程启动之前，等价于「上次进程留下的轮次」
    with database.session() as db:
        run = db.query(watch_module.WatchRun).order_by(
            watch_module.WatchRun.id.desc()
        ).first()
        run.started_at = watch_module.PROCESS_STARTED_AT - timedelta(hours=2)
        run.status = "running"
        run.finished_at = None
        db.flush()

    progress = service.status()["progress"]
    assert progress["running"] is True
    assert progress["resumed"] is True


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
