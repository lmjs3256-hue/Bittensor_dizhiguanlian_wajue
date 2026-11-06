from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.service as service_module
import app.taostats as taostats
from app.db import AddressLibrary, Database
from app.jobs import RelationJobManager
from app.service import LinkService, select_exportable_addresses
from app.taostats import TransferPage


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
NEIGHBORS = [
    "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN",
    "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z",
]


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    taostats.reset_client_state_for_tests()
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})
    yield
    taostats.reset_client_state_for_tests()


def transfer_row(index, source, target, amount=1_000_000_000):
    return {
        "id": f"edge-{index}",
        "block_number": 100 + index,
        "from": source,
        "to": target,
        "amount": str(amount),
        "transaction_hash": f"0x{index:04d}",
    }


def build_service(directory, name="history.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def fake_pages(monkeypatch):
    def fake_transfer(address, page):
        if address == SEED:
            return TransferPage(
                [transfer_row(i, SEED, neighbor) for i, neighbor in enumerate(NEIGHBORS)],
                None,
            )
        return TransferPage([], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )


def sample_result(seed=SEED, status="complete"):
    return {
        "seed": seed,
        "max_depth": 2,
        "data_source": "taostats",
        "data_cutoff_block": 9430000,
        "related": [
            {
                "address": NEIGHBORS[0],
                "depth": 1,
                "match": True,
                "library_category": "EXCHANGE",
                "library_label": "交易所",
                "exchange_confirmed": True,
                "exchange_rejected": False,
            },
            {
                "address": NEIGHBORS[1],
                "depth": 1,
                "match": True,
                "library_category": "VALIDATOR",
                "library_label": "验证者",
                "exchange_confirmed": False,
                "exchange_rejected": False,
            },
            {
                "address": NEIGHBORS[2],
                "depth": 2,
                "match": False,
                "library_category": "",
                "exchange_confirmed": False,
                "exchange_rejected": False,
            },
        ],
        "library_matches": [{"address": NEIGHBORS[0]}, {"address": NEIGHBORS[1]}],
        "seed_library_matches": [],
        "warnings": [],
        "limitations": [],
        "api_usage": {"today": 5},
        "status": status,
    }


def test_job_result_is_archived_with_exportable_addresses(monkeypatch):
    fake_pages(monkeypatch)

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=5)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            guard = 0
            while not job.is_finished and guard < 10:
                manager.run_round(job)
                guard += 1

            assert job.is_finished
            assert job.archive_id
            assert job.result["archive_id"] == job.archive_id

            history = service.relation_history()
            assert history["total"] == 1
            item = history["items"][0]
            assert item["seed"] == SEED
            assert item["status"] == "complete"
            assert item["kind"] == "job"
            assert item["related_count"] == len(NEIGHBORS)
            assert item["rounds"] == job.rounds_done

            detail = service.relation_history_detail(item["id"])
            assert {row["address"] for row in detail["result"]["related"]} == set(NEIGHBORS)
            assert detail["result"]["job"]["rounds"] == job.rounds_done
            # 用量统计不进入快照，避免历史页显示过期数字。
            assert "api_usage" not in detail["result"]

            content, media_type, filename = service.export_relation_history(
                item["id"], "txt"
            )
            assert media_type.startswith("text/plain")
            assert filename.endswith(".txt")
            assert set(content.split()) == set(NEIGHBORS)

            payload, media_type, _ = service.export_relation_history(item["id"], "json")
            assert media_type.startswith("application/json")
            assert SEED in payload
        finally:
            database.engine.dispose()


def test_cancelled_job_archives_partial_result(monkeypatch):
    fake_pages(monkeypatch)

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "cancel.db")
        try:
            manager = RelationJobManager(round_interval=600)
            job = manager.create(
                service, seed=SEED, depth=3, min_edge_tao="0.1", min_total_tao="0.1"
            )
            manager.cancel(job.id)
            assert job.status == "cancelled"

            item = service.relation_history()["items"][0]
            assert item["status"] == "cancelled"
            assert item["related_count"] == len(NEIGHBORS)
            detail = service.relation_history_detail(item["id"])
            assert detail["result"]["job"]["stopped"] is True
        finally:
            database.engine.dispose()


def test_export_excludes_confirmed_exchange_and_validator():
    selection = select_exportable_addresses(sample_result()["related"])

    assert selection["addresses"] == [NEIGHBORS[2]]
    assert selection["skipped_exchange"] == 1
    assert selection["skipped_validator"] == 1


def test_sync_relation_addresses_dedupes_and_keeps_existing_rows():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "sync.db")
        try:
            service.import_rows(
                [
                    {
                        "address": NEIGHBORS[0],
                        "label": "人工标签",
                        "category": "WATCH",
                        "note": "不要被覆盖",
                    }
                ],
                "manual",
            )

            result = service.sync_relation_addresses(
                seed=SEED,
                addresses=[NEIGHBORS[0], NEIGHBORS[1], NEIGHBORS[1], NEIGHBORS[2], "not-an-address"],
                category="WATCH",
                label="关联同步测试",
            )

            assert result["total"] == 4  # seed + 3 个关联地址（重复项已去重）
            assert result["inserted"] == 3
            assert result["existing"] == 1
            assert result["matched"] == 1
            assert result["invalid"] == 1
            assert result["seed_in_library"] is False

            with database.session() as db:
                kept = db.query(AddressLibrary).filter_by(address=NEIGHBORS[0]).one()
                assert kept.label == "人工标签"
                assert kept.note == "不要被覆盖"
                seed_row = db.query(AddressLibrary).filter_by(address=SEED).one()
                assert seed_row.label == "关联同步测试"
                assert seed_row.source == f"relation-sync:{SEED[:8]}"
        finally:
            database.engine.dispose()


