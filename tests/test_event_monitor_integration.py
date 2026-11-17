from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.service import LinkService
from app.event_monitor.default_watch import seed_default_subnet_watch
from app.event_monitor.legacy_inherit import inherit_legacy_event_state
from app.event_monitor.storage import Store
from tools.migrate_event_monitor import migrate_event_database


ADDRESS = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
ADDRESS_TWO = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"


@pytest.fixture
def merged_client(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WALLET_LINK_EVENT_CHAIN_ENABLED", "0")
    monkeypatch.setenv("WALLET_LINK_EVENT_GITHUB_ENABLED", "0")
    monkeypatch.setenv("WALLET_LINK_EVENT_DB", str(tmp_path / "event_monitor.db"))
    database = Database(tmp_path / "wallet_link.db")
    service = LinkService(database, main_module.CONFIG.rpc_url)
    monkeypatch.setattr(main_module, "DATABASE", database)
    monkeypatch.setattr(main_module, "SERVICE", service)
    monkeypatch.setattr(main_module, "EVENT_MONITOR_ENABLED", True)
    monkeypatch.setattr(main_module, "SEED_BOOKS_ENABLED", False)
    monkeypatch.setattr(main_module, "BUILTIN_ADDRESSES_ENABLED", False)
    monkeypatch.setattr(main_module, "AUTO_EXCHANGE_SYNC_ENABLED", False)
    monkeypatch.setattr(main_module, "AUTO_SUBNET_OWNER_SYNC_ENABLED", False)
    with TestClient(main_module.app) as client:
        yield client, database
    main_module.EVENT_MONITOR.stop()
    database.engine.dispose()




def create_book(client, name: str, addresses: list[str]) -> None:
    response = client.post(
        "/api/library/books",
        json={
            "name": name,
            "addresses": addresses,
            "include_seed": False,
            "category": "其他",
            "note": "事件监控集成测试",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["inserted"] >= 1

def test_event_monitor_page_assets_and_proxy_are_merged(merged_client):
    client, _ = merged_client

    page = client.get("/event-monitor?embed=1")
    assert page.status_code == 200
    assert "监控地址" in page.text
    assert "/static/event-monitor/app.js?v=1.0.5" in page.text

    assert client.get("/static/event-monitor/styles.css").status_code == 200
    assert client.get("/static/event-monitor/app.js").status_code == 200
    assert client.get("/event-monitor/floating?mode=window").status_code == 200

    health = client.get("/event-api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    runtime = client.get("/api/event-monitor/runtime").json()
    assert runtime["running"] is True
    assert runtime["port"] > 0
    assert runtime["settings"]["database"].endswith("event_monitor.db")
    assert client.get("/api/health").json()["event_monitor"]["running"] is True
    assert client.get("/event-api/product").json()["product"]["version"] == "1.0.5"


def test_address_event_api_uses_current_manual_note_without_rewriting_summary(merged_client):
    client, _ = merged_client
    created = client.post(
        "/event-api/addresses",
        json={
            "network": "finney",
            "addresses": [ADDRESS],
            "label": "SN53 项目方",
            "library_book": "SN53 NIOME",
            "manual_note": "回购",
        },
    )
    assert created.status_code == 201
    address_id = created.json()["address"]["address_id"]

    simulated = client.post(
        "/event-api/simulation/events",
        json={
            "netuid": 53,
            "signal_id": "address_stake_removed",
            "address_id": address_id,
        },
    )
    assert simulated.status_code == 202, simulated.text
    event_id = simulated.json()["event"]["event_id"]
    assert "【" not in simulated.json()["event"]["summary"]

    updated = client.put(
        f"/event-api/addresses/{address_id}",
        json={"manual_note": "官方、出货"},
    )
    assert updated.status_code == 200

    listed = client.get("/event-api/events", params={"source": "address", "limit": 10}).json()
    event = next(item for item in listed["events"] if item["event_id"] == event_id)
    matched = event["details"]["matched_addresses"][0]
    assert event["summary"] == simulated.json()["event"]["summary"]
    assert matched["manual_note"] == "官方、出货"
    assert matched["manual_note_tags"] == ["【官方】", "【出货】"]
    assert "【SN53 项目方】" in matched["display_tags"]

    detail = client.get(f"/event-api/events/{event_id}").json()["event"]
    assert detail["details"]["matched_addresses"][0]["manual_note"] == "官方、出货"
    assert detail["summary"] == simulated.json()["event"]["summary"]


def test_shared_library_does_not_auto_populate_monitor_list(merged_client):
    client, _ = merged_client

    create_book(client, "共享测试地址库", [ADDRESS])
    client.post(
        "/api/library",
        json={"address": ADDRESS_TWO, "category": "其他", "label": "未归档单地址", "note": ""},
    )

    shared_payload = client.get("/api/event-monitor/library").json()
    shared = shared_payload["items"]
    assert [(item["address"], item["book"]) for item in shared] == [(ADDRESS, "共享测试地址库")]
    assert shared_payload["unbooked_count"] >= 1

    watched = client.get("/event-api/addresses").json()["addresses"]
    assert watched == []


def test_confirming_shared_address_starts_monitoring_but_delete_keeps_library(merged_client):
    client, database = merged_client
    create_book(client, "项目观察库", [ADDRESS, ADDRESS_TWO])

    created = client.post(
        "/event-api/addresses",
        json={
            "network": "finney",
            "addresses": [ADDRESS],
            "label": "项目方观察",
            "library_book": "项目观察库",
            "min_transfer_tao": "0",
        },
    )
    assert created.status_code == 201
    assert created.json()["created"] == 1

    watched = client.get("/event-api/addresses").json()["addresses"]
    assert [item["address"] for item in watched] == [ADDRESS]
    assert watched[0]["enabled"] is True
    assert watched[0]["library_book"] == "项目观察库"
    assert watched[0]["manual_note"] == ""

    updated = client.put(
        f"/event-api/addresses/{watched[0]['address_id']}",
        json={"manual_note": "回购、官方"},
    )
    assert updated.status_code == 200
    assert updated.json()["address"]["label"] == "项目方观察"
    assert updated.json()["address"]["manual_note"] == "回购、官方"

    removed = client.delete(f"/event-api/addresses/{watched[0]['address_id']}")
    assert removed.status_code == 200
    assert client.get("/event-api/addresses").json()["addresses"] == []
    shared = client.get("/api/event-monitor/library").json()["items"]
    assert {item["address"] for item in shared} == {ADDRESS, ADDRESS_TWO}

    event_database = main_module.EVENT_MONITOR.settings.database
    with sqlite3.connect(database.path) as db:
        library_count = db.execute(
            "SELECT COUNT(*) FROM address_library WHERE address=?", (ADDRESS,)
        ).fetchone()[0]
    with sqlite3.connect(event_database) as db:
        watched_count = db.execute(
            "SELECT COUNT(*) FROM watched_addresses WHERE address=?", (ADDRESS,)
        ).fetchone()[0]
    assert library_count == 1
    assert watched_count == 0


def test_event_and_wallet_databases_are_isolated_but_share_library_api(merged_client):
    client, wallet_database = merged_client
    create_book(client, "独立事件库测试", [ADDRESS_TWO])
    client.post(
        "/event-api/addresses",
        json={
            "network": "finney",
            "addresses": [ADDRESS_TWO],
            "label": "独立事件库监控",
            "library_book": "独立事件库测试",
        },
    )

    event_database = main_module.EVENT_MONITOR.settings.database
    assert Path(wallet_database.path).resolve() != Path(event_database).resolve()

    with sqlite3.connect(wallet_database.path) as db:
        wallet_tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"address_library", "transfers"} <= wallet_tables
        assert "watched_addresses" not in wallet_tables
        assert db.execute("SELECT COUNT(*) FROM address_library").fetchone()[0] == 1

    with sqlite3.connect(event_database) as db:
        event_tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"events", "watched_addresses"} <= event_tables
        assert "address_library" not in event_tables
        assert db.execute("SELECT COUNT(*) FROM watched_addresses").fetchone()[0] == 1

    shared = client.get("/api/event-monitor/library").json()["items"]
    assert [item["address"] for item in shared] == [ADDRESS_TWO]


def test_legacy_event_db_migration_preserves_events_but_clears_watchlist(tmp_path):
    source = tmp_path / "legacy-events.db"
    target = tmp_path / "event_monitor.db"
    source_store = Store(source)
    source_store.add_watched_address("finney", ADDRESS, "旧监控地址")
    event = {
        "event_id": "a" * 64,
        "network": "finney",
        "netuid": 55,
        "event_type": "身份信息变更",
        "event_name": "SubtensorModule.SubnetIdentitySet",
        "source": "chain",
        "source_url": None,
        "event_at": "2026-11-17T00:00:00.000Z",
        "summary": "SN55 · 历史事件",
        "details": {"rule_id": "subnet_identity_set"},
        "raw_data": {"attributes": {"netuid": 55}},
        "block_number": 100,
        "block_hash": "0x" + "b" * 64,
        "event_index": 1,
        "runtime_spec_version": 491,
        "chain_status": "observed",
        "enrichment_status": "complete",
        "confidence": "high",
    }
    source_store.create_event(event)

    result = migrate_event_database(source, target, clear_watchlist=True)
    assert result["events"] == 1
    assert result["watchlist_before"] == 1
    assert result["watchlist_after"] == 0

    migrated = Store(target)
    assert migrated.get_event(event["event_id"])["summary"] == "SN55 · 历史事件"
    assert migrated.list_watched_addresses("finney") == []


def test_frontend_uses_address_book_first_and_second_level_selection():
    app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "event-monitor" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "优先匹配地址库名" in app_js
    assert "地址后四位命中" in app_js
    assert "shared-book-checkbox" in app_js
    assert "展开二级勾选" in app_js
    assert "library_book: item.book" in app_js
    assert "按地址库勾选事件监控" in app_js
    assert "address-manual-note-input" in app_js
    assert "manualNoteTags" in app_js
    assert "event-address-copy" in app_js
    assert "event-note-placeholder" in app_js
    assert "openManualNoteEditor" in app_js
    assert "address-${direction}" in app_js
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "event-monitor" / "styles.css").read_text(
        encoding="utf-8"
    )
    assert ".event-row.address-buy" in css
    assert ".event-row.address-sell" in css
    assert ".event-row.address-transfer" in css
    assert ".event-note-placeholder" in css
    assert ".detail-backdrop { backdrop-filter: none; }" in css

    floating_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "event-monitor" / "floating.js").read_text(
        encoding="utf-8"
    )
    assert "data-note-event-id" in floating_js
    assert "openNoteEditor" in floating_js
    assert "/event-api/addresses" in floating_js
    assert "manual_note" in floating_js
    floating_css = (Path(__file__).resolve().parents[1] / "app" / "static" / "event-monitor" / "floating.css").read_text(
        encoding="utf-8"
    )
    assert ".note-dialog" in floating_css
    assert ".note-dialog-input" in floating_css
    assert ".floating-toast" in floating_css


