from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.service import LinkService
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
    assert "/static/event-monitor/app.js?v=1.0.0" in page.text

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
    assert client.get("/event-api/product").json()["product"]["version"] == "1.0.0"


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
