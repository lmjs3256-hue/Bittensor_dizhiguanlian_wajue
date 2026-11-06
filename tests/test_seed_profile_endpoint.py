# -*- coding: utf-8 -*-
"""扫描一开始先查起始地址画像：接口、预算扣减、前端时序。

背景：用户要求「扫描开始优先查询起始地址信息，查到后直接先显示左侧栏，
关联地址继续扫描，直到结束再汇总结果」。实现拆成两步：
``POST /api/seed-profile`` 先返回画像（固定 2 次请求），前端立刻渲染左栏；
随后 ``/api/relations`` 或 ``/api/relations/jobs`` 带上 ``prefetched_requests``
继续扫描，并从本轮 5 次额度里扣掉，避免同一分钟超限。
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.service as service_module
from app import taostats
from app.db import Database
from app.jobs import RelationJobManager
from app.service import LinkService
from app.taostats import AccountProfile, TransferPage

SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
NEIGHBOR = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    taostats.reset_client_state_for_tests()
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})
    yield
    taostats.reset_client_state_for_tests()


def _fake_profile(monkeypatch, *, from_cache=False, count_cached=False):
    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: AccountProfile(
            created_on_date="2025-09-17",
            created_on_network="finney",
            balance_free_tao="12.46",
            balance_staked_tao="25.02",
            balance_total_tao="37.48",
            from_cache=from_cache,
        ),
    )
    monkeypatch.setattr(
        service_module,
        "get_stake_event_total",
        lambda address: (9466, count_cached),
    )


def _transfer_row(index, source, target):
    return {
        "id": f"edge-{index}",
        "block_number": 100 + index,
        "from": source,
        "to": target,
        "amount": "1000000000",
        "transaction_hash": f"0x{index:04d}",
    }


def _build(directory, name="seed.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def test_seed_profile_endpoint_returns_four_metrics(monkeypatch):
    _fake_profile(monkeypatch)
    with TemporaryDirectory() as directory:
        database, service = _build(directory)
        original = main_module.service
        try:
            main_module.app.dependency_overrides[original] = lambda: service
            with TestClient(main_module.app) as client:
                response = client.post("/api/seed-profile", json={"seed": SEED})
            assert response.status_code == 200
            body = response.json()
            profile = body["seed_profile"]
            assert body["seed"] == SEED
            assert profile["registered_on"] == "2025-09-17"
            assert profile["stake_event_count"] == 9466
            assert profile["balance_total_tao"] == "37.48"
            # 日均频次据此算出来，不是 0 也不是空。
            assert profile["daily_frequency"] > 0
            # 两次都是真实请求，前端要按这个数扣预算。
            assert body["api_requests"] == 2
        finally:
            main_module.app.dependency_overrides.pop(original, None)
            database.engine.dispose()


def test_seed_profile_endpoint_reports_zero_when_cached(monkeypatch):
    _fake_profile(monkeypatch, from_cache=True, count_cached=True)
    with TemporaryDirectory() as directory:
        database, service = _build(directory, "cached.db")
        original = main_module.service
        try:
            main_module.app.dependency_overrides[original] = lambda: service
            with TestClient(main_module.app) as client:
                body = client.post("/api/seed-profile", json={"seed": SEED}).json()
            assert body["api_requests"] == 0
            assert body["seed_profile"]["stake_event_count"] == 9466
        finally:
            main_module.app.dependency_overrides.pop(original, None)
            database.engine.dispose()


def test_seed_profile_endpoint_rejects_bad_address(monkeypatch):
    _fake_profile(monkeypatch)
    with TemporaryDirectory() as directory:
        database, service = _build(directory, "bad.db")
        original = main_module.service
        try:
            main_module.app.dependency_overrides[original] = lambda: service
            with TestClient(main_module.app) as client:
                response = client.post("/api/seed-profile", json={"seed": "not-an-address"})
            assert response.status_code == 422
        finally:
            main_module.app.dependency_overrides.pop(original, None)
            database.engine.dispose()


def test_seed_profile_works_without_api_key(monkeypatch):
    """没配 key 时不报错、不抛 500：四项显示「未获取」，扫描照常进行。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: False)
    with TemporaryDirectory() as directory:
        database, service = _build(directory, "nokey.db")
        original = main_module.service
        try:
            main_module.app.dependency_overrides[original] = lambda: service
            with TestClient(main_module.app) as client:
                response = client.post("/api/seed-profile", json={"seed": SEED})
            assert response.status_code == 200
            body = response.json()
            assert body["seed_profile"]["registered_on"] is None
            assert body["seed_profile"]["transfer_count"] is None
            assert body["api_requests"] == 0
        finally:
            main_module.app.dependency_overrides.pop(original, None)
            database.engine.dispose()