def test_legacy_subnet_profiles_are_inherited_once(tmp_path):
    source = Store(tmp_path / "legacy.db")
    source.upsert_subnet_profile_auto(
        "finney", 55,
        {"name": "NIOME", "website_url": "https://niome.example"},
        "0x" + "a" * 64,
    )
    source.set_trading_state(
        network="finney", netuid=55, enabled=True, registration_block=1,
        block_hash="0x" + "b" * 64, block_number=100,
    )
    source.upsert_chain_repository({
        "network": "finney", "netuid": 55, "owner": "niome", "name": "repo",
        "html_url": "https://github.com/niome/repo",
    })
    target = Store(tmp_path / "event_monitor.db")

    result = inherit_legacy_event_state(target.path, tmp_path / "legacy.db")
    assert result["inherited"] is True
    profiles = target.list_subnet_profiles("finney")
    assert profiles[0]["netuid"] == 55
    assert profiles[0]["name"] == "NIOME"
    assert target.list_repositories()[0]["owner"] == "niome"

    second = inherit_legacy_event_state(target.path, tmp_path / "legacy.db")
    assert second["inherited"] is False
    assert second["tables"]["subnet_profiles"]["skipped"] is True


def test_default_sn1_to_sn128_books_are_seeded_once_without_resurrection(tmp_path):
    store = Store(tmp_path / "event_monitor.db")
    items = [
        {"book": "SN1 Apex", "address": "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt", "label": "SN1"},
        {"book": "SN2 DaniLabs", "address": "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg", "label": "SN2"},
        {"book": "SN129 ignored", "address": "5FURtzHeFURtzHeFURtzHeFURtzHeFURtzHeFURt", "label": "SN129"},
    ]
    first = seed_default_subnet_watch(store, items)
    assert first["added"] == 2
    assert first["book_count"] == 2
    watched = store.list_watched_addresses("finney")
    assert {item["library_book"] for item in watched} == {"SN1 Apex", "SN2 DaniLabs"}

    removed = next(item for item in watched if item["library_book"] == "SN1 Apex")
    assert store.delete_watched_address(removed["address_id"])
    second = seed_default_subnet_watch(store, items)
    assert second["added"] == 0
    assert len(store.list_watched_addresses("finney")) == 1

    third = seed_default_subnet_watch(
        store,
        items + [{
            "book": "SN3 New",
            "address": "5HCFbwK8wHCFbwK8wHCFbwK8wHCFbwK8wHCFbwK8wHCF",
            "label": "SN3",
        }],
    )
    assert third["added"] == 1
    assert "SN3 New" in third["new_books"]
    assert len(store.list_watched_addresses("finney")) == 2


