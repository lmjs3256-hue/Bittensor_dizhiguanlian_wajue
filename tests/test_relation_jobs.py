from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.service as service_module
import app.taostats as taostats
from app.db import Database
from app.jobs import RelationJobManager
from app.service import LinkService
from app.taostats import TaostatsAuthError, TransferPage


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
NEIGHBORS = [
    "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN",
    "5FqBL928dMZ8JGvJ5jP5Qz8vJmM5Gk9m2yYQ4mZ8kM4Z7hE2a",
    "5HEUrASy5rJ7Q2XvE1mQ8kQ2sYz5f8pN4rT6wB3cD9gH2jK5",
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


def build_service(directory, name="jobs.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def test_multi_round_job_resumes_queue_without_refetching(monkeypatch):
    calls: list[tuple[str, str, int]] = []

    def fake_transfer(address, page):
        calls.append(("transfer", address, page))
        if address == SEED:
            return TransferPage(
                [transfer_row(i, SEED, neighbor) for i, neighbor in enumerate(NEIGHBORS)],
                None,
            )
        return TransferPage([], None)

    def fake_stake(address, page):
        calls.append(("stake", address, page))
        return TransferPage([], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(service_module, "get_stake_transfer_page", fake_stake)

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "multi.db")
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=20, max_addresses=100)
            job = manager.create(
                service,
                seed=SEED,
                depth=2,
                min_edge_tao="0.1",
                min_total_tao="0.1",
            )
            assert job.status == "waiting"
            assert job.rounds_done == 1
            first_round_calls = list(calls)
            assert len(first_round_calls) == 5
            assert first_round_calls[0] == ("transfer", SEED, 1)

            guard = 0
            while not job.is_finished and guard < 20:
                manager.run_round(job)
                guard += 1

            assert job.status == "complete"
            assert job.rounds_done >= 2
            # 断点续查：每个 (地址, 页) 只请求一次，种子首页不会在后续轮次重复出现。
            transfer_calls = [c for c in calls if c[0] == "transfer"]
            assert len(transfer_calls) == len(set(transfer_calls))
            assert transfer_calls.count(("transfer", SEED, 1)) == 1
            assert job.total_requests == len(calls)

            result = job.result
            related = {row["address"] for row in result["related"]}
            assert related == set(NEIGHBORS)
            assert result["pagination_complete"] is True
            assert result["api_requests"] == job.total_requests
            assert result["job"]["rounds"] == job.rounds_done
        finally:
            database.engine.dispose()


def test_job_stops_immediately_on_auth_failure(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "get_transfer_page",
        lambda *_a: (_ for _ in ()).throw(TaostatsAuthError("密钥未配置")),
    )
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "auth.db")
        try:
            manager = RelationJobManager(round_interval=1)
            job = manager.create(
                service, seed=SEED, depth=3, min_edge_tao="0.1", min_total_tao="0.1"
            )
            assert job.is_finished
            assert job.status == "complete"
            assert job.rounds_done == 1
            assert any("密钥未配置" in warning for warning in job.result["warnings"])
            assert job.result["pagination_complete"] is False
        finally:
            database.engine.dispose()


def test_job_respects_max_rounds_cap(monkeypatch):
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

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "cap.db")
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=1)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            manager.run_round(job)
            assert job.status == "complete"
            assert job.result["job"]["capped"] is True
            assert any("分轮续查上限" in warning for warning in job.result["warnings"])
            assert job.result["pagination_complete"] is False
        finally:
            database.engine.dispose()


def test_cancel_stops_further_rounds(monkeypatch):
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

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "cancel.db")
        try:
            manager = RelationJobManager(round_interval=1)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            rounds_before = job.rounds_done
            manager.cancel(job.id)
            assert job.status == "cancelled"
            manager.run_round(job)
            assert job.rounds_done == rounds_before
            assert manager.due() == []
        finally:
            database.engine.dispose()


