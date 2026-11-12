"""永久移除关联（0.9.5）：写进数据库、全机共享、可逐个恢复。

背景：以前结果页的「移除关联」只改前端内存里的一个 Set，刷新页面就没了；
更糟的是巡检的自动入库会把被移除的地址重新写进地址库，下一次扫描它又带
着一整条下游线索回来。用户的说法是「手动移除关联的地址是不方便分类，但
人工能确定是干扰地址」——这是一条**否定结论**，必须记住，而不是每次重来。

这组测试锁住五件事：

1. 移除后地址本身和它的直接转账对手（连同下游）都从结果里消失；
2. 与它无关的分支不受影响（不能一移除就整页清空）；
3. 自动入库不会让它复活；
4. 换一个 ``LinkService``（等价于重启进程）依然生效——说明是落盘的；
5. 恢复后原样回来，且标记本身幂等、非法地址拒绝。
"""

from __future__ import annotations

import pytest

import app.service as service_module
from app.db import AddressLibrary, AutoImportLog, Database
from app.service import InputError, LinkService

SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
PEER = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
OTHER = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
FRIEND = "5Gjq3XfuGxtAa6jmTgohbaAL4AA9RE7FQUBG87RGHG8xThor"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    """避免测试真的去连 RPC 查余额。"""

    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


def _transfer(key: str, block: int, source: str, target: str, tao: int) -> dict:
    return {
        "event_key": key,
        "tx_hash": f"0x{key}",
        "block_number": block,
        "block_hash": f"0xh{block}",
        "event_index": 0,
        "from_address": source,
        "to_address": target,
        "amount_rao": tao * 100_000_000,
        "amount_tao": service_module.Decimal(tao),
    }


def _seed_database(database: Database) -> None:
    """两条线索：seed→PEER→OTHER（要被剪掉）和 seed→FRIEND（应保留）。"""

    database.ingest_transfers(
        [
            _transfer("e1", 100, SEED, PEER, 50),
            _transfer("e2", 101, PEER, OTHER, 20),
            _transfer("e3", 102, SEED, FRIEND, 30),
        ]
    )


@pytest.fixture()
def env(tmp_path):
    database = Database(tmp_path / "removed.db")
    database.initialize()
    _seed_database(database)
    service = LinkService(database, "wss://example.invalid")
    try:
        yield service, database, tmp_path / "removed.db"
    finally:
        database.engine.dispose()


def _addresses(result: dict) -> set[str]:
    return {row["address"] for row in result.get("related") or []}


def _query(service: LinkService) -> dict:
    return service.relations(
        seed=SEED,
        depth=2,
        data_source="local",
        min_edge_tao="0",
        min_total_tao="0",
    )


def test_baseline_shows_both_branches(env):
    """先确认底数：没移除时三条关联地址都在，否则后面的断言没有意义。"""

    service, _db, _path = env
    assert _addresses(_query(service)) == {PEER, OTHER, FRIEND}


def test_removed_relation_prunes_address_and_downstream(env):
    """移除 PEER 后，PEER 和只能经由它的 OTHER 都应消失；FRIEND 保留。"""

    service, _db, _path = env
    service.remove_relation(PEER, seed=SEED, note="同一批转账的噪音")
    result = _query(service)
    addresses = _addresses(result)
    assert PEER not in addresses
    assert OTHER not in addresses, "被移除地址的下游应当一并剪掉"
    assert FRIEND in addresses, "无关分支不能被牵连"


def test_removed_relation_survives_new_service(env):
    """换一个 LinkService（等价于重启进程）标记仍生效——证明落在数据库里。"""

    service, _db, path = env
    service.remove_relation(PEER)
    fresh_db = Database(path)
    fresh_db.initialize()
    try:
        fresh_service = LinkService(fresh_db, "wss://example.invalid")
        assert PEER not in _addresses(_query(fresh_service))
        assert FRIEND in _addresses(_query(fresh_service))
    finally:
        fresh_db.engine.dispose()


def test_removed_relation_not_revived_by_auto_import(env):
    """自动入库把被移除地址写回地址库后，它仍然不该出现在结果里。

    这是当初提这个需求的直接原因：巡检自动入库 → 下一轮它又带着下游回来。
    """

    service, database, _path = env
    service.remove_relation(PEER)
    with database.session() as db:
        db.add(
            AddressLibrary(
                address=PEER,
                label="",
                category="其他",
                note="自动入库 · 与 测试库 累计 50 TAO（1 笔）",
                source="auto-watch:taostats",
                book="测试库",
            )
        )
        db.add(
            AutoImportLog(
                address=PEER,
                book="测试库",
                trigger_address=SEED,
                trigger_book="测试库",
                amount_tao=service_module.Decimal("50"),
                tx_count=1,
                stake_count=0,
                kind="tao",
                reason="测试：模拟巡检自动入库",
                status="active",
            )
        )
    result = _query(service)
    assert PEER not in _addresses(result)
    assert OTHER not in _addresses(result)


