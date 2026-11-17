from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

import app.main as main_module
import app.service as service_module
from app import settings_store
from app.collector import merge_subnet_owner_maps
from app.db import AddressBook, AddressLibrary, Database
from app.service import LinkService, SUBNET_OWNER_SOURCE


OWNER_A = "5HCFWvRqzSHWRPecN7q8J6c7aKQnrCZTMHstPv39xL1wgDHh"
OWNER_B = "5EcYQ3W77ndrmMWdvVQusoFqY8doxfP3U2zrh7xZQiaz7avY"
OWNER_C = "5GsGUrd21bnkNxQsH2grv474y4gcDwCmp5xJ72KeokspZ2bg"


def build_service(directory):
    database = Database(Path(directory) / "subnet-owners.db")
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def registry(netuid=55, name="NIOME", owner=OWNER_A, active=True):
    return {
        0: {
            "netuid": 0,
            "name": "root",
            "owner_address": "",
            "active": True,
        },
        netuid: {
            "netuid": netuid,
            "name": name,
            "owner_address": owner,
            "active": active,
        },
    }


def patch_registry(monkeypatch, value):
    monkeypatch.setattr(service_module, "query_subnet_owners", lambda _url: value)


def test_merge_subnet_owner_maps_keeps_name_owner_and_registration_status():
    merged = merge_subnet_owner_maps(
        [(55, True), (56, False), ("bad", True)],
        [(55, OWNER_A), (56, OWNER_B)],
        [(55, {"subnet_name": "  NIOME  "}), (56, {"subnet_name": "Old Name"})],
    )
    assert merged[55] == {
        "netuid": 55,
        "name": "NIOME",
        "owner_address": OWNER_A,
        "active": True,
    }
    assert merged[56]["active"] is False
    assert "bad" not in merged


def test_sync_creates_subnet_owner_book_with_number_name_and_note(monkeypatch):
    patch_registry(monkeypatch, registry())
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            result = service.sync_subnet_owner_addresses()
            assert result["inserted"] == 1
            assert result["active_total"] == 1
            assert result["archived"] == 0

            rows = service.library()
            assert len(rows) == 1
            assert rows[0]["address"] == OWNER_A
            assert rows[0]["book"] == "SN55 NIOME"
            assert "NIOME" in rows[0]["note"]
            assert rows[0]["source"] == SUBNET_OWNER_SOURCE
            books = service.address_books()
            assert books[0]["name"] == "SN55 NIOME"
            assert "项目方" in books[0]["note"]
        finally:
            database.engine.dispose()


def test_deregistered_subnet_is_archived_and_hidden(monkeypatch):
    patch_registry(monkeypatch, registry())
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_subnet_owner_addresses()
            patch_registry(monkeypatch, registry(active=False))
            result = service.sync_subnet_owner_addresses()
            assert result["archived"] == 1
            assert result["archived_items"][0]["netuid"] == 55
            assert service.library() == []
            assert service.address_books() == []
            status = service.subnet_owner_sync_status()
            assert status["active_total"] == 0
            assert status["archived_total"] == 1
            archived = service.subnet_owner_entries(include_archived=True)
            assert archived[0]["active"] is False
            assert archived[0]["archived_at"]
            with database.session() as db:
                assert db.query(AddressLibrary).filter(
                    AddressLibrary.source == SUBNET_OWNER_SOURCE
                ).count() == 0
                assert db.query(AddressBook).filter(
                    AddressBook.name.like("SN55 %")
                ).count() == 0
        finally:
            database.engine.dispose()


