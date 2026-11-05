"""地址库分类：大V / 群友 / 其他 + 用户自建。

分类只描述“这个地址是谁”，交易所/验证者属于系统分类，不参与人工选择。
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import text

from app.db import AddressLibrary, Database
from app.inputs import InputError
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
NEIGHBORS = [
    "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN",
    "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z",
]


def build_service(directory, name="categories.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def names(service):
    return [item["name"] for item in service.categories()]


def test_default_categories_are_seeded_once():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            assert names(service) == ["大V", "群友", "其他", "系统"]
            assert all(item["builtin"] for item in service.categories())
            # 再次 initialize 不应重复插入
            database.initialize()
            assert names(service) == ["大V", "群友", "其他", "系统"]
        finally:
            database.engine.dispose()


def test_add_category_is_idempotent_and_validated():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            assert service.add_category("  项目方 ") == {"created": True, "name": "项目方"}
            assert service.add_category("项目方")["created"] is False
            assert names(service)[-1] == "项目方"
            assert service.categories()[-1]["builtin"] is False

            for bad in ("", "   ", "X" * 13):
                with pytest.raises(InputError):
                    service.add_category(bad)
            for reserved in ("EXCHANGE", "validator", "WATCH"):
                with pytest.raises(InputError):
                    service.add_category(reserved)
        finally:
            database.engine.dispose()


def test_delete_category_moves_addresses_to_default():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.add_category("做市商")
            service.import_rows(
                [{"address": NEIGHBORS[0], "category": "做市商", "label": "做市"}], "manual"
            )
            assert service.categories()[-1]["count"] == 1

            result = service.delete_category("做市商")
            assert result["moved_to_default"] == 1
            assert "做市商" not in names(service)
            row = service.library()[0]
            assert row["category"] == "其他"

            with pytest.raises(InputError):
                service.delete_category("大V")
            with pytest.raises(InputError):
                service.delete_category("不存在的分类")
        finally:
            database.engine.dispose()


def test_category_book_and_sync_use_selected_category():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            created = service.create_address_book(
                name="大V 群", seed=SEED, addresses=NEIGHBORS[:1], category="大V"
            )
            assert created["category"] == "大V"
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=SEED).one()
                assert row.category == "大V"

            synced = service.sync_relation_addresses(
                seed=NEIGHBORS[2],
                addresses=NEIGHBORS[:1],
                category="群友",
                label="群里看到的",
            )
            assert synced["category"] == "群友"
            assert synced["inserted"] == 1  # 邻居 0 已经随上面建库入库了
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=NEIGHBORS[2]).one()
                assert row.category == "群友"
                assert row.label == "群里看到的"

            # 已在库、但还挂在默认“其他”上的地址，选分类同步时会归位；
            # 已经人工分好类（大V）的不会被改。
            service.import_rows([{"address": NEIGHBORS[1], "label": "未分类"}], "manual")
            again = service.sync_relation_addresses(
                seed="", addresses=[SEED, NEIGHBORS[1]], category="群友"
            )
            assert again["inserted"] == 0
            assert again["recategorized"] == 1
            with database.session() as db:
                assert db.query(AddressLibrary).filter_by(address=NEIGHBORS[1]).one().category == "群友"
                assert db.query(AddressLibrary).filter_by(address=SEED).one().category == "大V"

            # 自定义分类可以直接用，并且会出现在分类清单里
            service.create_address_book(
                name="自建分类库", seed="", addresses=[NEIGHBORS[1]], include_seed=False,
                category="打新党",
            )
            assert "打新党" in names(service)

            for bad in ("EXCHANGE", "VALIDATOR"):
                with pytest.raises(InputError):
                    service.create_address_book(
                        name="非法库", seed="", addresses=[], category=bad
                    )
                with pytest.raises(InputError):
                    service.sync_relation_addresses(
                        seed="", addresses=[], category=bad
                    )
        finally:
            database.engine.dispose()


def test_legacy_english_categories_are_migrated():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO address_library "
                        "(address, label, category, note, source, book, "
                        " exchange_confirmed, exchange_rejected, created_at, updated_at) "
                        "VALUES (:address, '老记录', 'WATCH', '', 'manual', '', 0, 0, :created, :created)"
                    ),
                    {"address": NEIGHBORS[0], "created": "2026-01-01T00:00:00+00:00"},
                )
            database.initialize()
            assert service.library()[0]["category"] == "其他"

            # 导出的旧 CSV 里写 SEED/BLACKLIST 也能正常折算
            service.import_rows(
                [{"address": NEIGHBORS[1], "category": "BLACKLIST", "label": "旧黑名单"}],
                "old.csv",
            )
            row = [
                item for item in service.library() if item["address"] == NEIGHBORS[1]
            ][0]
            assert row["category"] == "其他"
        finally:
            database.engine.dispose()


def test_category_label_marks_system_categories():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows(
                [{"address": NEIGHBORS[0], "category": "EXCHANGE", "label": "某所"}],
                "manual",
            )
            item = service.library()[0]
            assert item["category"] == "EXCHANGE"
            assert item["category_label"] == "交易所（系统）"
            # 系统分类不出现在可选分类里
            assert "EXCHANGE" not in names(service)
        finally:
            database.engine.dispose()


def test_api_categories_endpoints():
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            client = TestClient(main_module.app)
            assert [item["name"] for item in client.get("/api/library/categories").json()["items"]] == [
                "大V",
                "群友",
                "其他",
                "系统",
            ]
            created = client.post("/api/library/categories", json={"name": "基金"})
            assert created.status_code == 200
            assert created.json()["created"] is True
            assert client.post("/api/library/categories", json={"name": "EXCHANGE"}).status_code == 422

            client.post(
                "/api/library",
                json={"address": NEIGHBORS[0], "category": "基金", "label": "某基金"},
            )
            assert client.get("/api/library").json()["items"][0]["category"] == "基金"

            removed = client.delete("/api/library/categories/%E5%9F%BA%E9%87%91")
            assert removed.status_code == 200
            assert removed.json()["moved_to_default"] == 1
        finally:
            main_module.SERVICE = original
            database.engine.dispose()
