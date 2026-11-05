"""组内手动添加地址 + 交易所人工结论改判。

对应两个使用场景：
1. 地址库清单展开后，直接往这一组里手打/粘贴地址；
2. 人工否定（或确认）交易所后判错了，随时能改回来。
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.db import AddressLibrary, Database
from app.inputs import InputError
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
EXTRA = [
    "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN",
    "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z",
]


def build_service(directory, name="group-add.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def test_add_group_addresses_newlines_commas_and_note():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(
                name="某群", seed=SEED, addresses=[EXTRA[0]], category="群友", include_seed=True
            )
            payload = "  ".join([EXTRA[1], EXTRA[1]]) + f"\n{EXTRA[2]}，"
            result = service.add_group_addresses(
                payload, book="某群", label="某群", category="群友", note="手动补录"
            )

            # 同一地址写两次只加一次
            assert result["inserted"] == 2
            assert result["skipped"] == 0
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=EXTRA[2]).one()
                assert row.book == "某群"
                assert row.label == "某群"
                assert row.category == "群友"
                assert row.note == "手动补录"
                assert row.source == "manual:group-add"
                assert "群友" in row.categories
        finally:
            database.engine.dispose()


def test_add_group_addresses_skips_existing_without_touching_them():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "group-add-skip.db")
        try:
            service.import_rows(
                [{"address": EXTRA[0], "category": "大V", "label": "原有名字", "note": "原有备注"}],
                "manual",
            )
            result = service.add_group_addresses(
                EXTRA[0], book="别的库", label="别的组", category="群友", note="新备注"
            )

            assert result["inserted"] == 0
            assert result["skipped"] == 1
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=EXTRA[0]).one()
                assert row.label == "原有名字"
                assert row.note == "原有备注"
                assert row.category == "大V"
                assert row.book == ""
        finally:
            database.engine.dispose()


def test_add_group_addresses_reports_invalid_and_allows_undo():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "group-add-undo.db")
        try:
            with pytest.raises(InputError, match="没有有效的 SS58"):
                service.add_group_addresses("not-an-address", book="某库")

            result = service.add_group_addresses(
                f"{EXTRA[0]}\nnot-an-address", book="某库", label="某库", category="其他"
            )

            assert result["inserted"] == 1
            assert result["invalid_count"] == 1
            assert result["invalid"] == ["not-an-address"]

            # 手动添加也写撤回快照，加错了能整体撤回。
            undo = service.undo_last_import()
            assert undo["deleted"] == 1
            with database.session() as db:
                assert db.query(AddressLibrary).filter_by(address=EXTRA[0]).first() is None
        finally:
            database.engine.dispose()


def test_exchange_decision_can_be_corrected_both_ways():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "exchange-correct.db")
        try:
            service.import_rows([{"address": EXTRA[0], "category": "大V"}], "manual")
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=EXTRA[0]).one()
                assert row.label == ""

            # 先否定
            rejected = service.decide_exchange_address(EXTRA[0], "reject")["item"]
            assert rejected["exchange_rejected"] is True
            assert rejected["exchange_confirmed"] is False
            assert rejected["label"] == "人工否定交易所"

            # 判错了 → 改判为交易所
            confirmed = service.decide_exchange_address(EXTRA[0], "confirm")["item"]
            assert confirmed["exchange_confirmed"] is True
            assert confirmed["exchange_rejected"] is False
            assert confirmed["label"] == "人工确认交易所"
            assert confirmed["category"] == "EXCHANGE"

            # 再改回去（否定），来回都能改
            again = service.decide_exchange_address(EXTRA[0], "reject")["item"]
            assert again["exchange_rejected"] is True
            assert again["exchange_confirmed"] is False

            # 重置回待确认
            reset = service.decide_exchange_address(EXTRA[0], "reset")["item"]
            assert reset["exchange_confirmed"] is False
            assert reset["exchange_rejected"] is False
        finally:
            database.engine.dispose()


def test_exchange_correction_keeps_manual_group_name():
    """人工改判不应该把人给这一组起的名字冲掉。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "exchange-label.db")
        try:
            service.import_rows(
                [{"address": EXTRA[1], "category": "EXCHANGE", "label": "某交易所·热钱包"}], "manual"
            )

            service.decide_exchange_address(EXTRA[1], "reject")
            service.decide_exchange_address(EXTRA[1], "confirm")

            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=EXTRA[1]).one()
                assert row.label == "某交易所·热钱包"
                assert row.exchange_confirmed is True
        finally:
            database.engine.dispose()


def test_group_addresses_endpoint_and_exchange_endpoint(monkeypatch):
    """接口层：组内加地址 / 改判交易所都能走通 HTTP。"""

    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.jobs import RelationJobManager

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api-group-add.db")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                added = client.post(
                    "/api/library/group-addresses",
                    json={
                        "addresses": f"{EXTRA[0]}\n{EXTRA[1]}",
                        "book": "接口库",
                        "label": "接口库",
                        "category": "其他",
                        "note": "接口补录",
                    },
                )
                assert added.status_code == 200
                body = added.json()
                assert body["inserted"] == 2
                assert body["skipped"] == 0
                assert body["group"] == "接口库"

                again = client.post(
                    "/api/library/group-addresses",
                    json={"addresses": EXTRA[0], "book": "接口库", "label": "接口库"},
                )
                assert again.status_code == 200
                assert again.json()["skipped"] == 1

                bad = client.post(
                    "/api/library/group-addresses",
                    json={"addresses": "not-an-address", "book": "接口库"},
                )
                assert bad.status_code == 422

                rejected = client.post(
                    "/api/library/confirm-exchange-address",
                    json={"address": EXTRA[0], "decision": "reject"},
                )
                assert rejected.status_code == 200
                assert rejected.json()["item"]["exchange_rejected"] is True
                # 接口默认不传 label，原有的组名要保留
                assert rejected.json()["item"]["label"] == "接口库"

                fixed = client.post(
                    "/api/library/confirm-exchange-address",
                    json={"address": EXTRA[0], "decision": "confirm"},
                )
                assert fixed.status_code == 200
                item = fixed.json()["item"]
                assert item["exchange_confirmed"] is True
                assert item["exchange_rejected"] is False
        finally:
            database.engine.dispose()