def test_prefetched_requests_are_deducted_from_crawl_budget(monkeypatch):
    """画像先花掉 2 次，本轮爬取就只剩 3 次，不能合计 7 次撞限速。"""

    calls: list[tuple[str, int]] = []

    def fake_transfer(address, page):
        calls.append((address, page))
        return TransferPage([_transfer_row(0, SEED, NEIGHBOR)], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )
    _fake_profile(monkeypatch)

    with TemporaryDirectory() as directory:
        database, service = _build(directory, "budget.db")
        try:
            result = service.relations(
                seed=SEED,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
                prefetched_requests=2,
            )
            # 5 次额度减掉画像的 2 次 → 爬取最多发 3 次请求。
            # 一级转账 + 一级子网（起始地址与邻居各一次）刚好 3 次就到底。
            assert result["api_requests"] == 3
            assert result["prefetched_requests"] == 2
            # 爬取预算被扣到 3，不会出现 5 次。
            assert len(calls) <= 3
        finally:
            database.engine.dispose()


def test_seed_profile_requests_are_reused_not_refetched(monkeypatch):
    """画像查过一次后，合成结果时命中缓存，不会再花额度。"""

    calls: list[str] = []

    def fake_account(address):
        calls.append(address)
        return AccountProfile(created_on_date="2025-09-17")

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(service_module, "get_account_profile", fake_account)
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (10, False))
    monkeypatch.setattr(
        service_module, "get_transfer_page", lambda *_a: TransferPage([], None)
    )
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )

    with TemporaryDirectory() as directory:
        database, service = _build(directory, "reuse.db")
        try:
            snapshot = service.seed_profile_snapshot(seed=SEED)
            assert snapshot["api_requests"] == 2
            # 第二次（结果合成阶段）应当命中 taostats 客户端的缓存。
            monkeypatch.setattr(
                service_module, "get_account_profile", lambda address: AccountProfile(
                    created_on_date="2025-09-17", from_cache=True
                )
            )
            monkeypatch.setattr(
                service_module, "get_stake_event_total", lambda address: (10, True)
            )
            result = service.relations(
                seed=SEED,
                depth=1,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
                prefetched_requests=snapshot["api_requests"],
            )
            assert result["seed_profile"]["registered_on"] == "2025-09-17"
            # 画像没再花请求（命中缓存），只有爬取本身的 2 次。
            assert result["seed_profile_requests"] == 0
            assert result["api_requests"] == 2
            assert result["api_requests_total"] == 2
            assert calls == [SEED]
        finally:
            database.engine.dispose()


def test_job_first_round_deducts_prefetched_requests(monkeypatch):
    """分轮作业的第一轮同样要扣掉画像的 2 次，后续轮次不扣。"""

    rounds: list[int] = []

    def fake_transfer(address, page):
        return TransferPage([_transfer_row(0, SEED, NEIGHBOR)], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )
    _fake_profile(monkeypatch)

    with TemporaryDirectory() as directory:
        database, service = _build(directory, "jobbudget.db")
        try:
            manager = RelationJobManager(round_interval=1, max_rounds=3, max_addresses=50)
            job = manager.create(
                service,
                seed=SEED,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                prefetched_requests=2,
            )
            rounds.append(job.rounds_done)
            # 第一轮只有 3 次请求额度，不是 5 次。
            assert manager.payload(job)["prefetched_requests"] == 2
            assert job.state.rounds[0] == 3
        finally:
            database.engine.dispose()


def test_job_payload_exposes_prefetched_requests(monkeypatch):
    monkeypatch.setattr(
        service_module, "get_transfer_page", lambda *_a: TransferPage([], None)
    )
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None)
    )
    _fake_profile(monkeypatch)

    with TemporaryDirectory() as directory:
        database, service = _build(directory, "jobpayload.db")
        try:
            manager = RelationJobManager(round_interval=600)
            job = manager.create(
                service,
                seed=SEED,
                depth=1,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                prefetched_requests=2,
            )
            payload = manager.payload(job)
            assert payload["prefetched_requests"] == 2
        finally:
            database.engine.dispose()
