# -*- coding: utf-8 -*-
"""内置固定名单（交易所 / 验证者 / 公共地址）与验证者同步 / 终止关联。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app.service as service_module
from app.builtin_addresses import builtin_entries
from app.db import Database
from app.inputs import InputError
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
VALIDATOR = "5HK5tp6t2S59DywmHRWPBVJeJ86T61KjurYqeooqj8sREpeN"
EXCHANGE = "5Gr1Ab7LSV4eT6RdWmVeoxnTpDBWAZFLXHHrcaWNDdjK22pp"
BOOK_ADDRESS = "5FV99mBYw3tYrMTXXe52PDa69an1AE19aTrpnB4kzxW73yUj"
DOWNSTREAM = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
OTHER = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"
SYNCED_VALIDATOR = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
SYNCED_VALIDATOR_2 = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


def build_service(directory, name="builtin.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def ingest_chain(database):
    """SEED → VALIDATOR → DOWNSTREAM，另有一条 SEED → OTHER。"""

    records = [
        ("v-1", SEED, VALIDATOR),
        ("v-2", VALIDATOR, DOWNSTREAM),
        ("v-3", SEED, OTHER),
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


def test_builtin_seed_writes_all_entries_and_is_idempotent():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            first = service.ensure_builtin_addresses()
            assert first["inserted"] == len(builtin_entries())
            assert first["updated"] == 0

            rows = {row["address"]: row for row in service.library()}
            exchange = rows[EXCHANGE]
            assert exchange["category"] == "EXCHANGE"
            assert exchange["source"] == "builtin:exchange"
            assert exchange["exchange_confirmed"] is True
            assert exchange["exchange_locked"] is True
            assert exchange["builtin_locked"] is True

            validator = rows[VALIDATOR]
            assert validator["category"] == "VALIDATOR"
            assert validator["source"] == "builtin:validator"
            assert validator["builtin_locked"] is True

            # 第二次启动不新增、不重复修改。
            again = service.ensure_builtin_addresses()
            assert again["inserted"] == 0
            assert again["updated"] == 0
            assert len(service.library()) == len(builtin_entries())
        finally:
            database.engine.dispose()


def test_builtin_keeps_user_category_and_label():
    """库里已有的人工分类/名称保留，只追加系统身份。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows(
                [{"address": BOOK_ADDRESS, "label": "rrik", "category": "大V"}],
                "manual",
            )
            service.ensure_builtin_addresses()
            row = next(
                item for item in service.library() if item["address"] == BOOK_ADDRESS
            )
            assert row["category"] == "大V"
            assert row["label"] == "rrik"
            assert "EXCHANGE" in row["categories"]
            assert row["exchange_confirmed"] is True
            assert row["source"] == "builtin:exchange"
        finally:
            database.engine.dispose()


def test_builtin_rows_cannot_be_deleted_or_recategorized():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.ensure_builtin_addresses()
            row = next(item for item in service.library() if item["address"] == EXCHANGE)
            with pytest.raises(InputError):
                service.remove_address(row["id"])
            with pytest.raises(InputError):
                service.update_library_categories(row["id"], ["群友"])
            with pytest.raises(InputError):
                service.decide_exchange(row["id"], "reject")
            with pytest.raises(InputError):
                service.decide_exchange_address(EXCHANGE, "reject")

            # 判定没有被改动。
            current = next(item for item in service.library() if item["address"] == EXCHANGE)
            assert current["exchange_confirmed"] is True
            assert current["exchange_rejected"] is False
        finally:
            database.engine.dispose()


def test_batch_delete_skips_builtin_rows():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows(
                [
                    {"address": SEED, "category": "大V"},
                    {"address": OTHER, "category": "其他"},
                ],
                "manual",
            )
            service.ensure_builtin_addresses()
            ids = [item["id"] for item in service.library()]
            removed = service.remove_addresses(ids)
            assert removed == 2
            remaining = {item["address"] for item in service.library()}
            assert SEED not in remaining and OTHER not in remaining
            assert VALIDATOR in remaining and EXCHANGE in remaining
        finally:
            database.engine.dispose()


def test_builtin_validator_is_terminal():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "terminal.db")
        try:
            ingest_chain(database)
            service.ensure_builtin_addresses()

            result = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1", data_source="local"
            )
            addresses = {row["address"] for row in result["related"]}
            # 验证者本身保留，但下游不再展开，避免无效关联把结果灌满。
            assert VALIDATOR in addresses
            assert DOWNSTREAM not in addresses
            assert OTHER in addresses
            validator_row = next(
                row for row in result["related"] if row["address"] == VALIDATOR
            )
            assert validator_row["address_role"] == "人工确认验证者"
        finally:
            database.engine.dispose()


