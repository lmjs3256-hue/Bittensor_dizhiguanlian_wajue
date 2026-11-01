from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from fastapi.testclient import TestClient

import app.service as service_module
import app.taostats as taostats
import app.main as main_module
from app.db import Database
from app.service import LinkService
from app.taostats import (
    TaostatsAuthError,
    TaostatsRateLimitError,
    TransferPage,
    get_exchange_addresses,
    get_transfer_page,
)


ADDRESSES = [
    "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt",
    "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN",
]


@pytest.fixture(autouse=True)
def reset_taostats_state():
    taostats.reset_client_state_for_tests()
    yield
    taostats.reset_client_state_for_tests()


def api_response(payload, status=200):
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://api.taostats.io/v1/transfers"),
    )


def test_transfer_client_sends_key_and_parses_paginated_response(monkeypatch):
    monkeypatch.setenv("TAOSTATS_API_KEY", "test-secret")
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return api_response(
            {
                "data": [{"id": "1", "amount": "1230000000"}],
                "pagination": {
                    "next_page": 2,
                    "per_page": 200,
                    "total_pages": 5000,
                    "total_items": 1000001,
                },
            }
        )

    monkeypatch.setattr(taostats.httpx, "get", fake_get)

    page = get_transfer_page(ADDRESSES[0])
    cached = get_transfer_page(ADDRESSES[0])

    assert page.rows[0]["amount"] == "1230000000"
    assert page.next_page == 2
    assert page.truncated_by_window is True
    assert page.from_cache is False
    assert cached.from_cache is True
    assert len(calls) == 1
    assert calls[0][1]["params"] == {
        "network": "finney",
        "address": ADDRESSES[0],
        "page": 1,
        "limit": 200,
    }
    assert calls[0][1]["headers"]["Authorization"] == "test-secret"