def test_group_addresses_endpoint_and_exchange_endpoint(monkeypatch):
    """接口层：组内加地址 / 改判交易所都能走通 HTTP。"""

    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.jobs import RelationJobManager

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api-group-add.db")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                added = client.post(
                    "/api/library/group-addresses",
                    json={
                        "addresses": f"{EXTRA[0]}\n{EXTRA[1]}",
                        "book": "接口库",
                        "label": "接口库",
                        "category": "其他",
                        "note": "接口补录",
                    },
                )
                assert added.status_code == 200
                body = added.json()
                assert body["inserted"] == 2
                assert body["skipped"] == 0
                assert body["group"] == "接口库"

                again = client.post(
                    "/api/library/group-addresses",
                    json={"addresses": EXTRA[0], "book": "接口库", "label": "接口库"},
                )
                assert again.status_code == 200
                assert again.json()["skipped"] == 1

                bad = client.post(
                    "/api/library/group-addresses",
                    json={"addresses": "not-an-address", "book": "接口库"},
                )
                assert bad.status_code == 422

                rejected = client.post(
                    "/api/library/confirm-exchange-address",
                    json={"address": EXTRA[0], "decision": "reject"},
                )
                assert rejected.status_code == 200
                assert rejected.json()["item"]["exchange_rejected"] is True
                # 接口默认不传 label，原有的组名要保留
                assert rejected.json()["item"]["label"] == "接口库"

                fixed = client.post(
                    "/api/library/confirm-exchange-address",
                    json={"address": EXTRA[0], "decision": "confirm"},
                )
                assert fixed.status_code == 200
                item = fixed.json()["item"]
                assert item["exchange_confirmed"] is True
                assert item["exchange_rejected"] is False
        finally:
            database.engine.dispose()


def test_existing_address_reports_conflict_and_can_be_moved():
    """已在别的组的地址：默认跳过并报冲突，勾选「移过来」才转组。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(
                name="旧库", seed=SEED, addresses=[EXTRA[0]], category="群友", include_seed=True
            )
            # 默认只新增：跳过并告诉前端它在哪个组
            first = service.add_group_addresses(
                EXTRA[0], label="人工确认交易所", category="EXCHANGE"
            )
            assert first["inserted"] == 0
            assert first["moved"] == 0
            assert first["skipped"] == 1
            assert first["conflict_count"] == 1
            assert first["conflicts"][0]["group"] == "旧库"
            with database.session() as db:
                assert db.query(AddressLibrary).filter_by(address=EXTRA[0]).one().book == "旧库"

            # 勾选移过来：改归属，备注保留
            moved = service.add_group_addresses(
                EXTRA[0],
                label="人工确认交易所",
                category="EXCHANGE",
                move_existing=True,
            )
            assert moved["moved"] == 1
            assert moved["inserted"] == 0
            assert moved["conflict_count"] == 0
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=EXTRA[0]).one()
                assert row.book == ""
                assert row.label == "人工确认交易所"
                assert row.category == "EXCHANGE"
                assert row.source == "manual:exchange-confirmation"
                assert row.exchange_confirmed is True

            # 撤回能把它还原回旧库
            service.undo_last_import()
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=EXTRA[0]).one()
                assert row.book == "旧库"
                assert row.label == "旧库"
                assert row.exchange_confirmed is False
        finally:
            database.engine.dispose()


def test_adding_into_exchange_group_sets_decision():
    """往「人工确认交易所 / 人工否定交易所」组加新地址，同步写交易所结论。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            added = service.add_group_addresses(
                EXTRA[0], label="人工确认交易所", category="EXCHANGE"
            )
            assert added["inserted"] == 1
            rejected = service.add_group_addresses(
                EXTRA[1], label="人工否定交易所", category="EXCHANGE"
            )
            assert rejected["inserted"] == 1
            with database.session() as db:
                ok = db.query(AddressLibrary).filter_by(address=EXTRA[0]).one()
                no = db.query(AddressLibrary).filter_by(address=EXTRA[1]).one()
                assert ok.exchange_confirmed is True and ok.exchange_rejected is False
                assert no.exchange_rejected is True and no.exchange_confirmed is False
                # 普通组不会乱写标志
            plain = service.add_group_addresses(EXTRA[2], book="普通库", label="普通库")
            assert plain["inserted"] == 1
            with database.session() as db:
                row = db.query(AddressLibrary).filter_by(address=EXTRA[2]).one()
                assert row.exchange_confirmed is False
                assert row.exchange_rejected is False
        finally:
            database.engine.dispose()
