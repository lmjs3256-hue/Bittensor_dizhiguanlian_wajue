# -*- coding: utf-8 -*-
"""公共地址（PUBLIC）与地址库「全部 / 选中」导出。"""

import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app.service as service_module
from app.db import Database
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
PUBLIC_ADDR = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
DOWNSTREAM = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
OTHER = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"
THIRD = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


def build_service(directory, name="public.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def ingest_chain(database):
    """SEED → PUBLIC_ADDR → DOWNSTREAM，另有一条 SEED → OTHER。"""

    records = [
        ("pub-1", SEED, PUBLIC_ADDR),
        ("pub-2", PUBLIC_ADDR, DOWNSTREAM),
        ("pub-3", SEED, OTHER),
    ]
    database.ingest_transfers(
        [
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
        ]
    )


def test_marking_public_keeps_user_category():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows([{"address": PUBLIC_ADDR, "category": "群友"}], "manual")
            result = service.decide_public_address(PUBLIC_ADDR, True)
            item = result["item"]
            assert "PUBLIC" in item["categories"]
            # 主分类仍归用户所有，标公共地址不会把它冲掉。
            assert item["category"] == "群友"

            # 重复标记是幂等的
            again = service.decide_public_address(PUBLIC_ADDR, True)
            assert again["item"]["categories"].count("PUBLIC") == 1

            removed = service.decide_public_address(PUBLIC_ADDR, False)
            assert "PUBLIC" not in removed["item"]["categories"]
            assert removed["item"]["category"] == "群友"
        finally:
            database.engine.dispose()


def test_marking_public_on_new_address_creates_public_row():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            result = service.decide_public_address(PUBLIC_ADDR, True)
            item = result["item"]
            assert item["category"] == "PUBLIC"
            assert item["label"] == "公共地址"
            assert item["source"] == "manual:public-address"

            # 取消后回落到默认分类，不会残留 PUBLIC 主分类。
            removed = service.decide_public_address(PUBLIC_ADDR, False)
            assert removed["item"]["category"] == "其他"
            assert "PUBLIC" not in removed["item"]["categories"]
        finally:
            database.engine.dispose()


def test_public_address_is_terminal_and_filterable():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "terminal.db")
        try:
            ingest_chain(database)
            service.decide_public_address(PUBLIC_ADDR, True)

            normal = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1", data_source="local"
            )
            addresses = {row["address"] for row in normal["related"]}
            # 公共地址本身保留（相当于「汇聚点」），但它下游不再展开。
            assert PUBLIC_ADDR in addresses
            assert DOWNSTREAM not in addresses
            assert OTHER in addresses
            assert any("公共地址" in warning for warning in normal["warnings"])
            public_row = next(
                row for row in normal["related"] if row["address"] == PUBLIC_ADDR
            )
            assert public_row["address_role"] == "公共地址"

            filtered = service.relations(
                seed=SEED,
                depth=3,
                min_edge_tao="1",
                min_total_tao="1",
                data_source="local",
                filter_publics=True,
            )
            filtered_addresses = {row["address"] for row in filtered["related"]}
            assert PUBLIC_ADDR not in filtered_addresses
            assert DOWNSTREAM not in filtered_addresses
            assert OTHER in filtered_addresses
            assert any("公共地址过滤" in warning for warning in filtered["warnings"])
        finally:
            database.engine.dispose()


def test_public_sorted_right_after_exchange():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "sort.db")
        try:
            service.import_rows(
                [
                    {"address": PUBLIC_ADDR, "category": "群友"},
                    {"address": THIRD, "category": "大V"},
                ],
                "manual",
            )
            # 两个地址都标成系统身份：交易所优先，其次公共地址。
            claim = service.decide_exchange_address(PUBLIC_ADDR, "confirm")
            assert claim["item"]["category"] == "EXCHANGE"
            service.decide_public_address(THIRD, True)

            database.ingest_transfers(
                [
                    {
                        "event_key": f"sort-{index}",
                        "tx_hash": f"sort-{index}",
                        "block_number": 200 + index,
                        "block_hash": "0xblock",
                        "event_index": index,
                        "from_address": SEED,
                        "to_address": address,
                        "amount_rao": 5_000_000_000,
                        "amount_tao": 5,
                    }
                    for index, address in enumerate([THIRD, PUBLIC_ADDR, OTHER])
                ]
            )
            result = service.relations(
                seed=SEED, depth=1, min_edge_tao="1", min_total_tao="1", data_source="local"
            )
            order = [row["address"] for row in result["related"]]
            assert order[0] == PUBLIC_ADDR  # 交易所
            assert order[1] == THIRD  # 公共地址
        finally:
            database.engine.dispose()


def test_export_all_and_selected_only():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "export.db")
        try:
            service.import_rows(
                [
                    {"address": SEED, "category": "大V"},
                    {"address": PUBLIC_ADDR, "category": "群友"},
                    {"address": OTHER, "category": "其他"},
                ],
                "manual",
            )

            all_csv, _ = service.export_library("csv")
            all_rows = list(csv.DictReader(io.StringIO(all_csv)))
            assert {row["address"] for row in all_rows} == {SEED, PUBLIC_ADDR, OTHER}

            selected_csv, _ = service.export_library("csv", [SEED, OTHER])
            selected_rows = list(csv.DictReader(io.StringIO(selected_csv)))
            assert {row["address"] for row in selected_rows} == {SEED, OTHER}
            # 表头保持完整，导入端仍能识别。
            assert list(selected_rows[0].keys()) == list(all_rows[0].keys())

            selected_json = json.loads(service.export_library("json", [PUBLIC_ADDR])[0])
            assert [item["address"] for item in selected_json["addresses"]] == [PUBLIC_ADDR]

            # 选中集合为空 / 全部失效时给出明确错误，而不是静默导出全库。
            with pytest.raises(Exception):
                service.export_library("csv", [])
            with pytest.raises(Exception):
                service.export_library("csv", ["5NotInLibrary00000000000000000000000000000000000"])
        finally:
            database.engine.dispose()


def test_export_selected_api_returns_only_selected():
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api-export.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            service.import_rows(
                [
                    {"address": SEED, "category": "大V"},
                    {"address": OTHER, "category": "其他"},
                ],
                "manual",
            )
            client = TestClient(main_module.app)
            response = client.post(
                "/api/library/export",
                json={"format": "csv", "addresses": [SEED]},
            )
            assert response.status_code == 200
            rows = list(csv.DictReader(io.StringIO(response.text)))
            assert [row["address"] for row in rows] == [SEED]
            assert "selected-1" in response.headers["content-disposition"]

            marked = client.post(
                "/api/library/mark-public", json={"address": OTHER, "marked": True}
            )
            assert marked.status_code == 200
            assert "PUBLIC" in marked.json()["item"]["categories"]
        finally:
            main_module.SERVICE = original
            database.engine.dispose()
