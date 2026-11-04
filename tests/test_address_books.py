from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import AddressBook, AddressLibrary, Database
from app.inputs import InputError
from app.jobs import RelationJobManager
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
NEIGHBORS = [
    "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN",
    "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z",
]


def build_service(directory, name="books.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def seed_public_addresses(service):
    service.import_rows(
        [{"address": NEIGHBORS[0], "category": "EXCHANGE", "label": "交易所"}], "manual"
    )
    service.import_rows(
        [{"address": NEIGHBORS[1], "category": "VALIDATOR", "label": "验证者"}], "manual"
    )


def test_create_address_book_imports_seed_and_skips_public_addresses():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            seed_public_addresses(service)

            result = service.create_address_book(
                name="  疑似同一实体  ",
                seed=SEED,
                addresses=NEIGHBORS,
                include_seed=True,
            )

            assert result["name"] == "疑似同一实体"
            assert result["total"] == 4
            assert result["inserted"] == 2  # 发起地址 + 1 个普通关联地址
            assert result["skipped_exchange"] == 1
            assert result["skipped_validator"] == 1

            with database.session() as db:
                rows = db.query(AddressLibrary).filter_by(book="疑似同一实体").all()
                addresses = {row.address for row in rows}
                assert addresses == {SEED, NEIGHBORS[2]}
                row = db.query(AddressLibrary).filter_by(address=SEED).one()
                assert row.category == "WATCH"
                assert row.label == "疑似同一实体"
                assert row.source == "book:疑似同一实体"
                assert "新建并导入" in row.note
                # 公共地址没有被改库名，也没有被改分类
                assert db.query(AddressLibrary).filter_by(address=NEIGHBORS[0]).one().book == ""

            books = service.address_books()
            assert [item["name"] for item in books] == ["疑似同一实体"]
            assert books[0]["count"] == 2
            assert books[0]["seed"] == SEED
        finally:
            database.engine.dispose()


def test_create_address_book_appends_and_keeps_existing_rows():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "append.db")
        try:
            service.import_rows(
                [{"address": NEIGHBORS[2], "label": "人工标签", "category": "WATCH", "note": "别改"}],
                "manual",
            )

            first = service.create_address_book(
                name="库A", seed=SEED, addresses=[NEIGHBORS[2]], include_seed=True
            )
            assert first["inserted"] == 1  # 只有发起地址是新的
            assert first["attached"] == 1  # 人工记录只补库名

            with database.session() as db:
                kept = db.query(AddressLibrary).filter_by(address=NEIGHBORS[2]).one()
                assert kept.label == "人工标签"
                assert kept.note == "别改"
                assert kept.book == "库A"

            second = service.create_address_book(
                name="库A", seed=SEED, addresses=[NEIGHBORS[2]], include_seed=True
            )
            assert second["inserted"] == 0
            assert second["already_in_book"] == 2
            assert second["total"] == 2

            other = service.create_address_book(
                name="库B", seed="", addresses=[NEIGHBORS[2]], include_seed=False
            )
            # 已属于库A的地址不会被抢到库B
            assert other["other_book"] == 1
            assert other["inserted"] == 0
        finally:
            database.engine.dispose()


def test_create_address_book_rejects_empty_or_public_only_imports():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "reject.db")
        try:
            seed_public_addresses(service)

            with pytest.raises(InputError):
                service.create_address_book(name="   ", seed=SEED, addresses=NEIGHBORS)

            with pytest.raises(InputError) as excinfo:
                service.create_address_book(
                    name="空库", seed="", addresses=[NEIGHBORS[0], NEIGHBORS[1]]
                )
            assert "交易所" in str(excinfo.value)

            with pytest.raises(InputError):
                service.create_address_book(
                    name="分类错误",
                    seed=SEED,
                    addresses=[NEIGHBORS[2]],
                    category="EXCHANGE",
                )

            assert service.address_books() == []
        finally:
            database.engine.dispose()


def test_dissolve_address_book_keeps_addresses():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "dissolve.db")
        try:
            service.create_address_book(
                name="临时库", seed=SEED, addresses=[NEIGHBORS[2]], include_seed=True
            )
            result = service.dissolve_address_book("临时库")

            assert result["addresses_kept"] == 2
            assert service.address_books() == []
            with database.session() as db:
                assert db.query(AddressLibrary).count() == 2
                assert db.query(AddressBook).count() == 0
        finally:
            database.engine.dispose()


def test_book_export_and_library_export_include_book_name():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "export.db")
        try:
            service.create_address_book(
                name="导出库", seed=SEED, addresses=[NEIGHBORS[2]], include_seed=True
            )

            content, media_type, filename = service.export_address_book("导出库", "txt")
            assert media_type.startswith("text/plain")
            assert filename == "导出库.txt"
            assert set(content.split()) == {SEED, NEIGHBORS[2]}

            csv_text, _, _ = service.export_address_book("导出库", "csv")
            assert "book" in csv_text.splitlines()[0]
            assert "导出库" in csv_text

            library_json, _ = service.export_library("json")
            assert '"book": "导出库"' in library_json
        finally:
            database.engine.dispose()


def test_book_endpoints_create_list_export_and_dissolve(monkeypatch):
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api.db")
        seed_public_addresses(service)
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                created = client.post(
                    "/api/library/books",
                    json={
                        "name": "接口库",
                        "seed": SEED,
                        "addresses": NEIGHBORS,
                        "include_seed": True,
                        "category": "WATCH",
                    },
                )
                assert created.status_code == 200
                body = created.json()
                assert body["inserted"] == 2
                assert body["skipped_exchange"] == 1
                assert body["skipped_validator"] == 1

                listing = client.get("/api/library/books").json()["items"]
                assert listing[0]["name"] == "接口库"
                assert listing[0]["count"] == 2

                exported = client.get("/api/library/books/接口库/export?format=txt")
                assert exported.status_code == 200
                assert set(exported.text.split()) == {SEED, NEIGHBORS[2]}

                bad = client.post(
                    "/api/library/books", json={"name": "", "addresses": [NEIGHBORS[2]]}
                )
                assert bad.status_code == 422

                missing = client.get("/api/library/books/没有这个库/export?format=txt")
                assert missing.status_code == 404

                dissolved = client.delete("/api/library/books/接口库")
                assert dissolved.status_code == 200
                assert dissolved.json()["addresses_kept"] == 2
                assert client.get("/api/library/books").json()["items"] == []
        finally:
            database.engine.dispose()