def test_restore_relation_brings_it_back(env):
    """恢复后 PEER / OTHER 原样回来，名单清空。"""

    service, _db, _path = env
    service.remove_relation(PEER)
    assert PEER not in _addresses(_query(service))

    restored = service.restore_relation(PEER)
    assert restored["removed"] is True
    assert restored["total"] == 0
    assert _addresses(_query(service)) == {PEER, OTHER, FRIEND}


def test_remove_relation_is_idempotent(env):
    """同一地址重复标记：第一次 created=True，之后只补备注，不重复计数。"""

    service, _db, _path = env
    first = service.remove_relation(PEER, seed=SEED)
    assert first["created"] is True
    assert first["total"] == 1

    second = service.remove_relation(PEER, note="第二次点的，补个理由")
    assert second["created"] is False
    assert second["total"] == 1

    items = service.removed_relations()
    assert len(items) == 1
    assert items[0]["note"] == "第二次点的，补个理由"
    assert items[0]["seed"] == SEED


def test_remove_relation_rejects_invalid_address(env):
    """非法地址不能进名单：否则会往库里灌垃圾，还会被当成过滤名单。"""

    service, _db, _path = env
    with pytest.raises(InputError):
        service.remove_relation("not-an-address")
    assert service.removed_relations() == []


def test_removed_relations_records_seed_and_time(env):
    """清单要给界面显示「从哪次扫描、什么时候」，两个字段都得有。"""

    service, _db, _path = env
    service.remove_relation(OTHER, seed=SEED, note="懒得分类")
    (item,) = service.removed_relations()
    assert item["address"] == OTHER
    assert item["seed"] == SEED
    assert item["note"] == "懒得分类"
    assert item["created_at"], "要有时间戳，界面才排得出先后"


def test_clear_removed_relations(env):
    """全清：判错了想重来时一次性恢复。"""

    service, _db, _path = env
    service.remove_relation(PEER)
    service.remove_relation(OTHER)
    assert service.clear_removed_relations()["cleared"] == 2
    assert service.removed_relations() == []
    assert _addresses(_query(service)) == {PEER, OTHER, FRIEND}


# ---------------------------------------------------------------------------
# HTTP 层：四个接口的往返
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import app.main as main_module

    database = Database(tmp_path / "api.db")
    database.initialize()
    _seed_database(database)
    service = LinkService(database, "wss://example.invalid")
    original = main_module.SERVICE
    main_module.SERVICE = service
    try:
        yield TestClient(main_module.app)
    finally:
        main_module.SERVICE = original
        database.engine.dispose()


def test_removed_endpoints_roundtrip(client):
    http = client

    empty = http.get("/api/relations/removed")
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "total": 0}

    added = http.post(
        "/api/relations/removed", json={"address": PEER, "seed": SEED, "note": "噪音"}
    )
    assert added.status_code == 200, added.text
    assert added.json()["created"] is True
    assert added.json()["total"] == 1

    again = http.post("/api/relations/removed", json={"address": PEER})
    assert again.status_code == 200
    assert again.json()["created"] is False

    listed = http.get("/api/relations/removed").json()
    assert listed["total"] == 1
    assert listed["items"][0]["address"] == PEER

    bad = http.post("/api/relations/removed", json={"address": "0OIl-not-base58"})
    assert bad.status_code == 422

    deleted = http.delete(f"/api/relations/removed/{PEER}")
    assert deleted.status_code == 200
    assert deleted.json() == {"address": PEER, "removed": True, "total": 0}

    # 再删一次：不报错，只是没删到东西——前端恢复按钮可能被点两次。
    deleted_again = http.delete(f"/api/relations/removed/{PEER}")
    assert deleted_again.status_code == 200
    assert deleted_again.json()["removed"] is False

    http.post("/api/relations/removed", json={"address": PEER})
    http.post("/api/relations/removed", json={"address": OTHER})
    cleared = http.post("/api/relations/removed/clear")
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] == 2
    assert http.get("/api/relations/removed").json()["total"] == 0