def test_auto_import_rows_sync_only_monitored_books_and_respect_exclusions(merged_client):
    client, database = merged_client
    third_address = "5HCFWvRqzSHWRPecN7q8J6c7aKQnrCZTMHstPv39xL1wgDHh"

    create_book(client, "SN1 Apex", [ADDRESS])
    create_book(client, "SN2 DaniLabs", [third_address])
    monitored = client.post(
        "/event-api/addresses",
        json={
            "network": "finney",
            "addresses": [ADDRESS],
            "label": "SN1 Apex",
            "library_book": "SN1 Apex",
        },
    )
    assert monitored.status_code == 201

    # 后续自动入库把 ADDRESS_TWO 加入已监控的 SN1 地址库。
    client.post(
        "/api/library/books",
        json={
            "name": "SN1 Apex",
            "addresses": [ADDRESS_TWO],
            "include_seed": False,
            "category": "其他",
        },
    )
    result = main_module._sync_auto_import_rows_to_event({
        "address_rows": [
            {"address": ADDRESS_TWO, "book": "SN1 Apex"},
            {"address": third_address, "book": "SN2 DaniLabs"},
        ]
    })
    assert result["added"] == 1
    watched = client.get("/event-api/addresses").json()["addresses"]
    assert {item["address"] for item in watched} == {ADDRESS, ADDRESS_TWO}
    assert {item["library_book"] for item in watched} == {"SN1 Apex"}

    synced = next(item for item in watched if item["address"] == ADDRESS_TWO)
    assert client.delete(f"/event-api/addresses/{synced['address_id']}").status_code == 200
    again = main_module._sync_auto_import_rows_to_event({
        "address_rows": [{"address": ADDRESS_TWO, "book": "SN1 Apex"}]
    })
    assert again["skipped"] == 1
    watched = client.get("/event-api/addresses").json()["addresses"]
    assert {item["address"] for item in watched} == {ADDRESS}

    manual = client.post(
        "/event-api/addresses",
        json={
            "network": "finney",
            "addresses": [ADDRESS_TWO],
            "label": "SN1 Apex",
            "library_book": "SN1 Apex",
        },
    )
    assert manual.status_code == 201
    watched = client.get("/event-api/addresses").json()["addresses"]
    assert {item["address"] for item in watched} == {ADDRESS, ADDRESS_TWO}