def test_transfer_client_requires_key_and_obeys_local_rate_guard(monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    with pytest.raises(TaostatsAuthError, match="未配置"):
        get_transfer_page(ADDRESSES[0])

    monkeypatch.setenv("TAOSTATS_API_KEY", "test-secret")
    calls = 0

    def fake_get(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return api_response(
            {
                "data": [],
                "pagination": {
                    "next_page": None,
                    "per_page": 200,
                    "total_pages": 0,
                    "total_items": 0,
                },
            }
        )

    monkeypatch.setattr(taostats.httpx, "get", fake_get)
    for index in range(taostats.LOCAL_REQUESTS_PER_MINUTE):
        get_transfer_page(f"address-{index}")

    with pytest.raises(TaostatsRateLimitError, match="5 次/分钟"):
        get_transfer_page("sixth-address")
    assert calls == taostats.LOCAL_REQUESTS_PER_MINUTE


def test_transfer_client_reports_auth_and_provider_rate_errors(monkeypatch):
    monkeypatch.setenv("TAOSTATS_API_KEY", "test-secret")
    monkeypatch.setattr(
        taostats.httpx, "get", lambda *_args, **_kwargs: api_response({}, 401)
    )
    with pytest.raises(TaostatsAuthError, match="无效或没有访问权限"):
        get_transfer_page(ADDRESSES[0])

    taostats.reset_client_state_for_tests()
    monkeypatch.setattr(
        taostats.httpx, "get", lambda *_args, **_kwargs: api_response({}, 429)
    )
    with pytest.raises(TaostatsRateLimitError, match="返回限流"):
        get_transfer_page(ADDRESSES[1])


def test_exchange_directory_parses_coldkeys_and_uses_cache(monkeypatch):
    monkeypatch.setenv("TAOSTATS_API_KEY", "test-secret")
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return api_response(
            {
                "data": [
                    {"coldkey": ADDRESSES[0], "name": "Example Exchange", "icon": "icon.png"},
                    {"coldkey": ADDRESSES[0], "name": "duplicate"},
                    {"coldkey": ADDRESSES[1], "name": "Another Exchange"},
                ]
            }
        )

    monkeypatch.setattr(taostats.httpx, "get", fake_get)

    rows, from_cache = get_exchange_addresses()
    cached, cached_from_cache = get_exchange_addresses()

    assert [row["address"] for row in rows] == ADDRESSES[:2]
    assert rows[0]["name"] == "Example Exchange"
    assert from_cache is False
    assert cached == rows
    assert cached_from_cache is True
    assert len(calls) == 1
    assert calls[0][0].endswith("/v1/exchange-addresses")
    assert calls[0][1]["headers"]["Authorization"] == "test-secret"


def test_service_syncs_exchange_directory_without_duplicates(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "get_exchange_addresses",
        lambda: ([{"address": ADDRESSES[0], "name": "Example Exchange"}], False),
    )
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "exchanges.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            first = service.sync_exchange_addresses()
            second = service.sync_exchange_addresses()
            rows = service.library()
            assert first["inserted"] == 1
            assert second["skipped"] == 1
            assert len(rows) == 1
            assert rows[0]["category"] == "EXCHANGE"
            assert rows[0]["label"] == "Example Exchange"
            assert rows[0]["source"] == "taostats:/v1/exchange-addresses"
        finally:
            database.engine.dispose()


def test_exchange_filter_stops_remote_expansion(monkeypatch):
    seed, exchange, beyond = ADDRESSES
    calls = []

    def fake_get(address, page):
        calls.append((address, page))
        if address == seed:
            return TransferPage(
                [{
                    "id": "exchange-edge",
                    "block_number": 10,
                    "from": seed,
                    "to": exchange,
                    "amount": "1000000000",
                    "transaction_hash": "0xexchange",
                }],
                None,
            )
        return TransferPage(
            [{
                "id": "beyond-edge",
                "block_number": 11,
                "from": exchange,
                "to": beyond,
                "amount": "2000000000",
                "transaction_hash": "0xbeyond",
            }],
            None,
        )

    monkeypatch.setattr(service_module, "get_transfer_page", fake_get)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "exchange-filter.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows([{"address": exchange, "category": "EXCHANGE"}], "manual")
            result = service.relations(
                seed=seed,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
                filter_exchanges=True,
            )
            assert calls == [(seed, 1)]
            assert result["related"] == []
            assert result["exchange_addresses_filtered"] == 1
        finally:
            database.engine.dispose()


def test_confirmed_exchange_is_terminal_without_filtering(monkeypatch):
    seed, exchange, beyond = ADDRESSES
    calls = []

    def fake_get(address, page):
        calls.append((address, page))
        if address == seed:
            return TransferPage(
                [{
                    "id": "exchange-edge-terminal",
                    "block_number": 10,
                    "from": seed,
                    "to": exchange,
                    "amount": "1000000000",
                    "transaction_hash": "0xexchange-terminal",
                }],
                None,
            )
        return TransferPage(
            [{
                "id": "beyond-edge-terminal",
                "block_number": 11,
                "from": exchange,
                "to": beyond,
                "amount": "2000000000",
                "transaction_hash": "0xbeyond-terminal",
            }],
            None,
        )

    monkeypatch.setattr(service_module, "get_transfer_page", fake_get)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "exchange-terminal.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows([{"address": exchange, "category": "EXCHANGE"}], "manual")
            result = service.relations(
                seed=seed,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
                filter_exchanges=False,
            )
            assert calls == [(seed, 1)]
            assert [row["address"] for row in result["related"]] == [exchange]
            assert result["related"][0]["address_role"] == "交易所地址"
        finally:
            database.engine.dispose()


def test_manual_validator_filter_stops_remote_expansion(monkeypatch):
    seed, validator, beyond = ADDRESSES
    calls = []

    monkeypatch.setattr(
        service_module,
        "get_active_validator_addresses",
        lambda: (set(), False),
    )

    def fake_get(address, page):
        calls.append((address, page))
        if address == seed:
            return TransferPage(
                [{
                    "id": "validator-edge",
                    "block_number": 10,
                    "from": seed,
                    "to": validator,
                    "amount": "1000000000",
                    "transaction_hash": "0xvalidator",
                }],
                None,
            )
        return TransferPage(
            [{
                "id": "beyond-validator-edge",
                "block_number": 11,
                "from": validator,
                "to": beyond,
                "amount": "2000000000",
                "transaction_hash": "0xbeyond-validator",
            }],
            None,
        )

    monkeypatch.setattr(service_module, "get_transfer_page", fake_get)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "validator-filter.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows([{"address": validator, "category": "VALIDATOR"}], "manual")
            result = service.relations(
                seed=seed,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
                filter_validators=True,
            )
            assert calls == [(seed, 1)]
            assert result["related"] == []
            assert result["validator_addresses_filtered"] == 1
        finally:
            database.engine.dispose()


def test_note_api_updates_library_and_creates_relation_note(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "note-api.db")
        try:
            database.initialize()
            monkeypatch.setattr(main_module, "DATABASE", database)
            monkeypatch.setattr(
                main_module, "SERVICE", LinkService(database, "wss://example.invalid")
            )
            with TestClient(main_module.app) as client:
                created = client.post(
                    "/api/library",
                    json={"address": ADDRESSES[0], "label": "DSV", "category": "WATCH"},
                )
                row_id = client.get("/api/library").json()["items"][0]["id"]
                updated = client.patch(
                    f"/api/library/{row_id}",
                    json={"note": "已复核"},
                )
                relation_note = client.post(
                    "/api/notes",
                    json={"address": ADDRESSES[1], "note": "关联备注"},
                )
                assert created.status_code == 200
                assert updated.status_code == 200
                assert updated.json()["item"]["note"] == "已复核"
                assert relation_note.status_code == 200
                assert relation_note.json()["created"] is True
        finally:
            database.engine.dispose()


def test_taostats_relation_traverses_three_hops_and_matches_library(monkeypatch):
    seed, hop1, hop2 = ADDRESSES
    hop3 = "5FourthHopAddress"
    rows = {
        seed: [
            {
                "id": "1",
                "block_number": 101,
                "from": seed,
                "to": hop1,
                "amount": "1000000000",
                "transaction_hash": "0x01",
            }
        ],
        hop1: [
            {
                "id": "2",
                "block_number": 102,
                "from": hop1,
                "to": hop2,
                "amount": "2000000000",
                "transaction_hash": "0x02",
            }
        ],
        hop2: [
            {
                "id": "3",
                "block_number": 103,
                "from": hop2,
                "to": hop3,
                "amount": "3000000000",
                "transaction_hash": "0x03",
            }
        ],
    }
    calls = []

    def fake_get(address, page):
        calls.append((address, page))
        return TransferPage(rows[address], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_get)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "relations.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            service.import_rows(
                [{"address": hop2, "label": "Known", "category": "WATCH"}],
                "test.csv",
            )

            result = service.relations(
                seed=seed,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
            )

            assert [row["address"] for row in result["related"]] == [hop1, hop2, hop3]
            assert [row["depth"] for row in result["related"]] == [1, 2, 3]
            assert result["library_matches"][0]["address"] == hop2
            assert result["library_matches"][0]["library_label"] == "Known"
            assert result["taostats_transfer_count"] == 3
            assert result["api_requests"] == 3
            assert result["pagination_complete"] is True
            assert result["related"][1]["total_tao"] == "5"
            assert calls == [(seed, 1), (hop1, 1), (hop2, 1)]
        finally:
            database.engine.dispose()


def test_taostats_relation_reads_next_page_and_flags_budget_exhaustion(monkeypatch):
    seed = ADDRESSES[0]
    page2_address = ADDRESSES[1]
    calls = []

    def fake_get(address, page):
        calls.append((address, page))
        if page == 1:
            return TransferPage(
                [
                    {
                        "id": "1",
                        "block_number": 10,
                        "from": seed,
                        "to": page2_address,
                        "amount": "1000000000",
                        "transaction_hash": "0x01",
                    }
                ],
                2,
            )
        return TransferPage(
            [
                {
                    "id": "2",
                    "block_number": 9,
                    "from": seed,
                    "to": "older-address",
                    "amount": "2000000000",
                    "transaction_hash": "0x02",
                }
            ],
            None,
        )

    monkeypatch.setattr(service_module, "get_transfer_page", fake_get)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "pages.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = service.relations(
                seed=seed,
                depth=1,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
            )
            assert [row["address"] for row in result["related"]] == ["older-address", page2_address]
            assert result["pagination_complete"] is True
            assert calls == [(seed, 1), (seed, 2)]
        finally:
            database.engine.dispose()

    monkeypatch.setattr(service_module, "MAX_TAOSTATS_REQUESTS_PER_QUERY", 1)

    def chain_get(address, _page):
        if address == seed:
            return TransferPage(
                [
                    {
                        "id": "a",
                        "block_number": 11,
                        "from": seed,
                        "to": page2_address,
                        "amount": "1000000000",
                        "transaction_hash": "0x0a",
                    }
                ],
                None,
            )
        return TransferPage([], None)

    monkeypatch.setattr(service_module, "get_transfer_page", chain_get)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "budget.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = service.relations(
                seed=seed,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
            )
            assert result["pagination_complete"] is False
            assert result["api_requests"] == 1
            assert any("单次查询最多调用" in warning for warning in result["warnings"])
        finally:
            database.engine.dispose()