def test_job_endpoints_expose_progress_and_result(monkeypatch):
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
    # 分轮查询走 Taostats，接口层会先校验 key 是否存在。
    taostats.configure_api_key("job-endpoint-test-key")

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "api.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        manager = RelationJobManager(round_interval=600)
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", manager)
        try:
            with TestClient(main_module.app) as client:
                created = client.post(
                    "/api/relations/jobs",
                    json={
                        "seed": SEED,
                        "depth": 2,
                        "min_edge_tao": "0.1",
                        "min_total_tao": "0.1",
                        "round_interval_seconds": 600,
                    },
                )
                assert created.status_code == 200
                body = created.json()
                job_id = body["job_id"]
                assert body["status"] == "waiting"
                assert body["requests_total"] == 5
                assert body["pending_stake"] >= 1
                assert body["next_round_in"] > 0

                status = client.get(f"/api/relations/jobs/{job_id}")
                assert status.status_code == 200
                assert status.json()["job_id"] == job_id

                cancelled = client.post(f"/api/relations/jobs/{job_id}/cancel")
                assert cancelled.status_code == 200
                assert cancelled.json()["status"] == "cancelled"

                missing = client.get("/api/relations/jobs/not-a-job")
                assert missing.status_code == 404
        finally:
            database.engine.dispose()


def test_job_stops_as_soon_as_queue_is_empty(monkeypatch):
    """轮数是上限而不是固定次数：队列清空就结束，不会空跑到上限。"""

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

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "early.db")
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=12, max_addresses=100)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            guard = 0
            while not job.is_finished and guard < 12:
                manager.run_round(job)
                guard += 1
            assert job.status == "complete"
            assert job.rounds_done < 12
            assert job.result["job"]["capped"] is False
            assert job.result["pagination_complete"] is True
            assert all(round_requests > 0 for round_requests in job.state.rounds)
        finally:
            database.engine.dispose()


def test_job_payload_reports_plan_and_last_round(monkeypatch):
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

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "plan.db")
        try:
            manager = RelationJobManager(round_interval=600, max_rounds=7)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            payload = manager.payload(job)
            assert payload["max_rounds"] == 7
            assert payload["last_round"]["counts"]["transfer_shallow"] == 5
            assert payload["next_round_plan"]["requests"] > 0
            assert payload["remaining_requests"] > 0
            assert payload["estimated_rounds_left"] >= 1
            assert payload["kind_labels"]["transfer_shallow"]
        finally:
            database.engine.dispose()


def test_cancel_returns_partial_result_with_remaining_counts(monkeypatch):
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

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "stop.db")
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=12)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            assert job.status == "waiting"
            manager.cancel(job.id)
            assert job.status == "cancelled"
            result = job.result
            assert result is not None
            # 已经查到的关联地址仍然返回。
            assert {row["address"] for row in result["related"]} == set(NEIGHBORS)
            assert result["pagination_complete"] is False
            assert result["job"]["stopped"] is True
            assert result["job"]["remaining_requests"] > 0
            assert any("已手动停止续查" in warning for warning in result["warnings"])
            payload = manager.payload(job)
            assert payload["status"] == "cancelled"
            assert payload["remaining_requests"] == result["job"]["remaining_requests"]
            assert payload["next_round_plan"]["requests"] == 0
        finally:
            database.engine.dispose()


def test_rate_limit_round_keeps_request_for_next_round(monkeypatch):
    attempts: list[str] = []
    state = {"limited": True}

    def fake_transfer(address, page):
        attempts.append(address)
        if address == SEED and state["limited"]:
            state["limited"] = False
            raise taostats.TaostatsRateLimitError("被限流", request_made=False)
        if address == SEED:
            return TransferPage(
                [transfer_row(0, SEED, NEIGHBORS[0])], None
            )
        return TransferPage([], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "limit.db")
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=12)
            job = manager.create(
                service, seed=SEED, depth=1, min_edge_tao="0.1", min_total_tao="0.1"
            )
            assert job.rounds_done == 1
            # 限流没有丢掉种子地址：下一轮仍然从它开始。
            assert job.state.pending_transfer >= 1
            manager.run_round(job)
            assert attempts.count(SEED) == 2
            assert job.state.pending_transfer == 0
        finally:
            database.engine.dispose()


def test_repeated_empty_rounds_stop_instead_of_idling(monkeypatch):
    def always_limited(*_a):
        raise taostats.TaostatsRateLimitError("持续限流", request_made=False)

    monkeypatch.setattr(service_module, "get_transfer_page", always_limited)
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "idle.db")
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=12)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            guard = 0
            while not job.is_finished and guard < 12:
                manager.run_round(job)
                guard += 1
            assert job.status == "complete"
            assert job.rounds_done == 3
            assert any("连续 3 轮没有取得任何数据" in warning for warning in job.result["warnings"])
        finally:
            database.engine.dispose()


