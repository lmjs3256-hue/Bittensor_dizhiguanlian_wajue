"""巡检「本地索引优先」：能不花 Taostats 额度就不花，但不能因此漏数据。

背景：一轮完整巡检每个地址 2 次请求，754 个地址约 1500 次/轮，全走 API
确实浪费。本地索引由链上 RPC 直采，不消耗 Taostats 配额，所以只要本地能
覆盖「该地址上一轮之后的新转账」，就没必要再问 API。

但这里有个必须守住的底线：**本地没数据 ≠ 没有新转账**。以下任一情况都必须
退回 API，否则会静默漏掉真实转账（漏了就再也不会被补上）：
1. 本地索引不存在 / 没有任何块区间；
2. baseline 早于本地区间起点（中间那段本地根本没有）；
3. 该地址从未在本地索引里出现过。
"""

from decimal import Decimal

import pytest

import app.taostats as taostats
import app.watch as watch_module
from app.db import (
    AddressLibrary,
    ChainCursor,
    Database,
    ScanJob,
    Transfer,
    WatchState,
)
from app.settings_store import DEFAULT_WATCH_CONFIG
from app.taostats import TransferPage
from app.watch import WatchService

LIB = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
NEW = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
OTHER = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "local-first.db")
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
            "prefer_local_index": True,
        }
    )
    return values


def _library(db, address=LIB, book="6NCK"):
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


def _cursor(db, last_block):
    with db.session() as session:
        session.add(ChainCursor(rpc_url="wss://test", last_block=last_block))


def _scan(db, start, end):
    """造一条成功的扫描任务记录——本地索引的"覆盖"完全由它定义。"""
    with db.session() as session:
        session.add(
            ScanJob(
                rpc_url="wss://test",
                status="complete",
                start_block=start,
                end_block=end,
                head_block=end,
                scanned_blocks=end - start + 1,
            )
        )


def _local_transfer(db, from_addr, to_addr, block, tao, edge_type="tao_transfer"):
    with db.session() as session:
        session.add(
            Transfer(
                event_key=f"k{block}-{from_addr[:6]}-{to_addr[:6]}-{tao}",
                tx_hash=f"0x{block}",
                block_number=block,
                from_address=from_addr,
                to_address=to_addr,
                amount_rao=int(Decimal(str(tao)) * Decimal(1_000_000_000)),
                amount_tao=Decimal(str(tao)),
                edge_type=edge_type,
            )
        )


def _state(db, address, last_block, checked=True):
    from datetime import datetime, timezone

    with db.session() as session:
        session.add(
            WatchState(
                address=address,
                last_block=last_block,
                last_checked_at=datetime.now(timezone.utc) if checked else None,
                checked_count=1 if checked else 0,
            )
        )


def _spy_api(monkeypatch):
    """记录 API 被调用次数；返回 (调用列表, 可设的返回行)。"""

    calls: list[str] = []
    payload = {"rows": []}

    def fake_query(*, address=None, from_address=None, to_address=None, page=1, limit=200):
        calls.append(address)
        return TransferPage(list(payload["rows"]), None)

    monkeypatch.setattr(watch_module, "get_transfer_page_query", fake_query)
    monkeypatch.setattr(taostats, "is_configured", lambda: True)
    return calls, payload


def _api_row(target, block, tao, tx="0x1"):
    return {
        "id": tx,
        "block_number": block,
        "from": LIB,
        "to": target,
        "amount": str(int(Decimal(str(tao)) * Decimal(1_000_000_000))),
        "transaction_hash": tx,
    }


# -- 该走本地的时候，真的不发请求 -------------------------------------------
def test_uses_local_index_and_spends_no_api_request(database, monkeypatch, config):
    _library(database)
    _cursor(database, 2000)
    _scan(database, 400, 2000)  # 连续覆盖 400..2000
    # 该地址在本地索引里出现过（400 块），且 baseline 落在区间内
    _local_transfer(database, LIB, OTHER, 400, 10)
    _state(database, LIB, 500)  # baseline 500 >= 区间起点 400
    calls, _payload = _spy_api(monkeypatch)

    service = WatchService(database)
    result = service._check_address(LIB, config)

    assert calls == [], "本地已覆盖，不该再发 API 请求"
    assert result["requests"] == 0
    assert service._local_hits == 1
    assert service._api_saved == 1  # include_stake=False 时只省 1 次


def test_local_edges_feed_auto_import(database, monkeypatch, config):
    """走本地也要能正常入库，不能只省额度不产出。"""

    _library(database)
    _cursor(database, 2000)
    _scan(database, 400, 2000)
    _local_transfer(database, LIB, OTHER, 400, 10)  # 让地址出现在本地
    _local_transfer(database, LIB, NEW, 1500, 45)  # 这条应被自动入库
    _state(database, LIB, 500)  # baseline 落在本地区间内
    _spy_api(monkeypatch)

    service = WatchService(database)
    result = service._check_address(LIB, config)

    assert result["requests"] == 0
    assert result["inserted"] == 1
    with database.session() as session:
        row = session.query(AddressLibrary).filter_by(address=NEW).one()
        assert row.source.startswith("auto-watch")
        assert row.book == "6NCK"


