"""群组合并：把交叉命中同一批地址的几个库/标签组并成一个。

覆盖：合并成新名字、继承原有组名、混自建库与标签组、撤回还原、
以及各种拒绝情况（不足两组 / 目标名被别的库占用 / 组不存在）。
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.db import AddressBook, AddressLibrary, Database
from app.inputs import InputError
from app.service import LinkService

A1 = "5HPcksyFuWSZS6Qqsr9wQcxz2t69x9hYBuS5QTFqVxHEMuKt"
A2 = "5C7yHzT1kbhDbRYSQbhwuSCCXoftsSZS5GGjgNzTHHzHgygN"
A3 = "5CrmCnoTqxpCuZoSjYRNb3FucZgWhYBCAc6yQcwoVyVT7oQ5"
A4 = "5H5J2LgZCfodSQhq5W4TFTb3rmQcczrCfGD2DDZSrJcXiihy"
A5 = "5H8Jaq6wWjKytpVRSgaLjuP5HnpUCvozq4M84B2TvZyy3zBh"
A6 = "5EzrtojKtG5eJR6UkSFcnx5Uq2D5AzjMXQwRqV2fYRhBXEPU"


def build_service(directory, name="merge.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def seed_two_books(service, database):
    service.create_address_book(
        name="羊A", seed=A1, addresses=[A2], category="群友", include_seed=True
    )
    service.create_address_book(
        name="羊B", seed=A3, addresses=[A4], category="大V", include_seed=True
    )
    with database.session() as db:
        db.query(AddressLibrary).filter_by(address=A2).one().note = "A 库的备注"


def test_merge_into_new_name_keeps_facts_and_drops_source_books():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            seed_two_books(service, database)
            # 0.8.18 起否定交易所会把地址移出库，这里改用"人工否定"的写法：
            # 先否定拿到 exchange_rejected，再手动加回库（模拟"以后另行添加"）。
            service.decide_exchange_address(A2, "reject")
            service.add_group_addresses(A2, book="羊A", move_existing=True)

            result = service.merge_address_groups(
                [{"kind": "book", "name": "羊A"}, {"kind": "book", "name": "羊B"}],
                "合并后的羊",
            )

            assert result["merged"] == 4
            assert result["groups"] == 2
            assert result["name"] == "合并后的羊"
            assert result["created_book"] is True
            with database.session() as db:
                rows = db.query(AddressLibrary).filter_by(book="合并后的羊").all()
                assert len(rows) == 4
                for row in rows:
                    assert row.label == "合并后的羊"
                merged_a2 = db.query(AddressLibrary).filter_by(address=A2).one()
                # 备注、交易所人工结论都保留；分类被改判成 EXCHANGE 也是原样保留
                assert merged_a2.note == "A 库的备注"
                assert merged_a2.exchange_rejected is True
                assert merged_a2.category == "EXCHANGE"
                merged_a1 = db.query(AddressLibrary).filter_by(address=A1).one()
                assert merged_a1.category == "群友"
                merged_a4 = db.query(AddressLibrary).filter_by(address=A4).one()
                assert merged_a4.category == "大V"
                # 旧库名不再挂在任何地址上，库表也只剩新库
                assert db.query(AddressLibrary).filter_by(book="羊A").count() == 0
                assert db.query(AddressLibrary).filter_by(book="羊B").count() == 0
                names = {item.name for item in db.query(AddressBook).all()}
                assert "合并后的羊" in names
                assert "羊A" not in names and "羊B" not in names
        finally:
            database.engine.dispose()


def test_merge_inherits_existing_group_name():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            seed_two_books(service, database)

            result = service.merge_address_groups(
                [{"kind": "book", "name": "羊A"}, {"kind": "book", "name": "羊B"}],
                "羊A",
            )

            assert result["merged"] == 4
            assert result["created_book"] is False
            with database.session() as db:
                assert db.query(AddressLibrary).filter_by(book="羊A").count() == 4
                assert db.query(AddressLibrary).filter_by(book="羊B").count() == 0
                names = {item.name for item in db.query(AddressBook).all()}
                assert names == {"羊A"}
        finally:
            database.engine.dispose()


def test_merge_label_group_and_book_together():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            # 一个自建库 + 一个人工命名的标签组
            service.create_address_book(
                name="库一", seed=A1, addresses=[A2], category="群友", include_seed=True
            )
            service.add_group_addresses(A5, label="散乱标签", category="其他")
            service.add_group_addresses(A6, label="散乱标签", category="其他")

            result = service.merge_address_groups(
                [{"kind": "book", "name": "库一"}, {"kind": "label", "name": "散乱标签"}],
                "一起看",
            )

            assert result["merged"] == 4
            with database.session() as db:
                rows = db.query(AddressLibrary).filter_by(book="一起看").all()
                assert {row.address for row in rows} == {A1, A2, A5, A6}
                # 原标签组的地址不再留在旧标签下
                assert db.query(AddressLibrary).filter_by(label="散乱标签").count() == 0
        finally:
            database.engine.dispose()


def test_merge_is_undoable():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            seed_two_books(service, database)
            service.merge_address_groups(
                [{"kind": "book", "name": "羊A"}, {"kind": "book", "name": "羊B"}],
                "临时合并",
            )

            service.undo_last_import()

            with database.session() as db:
                assert db.query(AddressLibrary).filter_by(book="羊A").count() == 2
                assert db.query(AddressLibrary).filter_by(book="羊B").count() == 2
                assert db.query(AddressLibrary).filter_by(book="临时合并").count() == 0
                names = {item.name for item in db.query(AddressBook).all()}
                assert {"羊A", "羊B"} <= names
                assert "临时合并" not in names
        finally:
            database.engine.dispose()


def test_merge_rejects_bad_input():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            seed_two_books(service, database)
            service.create_address_book(
                name="别的库", seed=A5, addresses=[A6], category="其他", include_seed=True
            )

            with pytest.raises(InputError):
                service.merge_address_groups([{"kind": "book", "name": "羊A"}], "只有一个")
            with pytest.raises(InputError):
                # 目标名已是别的库，不能直接吞并
                service.merge_address_groups(
                    [{"kind": "book", "name": "羊A"}, {"kind": "book", "name": "羊B"}],
                    "别的库",
                )
            with pytest.raises(InputError):
                service.merge_address_groups(
                    [{"kind": "book", "name": "不存在"}, {"kind": "book", "name": "羊A"}],
                    "随便",
                )
            with pytest.raises(InputError):
                service.merge_address_groups(
                    [{"kind": "weird", "name": "x"}, {"kind": "book", "name": "羊A"}],
                    "随便",
                )
            # 失败的合并不该动数据
            with database.session() as db:
                assert db.query(AddressLibrary).filter_by(book="羊A").count() == 2
                assert db.query(AddressLibrary).filter_by(book="别的库").count() == 2
        finally:
            database.engine.dispose()


def test_merge_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.jobs import RelationJobManager

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "merge-api.db")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                for name, seed, extra in (("甲", A1, A2), ("乙", A3, A4)):
                    created = client.post(
                        "/api/library/books",
                        json={
                            "name": name,
                            "seed": seed,
                            "addresses": [extra],
                            "include_seed": True,
                            "category": "其他",
                        },
                    )
                    assert created.status_code == 200

                merged = client.post(
                    "/api/library/merge-groups",
                    json={
                        "sources": [
                            {"kind": "book", "name": "甲"},
                            {"kind": "book", "name": "乙"},
                        ],
                        "target": "甲乙",
                    },
                )
                assert merged.status_code == 200
                assert merged.json()["merged"] == 4

                bad = client.post(
                    "/api/library/merge-groups",
                    json={"sources": [{"kind": "book", "name": "甲"}], "target": "x"},
                )
                assert bad.status_code == 422
        finally:
            database.engine.dispose()