def test_cancel_stops_mid_round_and_skips_balance_queries(monkeypatch):
    """停止续查应当即时生效：本轮剩余请求不再执行，结果合成不再查余额。"""

    calls: list[tuple[str, str, int]] = []

    def fake_transfer(address, page):
        calls.append(("transfer", address, page))
        if address == SEED:
            return TransferPage(
                [transfer_row(i, SEED, neighbor) for i, neighbor in enumerate(NEIGHBORS)],
                None,
            )
        return TransferPage([], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(
        service_module,
        "get_stake_transfer_page",
        lambda *a: calls.append(("stake", a[0], a[1])) or TransferPage([], None),
    )

    def no_balance_query(*_a, **_k):
        raise AssertionError("停止续查后不应再发起余额查询")

    monkeypatch.setattr(service_module, "query_account_balances", no_balance_query)

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "cancel.db")
        try:
            manager = RelationJobManager(round_interval=60, max_rounds=12)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            assert job.status == "waiting"

            # 模拟"正在跑一轮"时点了停止：should_stop 在第 3 个请求后生效。
            job.cancel_requested = True
            spent = job.crawl.run_round(5, should_stop=lambda: len(calls) >= 8)
            assert spent == 3  # 第一轮已用 5 次 + 本轮 3 次即触发停止

            manager._finish(job, status="cancelled", cap_note="停止")
            assert job.status == "cancelled"
            assert job.result is not None
            assert "related" in job.result
            # 余额被跳过：起始地址余额为"未获取"（None）。
            assert job.result["seed_balance_tao"] is None
        finally:
            database.engine.dispose()


def test_cancel_while_waiting_finishes_immediately(monkeypatch):
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

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "waiting.db")
        try:
            manager = RelationJobManager(round_interval=60, max_rounds=12)
            job = manager.create(
                service, seed=SEED, depth=2, min_edge_tao="0.1", min_total_tao="0.1"
            )
            assert job.status == "waiting"
            stopped = manager.cancel(job.id)
            assert stopped is job
            assert job.status == "cancelled"
            assert job.result is not None
            assert any("已手动停止" in warning for warning in job.result["warnings"])
        finally:
            database.engine.dispose()


def _ingest_chain(database):
    """本地索引造一条 S → N0 → N1 / N0 → N2 的转账链。"""
    records = [
        ("local-1", SEED, NEIGHBORS[0]),
        ("local-2", NEIGHBORS[0], NEIGHBORS[1]),
        ("local-3", NEIGHBORS[0], NEIGHBORS[2]),
    ]
    rows = [
        {
            "event_key": key,
            "tx_hash": key,
            "block_number": 100 + index,
            "block_hash": "0xblock",
            "event_index": index,
            "from_address": source,
            "to_address": target,
            "amount_rao": 1_000_000_000,
            "amount_tao": 1,
        }
        for index, (key, source, target) in enumerate(records)
    ]
    database.ingest_transfers(rows)


def test_relations_removed_addresses_cut_the_whole_clue():
    """移除关联：该地址不进结果，其线索下游在重查时一并消失。"""
    from decimal import Decimal  # noqa: F401

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "removed.db")
        try:
            _ingest_chain(database)
            full = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1",
                data_source="local",
            )
            assert {row["address"] for row in full["related"]} == set(NEIGHBORS[:3])

            cut = service.relations(
                seed=SEED, depth=3, min_edge_tao="1", min_total_tao="1",
                data_source="local",
                removed_addresses=[NEIGHBORS[0]],
            )
            assert cut["related"] == []
            assert any("手动移除" in warning for warning in cut["warnings"])
        finally:
            database.engine.dispose()


def test_relations_endpoint_accepts_removed_addresses():
    """/api/relations 透传 removed_addresses（本地数据源无需 API key）。"""
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "removed-api.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        original = main_module.service
        try:
            _ingest_chain(database)
            main_module.app.dependency_overrides[original] = lambda: service
            with TestClient(main_module.app) as client:
                payload = {
                    "seed": SEED, "depth": 3, "data_source": "local",
                    "min_edge_tao": "1", "min_total_tao": "1",
                    "removed_addresses": [NEIGHBORS[0]],
                }
                response = client.post("/api/relations", json=payload)
            assert response.status_code == 200
            body = response.json()
            assert {row["address"] for row in body["related"]} == set()
        finally:
            main_module.app.dependency_overrides.pop(original, None)
            database.engine.dispose()