def test_manual_validator_is_terminal_too():
    """人工同步/标记的验证者（无内置名单）同样终止关联。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "manual-terminal.db")
        try:
            ingest_chain(database)
            service.import_rows(
                [{"address": VALIDATOR, "label": "验证者 A", "category": "大V"}],
                "manual",
            )
            # 单条分类不能人工写成 VALIDATOR，这里直接走数据库层模拟同步结果。
            with database.session() as db:
                from sqlalchemy import select

                from app.db import AddressLibrary
                from app.service import encode_categories

                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == VALIDATOR)
                )
                row.categories = encode_categories(["大V", "VALIDATOR"])

            result = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1", data_source="local"
            )
            addresses = {row["address"] for row in result["related"]}
            assert VALIDATOR in addresses
            assert DOWNSTREAM not in addresses
        finally:
            database.engine.dispose()


def test_sync_validators_inserts_and_marks_existing(monkeypatch):
    addresses = {SYNCED_VALIDATOR, SYNCED_VALIDATOR_2, "not-an-address"}
    monkeypatch.setattr(
        service_module, "get_active_validator_addresses", lambda: (addresses, False)
    )
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "sync.db")
        try:
            service.import_rows(
                [{"address": SYNCED_VALIDATOR_2, "label": "老王", "category": "群友"}],
                "manual",
            )
            result = service.sync_validator_addresses()
            assert result["inserted"] == 1
            assert result["updated"] == 1
            assert result["official_total"] == 2
            assert result["invalid_count"] == 1

            rows = {row["address"]: row for row in service.library()}
            created = rows[SYNCED_VALIDATOR]
            assert created["category"] == "VALIDATOR"
            assert created["source"] == "taostats:/v1/validators/active"
            existing = rows[SYNCED_VALIDATOR_2]
            assert existing["category"] == "群友"
            assert existing["label"] == "老王"
            assert "VALIDATOR" in existing["categories"]

            again = service.sync_validator_addresses()
            assert again["inserted"] == 0
            assert again["updated"] == 0
            assert again["skipped"] == 2
        finally:
            database.engine.dispose()


def test_validator_sync_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module

    monkeypatch.setattr(
        service_module,
        "get_active_validator_addresses",
        lambda: ({SYNCED_VALIDATOR}, False),
    )
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            main_module.app.dependency_overrides[main_module.service] = lambda: service
            client = TestClient(main_module.app)
            try:
                response = client.post("/api/library/import-validators")
                assert response.status_code == 200
                assert response.json()["inserted"] == 1
            finally:
                main_module.app.dependency_overrides.clear()
        finally:
            main_module.SERVICE = original
            database.engine.dispose()


# -- 预置验证者快照 / 路由方法兼容 -----------------------------------------


def test_validator_snapshot_is_loaded_and_deduped():
    """随版本发布的验证者快照要能离线读到，且和人工名单不重复。"""

    from app.builtin_addresses import (
        VALIDATOR_SNAPSHOT_LABEL,
        load_validator_snapshot,
    )

    snapshot = load_validator_snapshot()
    assert len(snapshot) > 1000
    assert len(set(snapshot)) == len(snapshot)
    assert all(address.startswith("5") and len(address) == 48 for address in snapshot)

    entries = builtin_entries()
    ids = [item.address for item in entries]
    assert len(ids) == len(set(ids)), "预置快照与人工名单重叠必须去重"

    preset = [item for item in entries if item.label == VALIDATOR_SNAPSHOT_LABEL]
    assert len(preset) == len(snapshot) - (1 if VALIDATOR in snapshot else 0)
    assert all(item.category == "VALIDATOR" for item in preset)
    assert all(item.source == "builtin:validator" for item in preset)


def test_snapshot_addresses_become_locked_validator_rows():
    """预置的验证者地址进库后是 VALIDATOR 分类、内置来源、不可删除。"""

    from app.builtin_addresses import load_validator_snapshot

    sample = load_validator_snapshot()[:3]
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "snapshot.db")
        try:
            service.ensure_builtin_addresses()
            rows = {row["address"]: row for row in service.library()}
            for address in sample:
                row = rows[address]
                assert row["category"] == "VALIDATOR"
                assert row["builtin_locked"] is True
                assert "VALIDATOR" in row["categories"]
        finally:
            database.engine.dispose()


def test_validator_sync_endpoint_accepts_get(monkeypatch):
    """远端踩到过 405：浏览器拿到的是 GET。两条方法都要能用。"""

    from fastapi.testclient import TestClient

    import app.main as main_module

    monkeypatch.setattr(
        service_module,
        "get_active_validator_addresses",
        lambda: ({SYNCED_VALIDATOR_2}, False),
    )
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api-get.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            main_module.app.dependency_overrides[main_module.service] = lambda: service
            client = TestClient(main_module.app)
            try:
                response = client.get("/api/library/import-validators")
                assert response.status_code == 200
                assert response.json()["official_total"] == 1
            finally:
                main_module.app.dependency_overrides.clear()
        finally:
            main_module.SERVICE = original
            database.engine.dispose()


def test_unknown_library_subpath_returns_404_not_405():
    """动态行路由限定整数后，不存在的单词路径应该是 404 而不是误导性的 405。"""

    from fastapi.testclient import TestClient

    import app.main as main_module

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api-404.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            main_module.app.dependency_overrides[main_module.service] = lambda: service
            client = TestClient(main_module.app)
            try:
                response = client.get("/api/library/not-a-real-action")
                assert response.status_code == 404
            finally:
                main_module.app.dependency_overrides.clear()
        finally:
            main_module.SERVICE = original
            database.engine.dispose()