def test_sync_only_library_matched_adds_initiator_address():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "matched.db")
        try:
            service.import_rows([{"address": NEIGHBORS[0], "category": "WATCH"}], "manual")

            result = service.sync_relation_addresses(
                seed=SEED,
                addresses=NEIGHBORS,
                only_library_matched=True,
            )

            assert result["matched"] == 1
            # 命中地址已在库中会被跳过，实际新增的是发起地址。
            assert result["inserted"] == 1
            assert result["existing"] == 1
            assert result["added"][0]["address"] == SEED
        finally:
            database.engine.dispose()


def test_history_endpoints_list_detail_export_and_delete(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "api.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        archive_id = service.archive_relation(
            sample_result(), job_id="abc123", kind="job", status="complete", rounds=3, requests=15
        )
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                listing = client.get("/api/relations/history?limit=10")
                assert listing.status_code == 200
                body = listing.json()
                assert body["total"] == 1
                assert body["items"][0]["id"] == archive_id

                detail = client.get(f"/api/relations/history/{archive_id}")
                assert detail.status_code == 200
                assert detail.json()["item"]["requests"] == 15

                exported = client.get(f"/api/relations/history/{archive_id}/export?format=txt")
                assert exported.status_code == 200
                assert NEIGHBORS[2] in exported.text
                assert NEIGHBORS[0] not in exported.text
                assert "attachment" in exported.headers["content-disposition"]

                synced = client.post(
                    "/api/relations/sync-library",
                    json={"seed": SEED, "addresses": NEIGHBORS},
                )
                assert synced.status_code == 200
                assert synced.json()["inserted"] == 4

                missing = client.get("/api/relations/history/999999")
                assert missing.status_code == 404

                deleted = client.delete(f"/api/relations/history/{archive_id}")
                assert deleted.status_code == 200
                assert deleted.json()["deleted"] == 1
                assert client.get("/api/relations/history").json()["total"] == 0
        finally:
            database.engine.dispose()


def test_history_clear_endpoint(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "clear.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        for _ in range(3):
            service.archive_relation(sample_result(), kind="single")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                assert client.get("/api/relations/history").json()["total"] == 3
                cleared = client.post("/api/relations/history/clear")
                assert cleared.status_code == 200
                assert cleared.json()["deleted"] == 3
                assert client.get("/api/relations/history").json()["total"] == 0
        finally:
            database.engine.dispose()


def test_archives_are_trimmed_to_retention_limit(monkeypatch):
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "trim.db")
        try:
            monkeypatch.setattr(service_module, "MAX_ARCHIVE_ROWS", 2)
            ids = [service.archive_relation(sample_result(), kind="single") for _ in range(3)]
            remaining = [item["id"] for item in service.relation_history()["items"]]
            assert remaining == [ids[2], ids[1]]
        finally:
            database.engine.dispose()


def test_select_exportable_addresses_filters_by_depth():
    rows = [
        {"address": "addr-1", "depth": 1},
        {"address": "addr-2", "depth": 2},
        {"address": "addr-3", "depth": 2, "library_category": "VALIDATOR"},
        {"address": "addr-4", "depth": 3},
    ]

    all_rows = select_exportable_addresses(rows)
    assert all_rows["addresses"] == ["addr-1", "addr-2", "addr-4"]
    assert all_rows["skipped_other_depth"] == 0

    depth1 = select_exportable_addresses(rows, depth=1)
    assert depth1["addresses"] == ["addr-1"]
    assert depth1["skipped_other_depth"] == 3

    depth2 = select_exportable_addresses(rows, depth=2)
    # 层级过滤和交易所/验证者剔除同时生效。
    assert depth2["addresses"] == ["addr-2"]
    assert depth2["skipped_validator"] == 1
    assert depth2["skipped_other_depth"] == 2

    depth3 = select_exportable_addresses(rows, depth=3)
    assert depth3["addresses"] == ["addr-4"]


def test_history_export_supports_depth_filter(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "depth.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        result = sample_result()
        result["related"] = [
            {**result["related"][0], "depth": 1},  # 已确认交易所：任何层级都不导出
            {**result["related"][1], "depth": 1},  # 验证者：任何层级都不导出
            {**result["related"][2], "depth": 2},
        ]
        archive_id = service.archive_relation(
            result, kind="single", status="complete"
        )
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                full = client.get(
                    f"/api/relations/history/{archive_id}/export?format=txt"
                )
                assert full.status_code == 200
                assert NEIGHBORS[2] in full.text
                assert "-d" not in full.headers["content-disposition"].split("filename=")[1]

                depth2 = client.get(
                    f"/api/relations/history/{archive_id}/export?format=txt&depth=2"
                )
                assert depth2.status_code == 200
                assert NEIGHBORS[2] in depth2.text
                assert "d2" in depth2.headers["content-disposition"]

                # 1 级只剩被剔除的交易所/验证者：导出为空但不报错。
                depth1 = client.get(
                    f"/api/relations/history/{archive_id}/export?format=txt&depth=1"
                )
                assert depth1.status_code == 200
                assert NEIGHBORS[2] not in depth1.text

                depth_json = client.get(
                    f"/api/relations/history/{archive_id}/export?format=json&depth=2"
                )
                assert depth_json.status_code == 200
                payload = depth_json.json()
                assert [item["address"] for item in payload["related"]] == [NEIGHBORS[2]]

                # 层级上限是 10：11 级越界必须拒绝（9 级现在是合法值）。
                out_of_range = client.get(
                    f"/api/relations/history/{archive_id}/export?format=txt&depth=11"
                )
                assert out_of_range.status_code == 422
        finally:
            database.engine.dispose()
