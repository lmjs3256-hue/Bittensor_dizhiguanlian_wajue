"""官方交易所名单：默认导入、只增不减、判定固定不可改判。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.service as service_module
import app.taostats as taostats
from app import settings_store
from app.db import Database
from app.inputs import InputError
from app.service import (
    OFFICIAL_EXCHANGE_SOURCE,
    LinkService,
    select_exportable_addresses,
)


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
OFFICIAL = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
OTHER_OFFICIAL = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    taostats.reset_client_state_for_tests()
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})
    yield
    taostats.reset_client_state_for_tests()


def build_service(directory, name="official.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def patch_directory(monkeypatch, rows):
    monkeypatch.setattr(
        service_module,
        "get_exchange_addresses",
        lambda: (rows, False),
    )


def test_official_sync_inserts_and_reports_new_addresses(monkeypatch):
    patch_directory(
        monkeypatch,
        [
            {"address": OFFICIAL, "name": "Example Exchange"},
            {"address": OTHER_OFFICIAL, "name": "Another Exchange"},
            {"address": "not-an-address", "name": "Broken"},
        ],
    )
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            result = service.sync_exchange_addresses()
            assert result["inserted"] == 2
            assert result["updated"] == 0
            assert result["invalid_count"] == 1
            assert [item["address"] for item in result["inserted_addresses"]] == [
                OFFICIAL,
                OTHER_OFFICIAL,
            ]
            assert result["inserted_addresses"][0]["label"] == "Example Exchange"

            rows = {row["address"]: row for row in service.library()}
            official = rows[OFFICIAL]
            assert official["category"] == "EXCHANGE"
            assert official["source"] == OFFICIAL_EXCHANGE_SOURCE
            assert official["exchange_confirmed"] is True
            assert official["exchange_locked"] is True
        finally:
            database.engine.dispose()


def test_second_sync_only_counts_existing_addresses(monkeypatch):
    """第二次同步不新增、不更新，只报“已存在 N 个”。"""

    patch_directory(monkeypatch, [{"address": OFFICIAL, "name": "Example Exchange"}])
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_exchange_addresses()
            again = service.sync_exchange_addresses()
            assert again["inserted"] == 0
            assert again["updated"] == 0
            assert again["skipped"] == 1
            assert again["inserted_addresses"] == []
            assert len(service.library()) == 1
        finally:
            database.engine.dispose()


def test_sync_keeps_manual_category_and_appends_exchange(monkeypatch):
    """库里已有的地址：分类、名称、备注都不动，只补 EXCHANGE 标记并锁定。"""

    patch_directory(monkeypatch, [{"address": SEED, "name": "Binance"}])
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows(
                [{"address": SEED, "label": "老王的热钱包", "category": "大V", "note": "别动"}],
                "manual",
            )
            result = service.sync_exchange_addresses()
            assert result["inserted"] == 0
            assert result["updated"] == 1
            assert result["locked_addresses"][0]["address"] == SEED

            row = service.library()[0]
            assert row["category"] == "大V"
            assert "EXCHANGE" in row["categories"]
            assert row["label"] == "老王的热钱包"
            assert row["note"] == "别动"
            assert row["source"] == OFFICIAL_EXCHANGE_SOURCE
            assert row["exchange_locked"] is True
        finally:
            database.engine.dispose()


def test_official_rows_cannot_be_overruled(monkeypatch):
    patch_directory(monkeypatch, [{"address": OFFICIAL, "name": "Example Exchange"}])
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_exchange_addresses()
            row = service.library()[0]

            for decision in ("reject", "reset", "confirm"):
                with pytest.raises(InputError):
                    service.decide_exchange(row["id"], decision)
                with pytest.raises(InputError):
                    service.decide_exchange_address(OFFICIAL, decision)

            # 判定没有被改动：仍是官方确认状态。
            assert service.library()[0]["exchange_confirmed"] is True
            assert service.library()[0]["exchange_rejected"] is False
        finally:
            database.engine.dispose()


def test_manual_exchange_rows_still_editable():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows(
                [{"address": SEED, "category": "EXCHANGE", "label": "候选交易所"}],
                "manual.csv",
            )
            row = service.library()[0]
            assert row["exchange_locked"] is False
            rejected = service.decide_exchange(row["id"], "reject")
            assert rejected["item"]["exchange_rejected"] is True
            confirmed = service.decide_exchange_address(SEED, "confirm")
            assert confirmed["item"]["exchange_confirmed"] is True
        finally:
            database.engine.dispose()


def test_official_exchange_still_filtered_when_appended_to_user_category(monkeypatch):
    """「大V + EXCHANGE」这种组合也要被当成已确认交易所过滤/排除导出。"""

    patch_directory(monkeypatch, [{"address": SEED, "name": "Binance"}])
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows(
                [{"address": SEED, "label": "老王", "category": "大V"}],
                "manual",
            )
            service.sync_exchange_addresses()

            selection = select_exportable_addresses(
                [
                    {
                        "address": SEED,
                        "depth": 1,
                        "library_category": "大V",
                        "library_categories": ["大V", "EXCHANGE"],
                        "exchange_confirmed": True,
                        "exchange_rejected": False,
                    }
                ]
            )
            assert selection["addresses"] == []
            assert selection["skipped_exchange"] == 1
        finally:
            database.engine.dispose()


def test_import_rows_cannot_unlock_official_row(monkeypatch):
    patch_directory(monkeypatch, [{"address": OFFICIAL, "name": "Example Exchange"}])
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_exchange_addresses()
            # 备份还原/文件导入把 exchange_rejected 塞进来也不能生效。
            service.import_rows(
                [
                    {
                        "address": OFFICIAL,
                        "category": "EXCHANGE",
                        "exchange_confirmed": False,
                        "exchange_rejected": True,
                    }
                ],
                "backup.json",
            )
            row = service.library()[0]
            assert row["exchange_confirmed"] is True
            assert row["exchange_rejected"] is False
            assert row["source"] == OFFICIAL_EXCHANGE_SOURCE
        finally:
            database.engine.dispose()


def test_auto_sync_skips_without_key_and_records_state(monkeypatch):
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            skipped = service.auto_sync_exchange_addresses()
            assert skipped["status"] == "skipped"
            assert skipped["reason"] == "no_key"
        finally:
            database.engine.dispose()

    patch_directory(monkeypatch, [{"address": OFFICIAL, "name": "Example Exchange"}])
    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "auto.db")
        try:
            synced = service.auto_sync_exchange_addresses()
            assert synced["status"] == "synced"
            assert synced["inserted"] == 1

            status = service.exchange_sync_status()
            assert status["official_total"] == 1
            assert status["notify"] is True
            assert [item["address"] for item in status["pending"]] == [OFFICIAL]

            service.acknowledge_exchange_notice()
            assert service.exchange_sync_status()["pending"] == []
            assert service.exchange_sync_status()["notify"] is False
        finally:
            database.engine.dispose()


def test_exchange_sync_endpoints(monkeypatch):
    patch_directory(monkeypatch, [{"address": OFFICIAL, "name": "Example Exchange"}])
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api.db")
        try:
            main_module.app.dependency_overrides[main_module.service] = lambda: service
            client = TestClient(main_module.app)
            try:
                synced = client.post("/api/library/import-exchanges")
                assert synced.status_code == 200
                assert synced.json()["inserted"] == 1

                status = client.get("/api/library/exchange-sync")
                assert status.status_code == 200
                assert status.json()["official_total"] == 1
                assert status.json()["pending"][0]["address"] == OFFICIAL

                ack = client.post("/api/library/exchange-sync/ack")
                assert ack.status_code == 200
                assert client.get("/api/library/exchange-sync").json()["pending"] == []

                row_id = service.library()[0]["id"]
                blocked = client.post(
                    f"/api/library/{row_id}/confirm-exchange",
                    json={"decision": "reject"},
                )
                assert blocked.status_code == 404
            finally:
                main_module.app.dependency_overrides.clear()
        finally:
            database.engine.dispose()
            settings_store.save_exchange_sync_state({})
