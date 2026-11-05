from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.db import AddressLibrary, Database
from app.inputs import InputError
from app.service import LinkService, _parse_upload


ADDRESSES = [
    "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt",
    "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN",
]


def test_txt_upload_ignores_csv_suffix_and_header():
    rows = _parse_upload(
        "DSV.txt",
        "address,label\n"
        f"{ADDRESSES[0]},note\n"
        f"{ADDRESSES[1]},\n",
    )

    assert [row["address"] for row in rows] == ADDRESSES[:2]


def test_import_rejects_invalid_ss58_without_partial_writes():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "library.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            with pytest.raises(InputError, match="invalid SS58"):
                service.import_rows(
                    [
                        {"address": ADDRESSES[0], "category": "WATCH"},
                        {"address": "not-an-ss58-address", "category": "WATCH"},
                    ],
                    "test.csv",
                )
            assert service.library() == []
        finally:
            database.engine.dispose()


def test_bulk_delete_removes_only_selected_rows():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "library.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows(
                [
                    {"address": address, "label": str(index), "category": "WATCH"}
                    for index, address in enumerate(ADDRESSES)
                ],
                "test.csv",
            )
            rows = service.library()

            deleted = service.remove_addresses([rows[0]["id"], rows[0]["id"], 999999])

            assert deleted == 1
            assert {row["address"] for row in service.library()} == set(ADDRESSES[1:])
            with database.session() as session:
                assert session.query(AddressLibrary).count() == 2
        finally:
            database.engine.dispose()


def test_exchange_and_validator_categories_can_be_imported():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "categories.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = service.import_rows(
                [
                    {"address": ADDRESSES[0], "category": "EXCHANGE", "label": "交易所"},
                    {"address": ADDRESSES[1], "category": "VALIDATOR", "label": "验证者"},
                ],
                "manual.csv",
            )
            assert result["inserted"] == 2
            assert {row["category"] for row in service.library()} == {"EXCHANGE", "VALIDATOR"}
        finally:
            database.engine.dispose()


def test_exchange_rejection_is_persisted_and_not_filterable():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "exchange-decision.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows(
                [{"address": ADDRESSES[0], "category": "EXCHANGE", "label": "候选交易所"}],
                "manual.csv",
            )
            row = service.library()[0]
            rejected = service.decide_exchange(row["id"], "reject")
            assert rejected["item"]["exchange_rejected"] is True
            assert rejected["item"]["exchange_confirmed"] is False
            reset = service.decide_exchange(row["id"], "reset")
            assert reset["item"]["exchange_rejected"] is False
            assert reset["item"]["exchange_confirmed"] is False
        finally:
            database.engine.dispose()


def test_notes_can_be_saved_for_library_rows_and_new_relation_addresses():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "notes.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows(
                [{"address": ADDRESSES[0], "label": "DSV", "category": "WATCH"}],
                "DSV.txt",
            )
            row = service.library()[0]
            updated = service.update_library_note(row["id"], "人工复核")
            assert updated["item"]["note"] == "人工复核"

            created = service.save_note(ADDRESSES[1], "关联地址备注")
            assert created["created"] is True
            assert created["item"]["category"] == "其他"
            assert created["item"]["note"] == "关联地址备注"
            saved = {item["address"]: item for item in service.library()}
            assert saved[ADDRESSES[1]]["note"] == "关联地址备注"
        finally:
            database.engine.dispose()


def test_address_without_label_falls_back_to_last_four_characters():
    """标签没写时默认显示地址后四位，并标记为“非人工命名”。"""

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "default-label.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows(
                [{"address": ADDRESSES[0], "category": "WATCH"}],
                "DSV_normalized.txt",
            )
            item = service.library()[0]
            assert item["label"] == ADDRESSES[0][-4:] == "jEbt"
            assert item["label_custom"] is False
        finally:
            database.engine.dispose()


def test_rename_address_label_and_reset_to_default():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "rename-label.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows([{"address": ADDRESSES[0]}], "manual")
            row_id = service.library()[0]["id"]

            saved = service.update_library_label(row_id, "  某鲸鱼  ")
            assert saved["item"]["label"] == "某鲸鱼"
            assert saved["item"]["label_custom"] is True
            assert service.library()[0]["label"] == "某鲸鱼"

            reset = service.update_library_label(row_id, "")
            assert reset["item"]["label"] == ADDRESSES[0][-4:]
            assert reset["item"]["label_custom"] is False
        finally:
            database.engine.dispose()


def test_rename_label_endpoint_and_default_sync_label(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "rename-api.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            monkeypatch.setattr(main_module, "SERVICE", service)
            client = TestClient(main_module.app)
            created = service.import_rows([{"address": ADDRESSES[0]}], "manual")
            row_id = service.library()[0]["id"]
            assert created["inserted"] == 1

            response = client.patch(f"/api/library/{row_id}/label", json={"label": "交易所热钱包"})
            assert response.status_code == 200
            assert response.json()["item"]["label"] == "交易所热钱包"

            response = client.patch(f"/api/library/{row_id}/label", json={"label": ""})
            assert response.status_code == 200
            assert response.json()["item"]["label"] == ADDRESSES[0][-4:]

            synced = service.sync_relation_addresses(
                seed=ADDRESSES[2],
                addresses=[ADDRESSES[1]],
                include_seed=False,
            )
            labels = {item["address"]: item["label"] for item in service.library()}
            assert labels[ADDRESSES[1]] == ADDRESSES[2][-4:] == "SxsN"
            assert synced["label"] == "SxsN"
        finally:
            database.engine.dispose()


def test_library_export_is_portable_and_preserves_exchange_decisions():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "portable-library.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows(
                [
                    {
                        "address": ADDRESSES[0],
                        "label": "DSV",
                        "category": "WATCH",
                        "note": "研究备注",
                    },
                    {
                        "address": ADDRESSES[1],
                        "label": "OKX",
                        "category": "EXCHANGE",
                        "note": "公共地址",
                        "exchange_confirmed": True,
                    },
                    {
                        "address": ADDRESSES[2],
                        "label": "候选交易所",
                        "category": "EXCHANGE",
                        "exchange_confirmed": False,
                        "exchange_rejected": True,
                    },
                ],
                "manual.csv",
            )

            csv_text, csv_type = service.export_library("csv")
            csv_rows = _parse_upload("backup.csv", csv_text)
            assert csv_type == "text/csv; charset=utf-8"
            assert {row["address"] for row in csv_rows} == set(ADDRESSES)
            exported = {row["address"]: row for row in csv_rows}
            assert exported[ADDRESSES[0]]["note"] == "研究备注"
            assert exported[ADDRESSES[1]]["exchange_confirmed"] is True
            assert exported[ADDRESSES[2]]["exchange_rejected"] is True

            json_text, json_type = service.export_library("json")
            json_rows = _parse_upload("backup.json", json_text)
            assert json_type == "application/json; charset=utf-8"
            assert {row["address"] for row in json_rows} == set(ADDRESSES)
        finally:
            database.engine.dispose()


def test_library_export_rejects_unknown_format():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "portable-library-format.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            with pytest.raises(InputError, match="csv or json"):
                service.export_library("txt")
        finally:
            database.engine.dispose()
