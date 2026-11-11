"""入库日志「撤回并标为系统角色」的接口级验证。

用户需求原话：「入库地址在入库日志里面要能直接点开查看确认。后面还需要
增加可人工标记交易所、验证者、公共地址等按钮。当点击这三个按钮默认撤回
刚自动入的库并移入对应的系统库。」

这里从 HTTP 入口走一遍，确认三件事：
1. 一个接口同时完成「撤回」和「下结论」，不会只做一半；
2. 三种角色都能正确落到地址库（分类 / 分组 / 移出原库）；
3. 未知 role 被拒绝，不会误改数据。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app.watch as watch_module
from app.db import AddressLibrary, Database, Transfer
from app.service import LinkService
from app.settings_store import DEFAULT_WATCH_CONFIG
from app.watch import WatchService

SEED = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
IMPORTED = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
BOOK = "6NCK"


def _transfer(database, frm, to, block, amount_tao):
    with database.session() as session:
        session.add(
            Transfer(
                event_key=f"k{block}",
                tx_hash=f"0x{block}",
                block_number=block,
                block_hash="h",
                event_index=0,
                from_address=frm,
                to_address=to,
                amount_rao=int(Decimal(str(amount_tao)) * 10**9),
                amount_tao=Decimal(str(amount_tao)),
                edge_type="tao_transfer",
            )
        )


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "revoke-mark.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        watch = WatchService(database)

        config = dict(DEFAULT_WATCH_CONFIG)
        config.update({"threshold_tao": 30.0, "auto_import": True, "sweep_enabled": True})
        monkeypatch.setattr(
            watch_module.settings_store, "load_watch_config", lambda: config
        )

        # 建一个库 + 跑两轮流水线，产出恰好一条自动入库记录。
        # 0.9.0 起没有逐地址 API 巡检了，入库只能由本地索引撞出来。
        service.import_rows([{"address": SEED, "book": BOOK}], "manual")
        watch.sweep_local_index(config)  # 定游标
        _transfer(database, SEED, IMPORTED, 1000, 45)
        watch.sweep_local_index(config)

        log = watch.logs()["items"][0]
        assert log["address"] == IMPORTED

        original_service = main_module.SERVICE
        original_watch = main_module.WATCH
        main_module.SERVICE = service
        main_module.WATCH = watch
        try:
            yield TestClient(main_module.app), database, service, watch, log["id"]
        finally:
            main_module.SERVICE = original_service
            main_module.WATCH = original_watch
            database.engine.dispose()


@pytest.mark.parametrize(
    ("role", "expect_category", "expect_flag"),
    [
        ("exchange", "EXCHANGE", "exchange_confirmed"),
        ("validator", "VALIDATOR", None),
        ("public", "PUBLIC", None),
    ],
)
def test_revoke_and_mark_endpoint(client, role, expect_category, expect_flag):
    http, database, _service, watch, log_id = client
    response = http.post(
        f"/api/library/auto-import-log/{log_id}/revoke-and-mark",
        json={"role": role, "address": IMPORTED},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["role"] == role
    assert payload["revoked"]["removed"] is True

    with database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=IMPORTED).one()
        assert row.category == expect_category
        # 移出原库是这三种结论的共同要求
        assert row.book == ""
        if expect_flag:
            assert getattr(row, expect_flag) is True

    # 日志被标成已撤回
    item = watch.logs()["items"][0]
    assert item["status"] == "revoked"


def test_revoke_and_mark_rejects_unknown_role(client):
    http, database, _service, _watch, log_id = client
    response = http.post(
        f"/api/library/auto-import-log/{log_id}/revoke-and-mark",
        json={"role": "god", "address": IMPORTED},
    )
    assert response.status_code == 422

    # 拒绝时不能有任何副作用：日志仍是 active、地址还在库
    with database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=IMPORTED).one()
        assert row.book == BOOK


def test_revoke_and_mark_reports_missing_log(client):
    http, _database, _service, _watch, _log_id = client
    response = http.post(
        "/api/library/auto-import-log/999999/revoke-and-mark",
        json={"role": "exchange"},
    )
    assert response.status_code == 404


def test_logs_expose_detail_fields(client):
    """「点开查看确认」需要日志带上完整信息与当前角色。"""

    http, _database, _service, _watch, _log_id = client
    data = http.get("/api/library/auto-import-log?status=active").json()
    item = data["items"][0]
    assert item["address"] == IMPORTED
    assert item["trigger_address"] == SEED
    assert item["in_library"] is True
    assert item["current_book"] == BOOK
    assert item["current_categories"]
    assert item["first_block"] and item["last_block"]