def test_auth_failure_does_not_claim_request_budget_exhausted(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "get_transfer_page",
        lambda *_args: (_ for _ in ()).throw(TaostatsAuthError("密钥未配置")),
    )
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "auth.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = service.relations(
                seed=ADDRESSES[0],
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
            )
            assert result["pagination_complete"] is False
            assert result["api_requests"] == 0
            assert any("密钥未配置" in warning for warning in result["warnings"])
            assert not any("单次查询最多调用" in warning for warning in result["warnings"])
        finally:
            database.engine.dispose()


def test_api_selects_data_source_and_preserves_local_default(monkeypatch):
    seed, neighbor = ADDRESSES[:2]
    monkeypatch.setattr(
        service_module,
        "get_transfer_page",
        lambda *_args: TransferPage(
            [
                {
                    "id": "api-edge",
                    "block_number": 55,
                    "from": seed,
                    "to": neighbor,
                    "amount": "1000000000",
                    "transaction_hash": "0xapi",
                }
            ],
            None,
        ),
    )
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "api.db")
        try:
            database.initialize()
            monkeypatch.setattr(main_module, "DATABASE", database)
            monkeypatch.setattr(
                main_module, "SERVICE", LinkService(database, "wss://example.invalid")
            )
            with TestClient(main_module.app) as client:
                overview = client.get("/api/overview").json()
                assert isinstance(overview["taostats_configured"], bool)

                default_result = client.post(
                    "/api/relations",
                    json={"seed": seed, "depth": 1},
                )
                assert default_result.status_code == 200
                assert default_result.json()["data_source"] == "local"

                remote_result = client.post(
                    "/api/relations",
                    json={"seed": seed, "depth": 1, "data_source": "taostats"},
                )
                assert remote_result.status_code == 200
                assert remote_result.json()["related"][0]["address"] == neighbor

                invalid_source = client.post(
                    "/api/relations",
                    json={"seed": seed, "data_source": "unknown"},
                )
                assert invalid_source.status_code == 422
        finally:
            database.engine.dispose()