# -- 该退回 API 的三种情况，一个都不能省 ------------------------------------
def test_falls_back_when_local_index_empty(database, monkeypatch, config):
    _library(database)
    _state(database, LIB, 300)
    calls, payload = _spy_api(monkeypatch)
    payload["rows"] = [_api_row(NEW, 1500, 45)]

    service = WatchService(database)
    result = service._check_address(LIB, config)

    assert calls == [LIB], "没有本地索引时必须走 API"
    assert result["requests"] == 1
    assert service._local_hits == 0


def test_falls_back_when_baseline_before_local_window(database, monkeypatch, config):
    """baseline 早于本地起点 → 中间那段本地没有，必须问 API。"""

    _library(database)
    _cursor(database, 2000)
    _scan(database, 1000, 2000)
    _local_transfer(database, LIB, OTHER, 1000, 5)
    _state(database, LIB, 100)  # baseline 100 < 本地起点 1000 → 中间有缺口 → 中间有缺口
    calls, payload = _spy_api(monkeypatch)
    payload["rows"] = []

    service = WatchService(database)
    service._check_address(LIB, config)

    assert calls == [LIB]


def test_falls_back_when_address_never_in_local_index(database, monkeypatch, config):
    """地址从未在本地出现过 → 本地无法证明「没有新转账」。"""

    _library(database)
    _cursor(database, 2000)
    _scan(database, 1000, 2000)
    # 本地只有别的地址的转账，LIB 从没出现过
    _local_transfer(database, OTHER, NEW, 1200, 5)
    _state(database, LIB, 1500)  # baseline 在本地区间内，但该地址没出现过
    calls, payload = _spy_api(monkeypatch)
    payload["rows"] = []

    service = WatchService(database)
    service._check_address(LIB, config)

    assert calls == [LIB], "该地址本地无记录时必须走 API"


def test_prefer_local_index_can_be_disabled(database, monkeypatch, config):
    _library(database)
    _cursor(database, 2000)
    _scan(database, 400, 2000)
    _local_transfer(database, LIB, OTHER, 400, 10)
    _state(database, LIB, 500)
    calls, payload = _spy_api(monkeypatch)
    payload["rows"] = []

    config["prefer_local_index"] = False
    service = WatchService(database)
    service._check_address(LIB, config)

    assert calls == [LIB], "关掉开关后必须回到全部走 API 的旧行为"


# -- 游标推进：本地无新数据时不能原地打转 --------------------------------
def test_local_hit_advances_cursor_to_window_end(database, monkeypatch, config):
    """本地区间看完但没有新转账，游标要推进到区间上界，否则反复重扫。"""

    _library(database)
    _cursor(database, 2000)
    _scan(database, 400, 2000)
    _local_transfer(database, LIB, OTHER, 400, 10)
    _state(database, LIB, 500)
    _spy_api(monkeypatch)

    service = WatchService(database)
    service._check_address(LIB, config)

    with database.session() as session:
        state = session.get(WatchState, LIB)
        assert state.last_block == 2000, "应推进到本地覆盖上界"


def test_local_window_uses_cursor_not_max_transfer_block(database):
    """覆盖上界取游标：最大转账块号可能远小于已扫到的位置。"""

    _library(database)
    _cursor(database, 2000)
    _scan(database, 1500, 2000)
    _local_transfer(database, LIB, OTHER, 1500, 10)

    service = WatchService(database)
    assert service.local_window() == (1500, 2000)


def test_local_window_none_without_cursor(database):
    _local_transfer(database, LIB, OTHER, 1500, 10)
    service = WatchService(database)
    assert service.local_window() is None


# -- 断点必须识别：不连续的索引不能冒充「已覆盖」 -----------------------------
def test_discontinuous_scan_is_not_trusted(database, monkeypatch, config):
    """真实库里名义区间 18,254 块、实际只扫了 9,575 块（52%）。

    中间缺了 8,679 块（含一段 7,715 块的整段空洞，来自早期失败任务）。
    如果只取 [min, max] 当"已覆盖"，巡检会静默漏掉这些块里的转账。
    这里要求：连续段只能到断点为止。
    """

    _library(database)
    _cursor(database, 3000)
    # 连续 3000..2500，然后 2400..2000（中间 2499..2401 缺失）
    _scan(database, 2500, 3000)
    _scan(database, 2000, 2400)
    _local_transfer(database, LIB, OTHER, 2600, 10)
    _state(database, LIB, 2200)  # 落在下面那段里，但中间有断点

    service = WatchService(database)
    info = service.coverage()
    assert info["available"] is True
    # 连续上界只能到 2500（断点下方），不能一路延伸到 2000
    assert info["safe_from"] == 2500
    assert info["safe_to"] == 3000
    # 名义跨度比真实覆盖大，说明连续性不足
    assert info["covered_blocks"] < info["span_blocks"]
    # 这段 baseline 落在断点下方 → 必须退回 API
    assert service._local_covers(LIB, 2200, service.local_window()) is False


def test_coverage_reports_ratio(database):
    _cursor(database, 1000)
    _scan(database, 900, 1000)
    _scan(database, 500, 600)
    service = WatchService(database)
    info = service.coverage()
    # 900..1000 是 101 块、500..600 是 101 块，合计 202 块；名义跨度 501 块
    assert info["covered_blocks"] == 202
    assert info["span_blocks"] == 501
    assert 0.3 < info["ratio"] < 0.5
