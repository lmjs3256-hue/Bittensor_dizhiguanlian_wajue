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
            assert created["item"]["category"] == "WATCH"
            assert created["item"]["note"] == "关联地址备注"
            saved = {item["address"]: item for item in service.library()}
            assert saved[ADDRESSES[1]]["note"] == "关联地址备注"
        finally:
            database.engine.dispose()


def test_dsv_source_without_label_is_presented_as_one_group_label():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "dsv-label.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows(
                [{"address": ADDRESSES[0], "category": "WATCH"}],
                "DSV_normalized.txt",
            )
            assert service.library()[0]["label"] == "DSV"
        finally:
            database.engine.dispose()