def test_taostats_key_settings_are_memory_only_and_never_echoed(monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    calls = []

    def fake_get(_url, **kwargs):
        calls.append(kwargs)
        return api_response(
            {
                "data": [],
                "pagination": {
                    "next_page": None,
                    "per_page": 200,
                    "total_pages": 0,
                    "total_items": 0,
                },
            }
        )

    monkeypatch.setattr(taostats.httpx, "get", fake_get)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "key-settings.db")
        try:
            database.initialize()
            monkeypatch.setattr(main_module, "DATABASE", database)
            monkeypatch.setattr(
                main_module, "SERVICE", LinkService(database, "wss://example.invalid")
            )
            with TestClient(main_module.app) as client:
                key = "private-test-key"
                saved = client.post("/api/settings/taostats", json={"api_key": key})
                assert saved.status_code == 200
                assert saved.json() == {"configured": True}
                assert key not in saved.text
                assert client.get("/api/overview").json()["taostats_configured"] is True

                get_transfer_page(ADDRESSES[0])
                assert calls[0]["headers"]["Authorization"] == key

                too_long = "x" * 1025
                rejected = client.post(
                    "/api/settings/taostats", json={"api_key": too_long}
                )
                assert rejected.status_code == 422
                assert too_long not in rejected.text

                cleared = client.delete("/api/settings/taostats")
                assert cleared.status_code == 200
                assert cleared.json() == {"configured": False}
                assert client.get("/api/overview").json()["taostats_configured"] is False
                with pytest.raises(TaostatsAuthError, match="未配置"):
                    get_transfer_page(ADDRESSES[1])
        finally:
            database.engine.dispose()
