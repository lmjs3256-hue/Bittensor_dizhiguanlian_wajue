"""地址扫描查询端点的冒烟测试。

为什么要有这个文件：0.8.36 期间 `/api/relations` 与 `/api/jobs` 双双 500，
原因是 `service.prepare_relation_context()` 里用了 `Decimal` 而模块顶部的
`from decimal import Decimal` 在某次重构中丢了。`Decimal` 在该文件只有两处
引用，平时不执行到就想不起来；而这个函数是**每一次查询**的必经之路，
等于功能整体不可用，却没有任何测试覆盖到——测试全绿也没拦住。

这里直接打端点，保证「查询能跑通」这条最基础的链路永远有回归保护。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.service as service_module
from app.db import Database
from app.service import LinkService

SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
PEER = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
OTHER = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    """避免测试真的去连 RPC 查余额。"""

    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import app.main as main_module

    if True:
        database = Database(tmp_path / "smoke.db")
        database.initialize()
        database.ingest_transfers(
            [
                {
                    "event_key": "e1",
                    "tx_hash": "0x1",
                    "block_number": 100,
                    "block_hash": "0xh100",
                    "event_index": 0,
                    "from_address": SEED,
                    "to_address": PEER,
                    "amount_rao": 50_000_000_000,
                    "amount_tao": service_module.Decimal("50"),
                },
                {
                    "event_key": "e2",
                    "tx_hash": "0x2",
                    "block_number": 101,
                    "block_hash": "0xh101",
                    "event_index": 1,
                    "from_address": PEER,
                    "to_address": OTHER,
                    "amount_rao": 20_000_000_000,
                    "amount_tao": service_module.Decimal("20"),
                },
            ]
        )
        service = LinkService(database, "wss://example.invalid")
        original = main_module.SERVICE
        main_module.SERVICE = service
        try:
            yield TestClient(main_module.app), database
        finally:
            main_module.SERVICE = original
            database.engine.dispose()


def test_relations_endpoint_works_with_local_source(client):
    """最基础的本地查询必须能返回结果，不能 500。"""

    http, _db = client
    response = http.post(
        "/api/relations",
        json={
            "seed": SEED,
            "depth": 1,
            "data_source": "local",
            "min_edge_tao": "0",
            "min_total_tao": "0",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["seed"] == SEED
    assert isinstance(payload.get("related"), list)


def test_prepare_relation_context_builds_book_pair_stats(tmp_path):
    """直接覆盖 prepare_relation_context：Decimal 在这里被用到。

    这条是给 NameError 类的回归上锁——只要该函数里任何名字没定义好，
    这里立刻失败，不用等到用户点查询。
    """

    if True:
        database = Database(tmp_path / "ctx.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        try:
            # 造一条库配对统计，确保 Decimal 那一行真的被执行
            from app.db import BookPairStat

            with database.session() as db:
                db.add(
                    BookPairStat(
                        book_a="A", book_b="B", total_tao=service_module.Decimal("12.5"), tx_count=3
                    )
                )
            context = service.prepare_relation_context(
                seed=SEED,
                depth=1,
                min_edge_tao="0",
                min_total_tao="0",
                data_source="local",
            )
            assert context is not None
            assert ("A", "B") in context.book_pair_stats
            assert context.book_pair_stats[("A", "B")] == service_module.Decimal("12.5")
        finally:
            database.engine.dispose()


def test_service_module_exposes_decimal():
    """`Decimal` 必须在模块作用域可用——那次 500 就是它没了。"""

    assert hasattr(service_module, "Decimal")
    assert service_module.Decimal("1.5") + service_module.Decimal("0.5") == 2
