# -*- coding: utf-8 -*-
"""「系统」分类与多分类：过滤、关联截止、人工改分类。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app.service as service_module
from app.db import Database
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
SYSTEM_ADDR = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
DOWNSTREAM = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
OTHER = "5FqBL928dMZ8JGvJ5jP5Qz8vJmM5Gk9m2yYQ4mZ8kM4Z7hE2a"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


def build_service(directory, name="test.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def ingest_chain(database):
    records = [
        ("sys-1", SEED, SYSTEM_ADDR),
        ("sys-2", SYSTEM_ADDR, DOWNSTREAM),
        ("sys-3", SEED, OTHER),
    ]
    database.ingest_transfers([
        {
            "event_key": key,
            "tx_hash": key,
            "block_number": 100 + index,
            "block_hash": "0xblock",
            "event_index": index,
            "from_address": source,
            "to_address": target,
            "amount_rao": 1_000_000_000,
            "amount_tao": 1,
        }
        for index, (key, source, target) in enumerate(records)
    ])


def test_address_can_belong_to_multiple_categories():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "categories.db")
        try:
            service.import_rows([{"address": SEED, "category": "大V"}], "manual")
            row = service.library()[0]
            saved = service.update_library_categories(row["id"], ["大V", "系统", "群友"])
            assert saved["item"]["categories"] == ["大V", "系统", "群友"]
            assert saved["item"]["category"] == "大V"
            assert service.library()[0]["categories"] == ["大V", "系统", "群友"]
            # 去重 + 空列表回落到默认分类
            again = service.update_library_categories(row["id"], [])
            assert again["item"]["categories"] == ["其他"]
            # 不能把地址改成 EXCHANGE / VALIDATOR 这类专用分类
            system_only = service.update_library_categories(row["id"], ["系统", "EXCHANGE"])
            assert system_only["item"]["categories"] == ["系统"]
        finally:
            database.engine.dispose()


def test_system_address_is_terminal_and_filterable():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "terminal.db")
        try:
            ingest_chain(database)
            service.import_rows([{"address": SYSTEM_ADDR, "category": "系统"}], "manual")

            normal = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1",
                data_source="local",
            )
            addresses = {row["address"] for row in normal["related"]}
            # 系统地址本身保留，但它下游的转账不再扩展。
            assert SYSTEM_ADDR in addresses
            assert DOWNSTREAM not in addresses
            assert OTHER in addresses
            assert any("关联终点" in warning for warning in normal["warnings"])

            filtered = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1",
                data_source="local", filter_systems=True,
            )
            filtered_addresses = {row["address"] for row in filtered["related"]}
            assert SYSTEM_ADDR not in filtered_addresses
            assert DOWNSTREAM not in filtered_addresses
            assert OTHER in filtered_addresses
            assert any("系统地址过滤" in warning for warning in filtered["warnings"])
        finally:
            database.engine.dispose()


def test_system_in_secondary_category_is_also_terminal():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "secondary.db")
        try:
            ingest_chain(database)
            service.import_rows([{"address": SYSTEM_ADDR, "category": "大V"}], "manual")
            row = service.library()[0]
            service.update_library_categories(row["id"], ["大V", "系统"])

            result = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1",
                data_source="local",
            )
            addresses = {row["address"] for row in result["related"]}
            assert SYSTEM_ADDR in addresses
            assert DOWNSTREAM not in addresses
            system_row = next(item for item in result["related"] if item["address"] == SYSTEM_ADDR)
            assert "系统" in system_row["library_categories"]
        finally:
            database.engine.dispose()


def test_categories_endpoint_updates_and_lists_system():
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            service.import_rows([{"address": SEED, "category": "大V"}], "manual")
            row_id = service.library()[0]["id"]
            client = TestClient(main_module.app)
            assert "系统" in [
                item["name"] for item in client.get("/api/library/categories").json()["items"]
            ]
            response = client.patch(
                f"/api/library/{row_id}/categories",
                json={"categories": ["大V", "系统"]},
            )
            assert response.status_code == 200
            assert response.json()["item"]["categories"] == ["大V", "系统"]
            assert service.library()[0]["categories"] == ["大V", "系统"]
            # 单个添加时也能直接选「系统」分类
            assert client.post(
                "/api/library", json={"address": DOWNSTREAM, "category": "系统"}
            ).status_code == 200
            assert any(
                item["address"] == DOWNSTREAM and item["category"] == "系统"
                for item in service.library()
            )
        finally:
            main_module.SERVICE = original
            database.engine.dispose()