def test_reregistered_netuid_replaces_name_and_owner_and_hides_old_book(monkeypatch):
    patch_registry(monkeypatch, registry(name="Old Project", owner=OWNER_A))
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_subnet_owner_addresses()
            patch_registry(monkeypatch, registry(active=False))
            service.sync_subnet_owner_addresses()
            patch_registry(monkeypatch, registry(name="New Project", owner=OWNER_B))
            result = service.sync_subnet_owner_addresses()
            assert result["restored"] == 1
            entry = service.subnet_owner_entries()[0]
            assert entry["name"] == "New Project"
            assert entry["owner_address"] == OWNER_B
            rows = service.library()
            assert len(rows) == 1
            assert rows[0]["address"] == OWNER_B
            assert rows[0]["book"] == "SN55 New Project"
            assert [book["name"] for book in service.address_books()] == ["SN55 New Project"]
            with database.session() as db:
                project_rows = db.query(AddressLibrary).filter(
                    AddressLibrary.source == SUBNET_OWNER_SOURCE
                ).all()
                assert [row.address for row in project_rows] == [OWNER_B]
                assert [row.book for row in project_rows] == ["SN55 New Project"]
        finally:
            database.engine.dispose()


def test_same_owner_reregistration_reuses_owner_row_without_conflict(monkeypatch):
    patch_registry(monkeypatch, registry(name="Old Project", owner=OWNER_A))
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_subnet_owner_addresses()
            patch_registry(monkeypatch, registry(active=False))
            service.sync_subnet_owner_addresses()
            patch_registry(monkeypatch, registry(name="New Project", owner=OWNER_A))
            result = service.sync_subnet_owner_addresses()
            assert result["owner_conflicts"] == []
            rows = service.library()
            assert len(rows) == 1
            assert rows[0]["address"] == OWNER_A
            assert rows[0]["book"] == "SN55 New Project"
            assert [book["name"] for book in service.address_books()] == ["SN55 New Project"]
        finally:
            database.engine.dispose()


def test_fast_reregistration_without_observed_archive_removes_old_owner(monkeypatch):
    """两次同步之间完成注销+重注册时，旧 owner 也必须物理移出 SN 库。"""

    patch_registry(monkeypatch, registry(netuid=113, name="SN113", owner=OWNER_A))
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_subnet_owner_addresses()
            patch_registry(monkeypatch, registry(netuid=113, name="SN113", owner=OWNER_B))
            result = service.sync_subnet_owner_addresses()

            assert result["updated"] == 1
            item = result["updated_items"][0]
            assert item["netuid"] == 113
            assert item["old_name"] == item["name"] == "SN113"
            assert item["old_address"] == OWNER_A
            assert item["address"] == OWNER_B

            with database.session() as db:
                rows = db.query(AddressLibrary).filter(
                    AddressLibrary.source == SUBNET_OWNER_SOURCE
                ).all()
                assert [(row.address, row.book) for row in rows] == [
                    (OWNER_B, "SN113 SN113")
                ]
                assert db.query(AddressBook).filter(
                    AddressBook.name.like("SN113 %")
                ).count() == 1
        finally:
            database.engine.dispose()


def test_sync_toast_names_updated_subnet_and_old_new_owner():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "function describeSubnetOwnerChanges" in js
    assert "old_address" in js
    assert "更新 ${data.updated} 个：" in js


def test_auto_sync_respects_weekly_interval(monkeypatch):
    patch_registry(monkeypatch, registry())
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.sync_subnet_owner_addresses()
            recent = service.auto_sync_subnet_owners()
            assert recent["status"] == "skipped"
            assert recent["reason"] == "recent"

            state = settings_store.load_subnet_owner_sync_state()
            state["attempted_at"] = "2000-01-01T00:00:00+00:00"
            settings_store.save_subnet_owner_sync_state(state)
            synced = service.auto_sync_subnet_owners()
            assert synced["status"] == "synced"
        finally:
            database.engine.dispose()


def test_subnet_owner_endpoints(monkeypatch):
    patch_registry(monkeypatch, registry())
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            main_module.app.dependency_overrides[main_module.service] = lambda: service
            client = TestClient(main_module.app)
            try:
                response = client.post("/api/library/import-subnet-owners")
                assert response.status_code == 200
                assert response.json()["inserted"] == 1
                status = client.get("/api/library/subnet-owner-sync")
                assert status.status_code == 200
                assert status.json()["active_total"] == 1
                items = client.get("/api/library/subnet-owners").json()["items"]
                assert items[0]["netuid"] == 55
            finally:
                main_module.app.dependency_overrides.clear()
        finally:
            database.engine.dispose()
