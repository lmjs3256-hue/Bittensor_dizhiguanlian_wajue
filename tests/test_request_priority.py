from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app.service as service_module
import app.taostats as taostats
from app.db import Database
from app.service import LinkService
from app.taostats import TransferPage


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
RELATED = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"


@pytest.fixture(autouse=True)
def reset_taostats_state():
    taostats.reset_client_state_for_tests()
    yield
    taostats.reset_client_state_for_tests()


def transfer_row(index: int, source: str, target: str, amount: int = 1_000_000_000) -> dict:
    return {
        "id": f"edge-{index}",
        "block_number": 100 + index,
        "from": source,
        "to": target,
        "amount": str(amount),
        "transaction_hash": f"0x{index:04d}",
    }


def run_relations(monkeypatch, *, depth: int, service: LinkService, **kwargs):
    return service.relations(
        seed=SEED,
        depth=depth,
        min_edge_tao="0.1",
        min_total_tao="0.1",
        data_source="taostats",
        **kwargs,
    )


def test_budget_spends_on_level_one_transfers_before_subnet_stake(monkeypatch):
    neighbors = [f"neighbor-{index}" for index in range(6)]
    calls: list[tuple[str, str, int]] = []

    def fake_transfer(address, page):
        calls.append(("transfer", address, page))
        if address == SEED:
            return TransferPage(
                [transfer_row(index, SEED, neighbor) for index, neighbor in enumerate(neighbors)],
                None,
            )
        return TransferPage([], None)

    def fake_stake(address, page):
        calls.append(("stake", address, page))
        return TransferPage([], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(service_module, "get_stake_transfer_page", fake_stake)
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_args, **_kwargs: {})

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "priority.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = run_relations(monkeypatch, depth=2, service=service)

            assert result["api_requests"] == service_module.MAX_TAOSTATS_REQUESTS_PER_QUERY
            assert calls[0] == ("transfer", SEED, 1)
            # 额度全部用于转账明细：一级邻居的首页先于种子地址的子网 stake 查询。
            assert [kind for kind, _, _ in calls] == ["transfer"] * 5
            assert all(page == 1 for _, _, page in calls[1:])
            assert len({address for _, address, _ in calls[1:]}) == 4
            assert any(
                "未执行" in warning and "子网 stake 转移请求" in warning
                for warning in result["warnings"]
            )
        finally:
            database.engine.dispose()


def test_level_one_stake_runs_before_deeper_transfers(monkeypatch):
    hop1 = "hop-1"
    hop2 = "hop-2"
    calls: list[tuple[str, str, int]] = []

    def fake_transfer(address, page):
        calls.append(("transfer", address, page))
        if address == SEED:
            return TransferPage([transfer_row(1, SEED, hop1)], None)
        if address == hop1:
            return TransferPage([transfer_row(2, hop1, hop2)], None)
        return TransferPage([], None)

    def fake_stake(address, page):
        calls.append(("stake", address, page))
        return TransferPage([], None)

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(service_module, "get_stake_transfer_page", fake_stake)
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_args, **_kwargs: {})

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "priority-depth.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = run_relations(monkeypatch, depth=3, service=service)

            assert [kind for kind, _, _ in calls] == [
                "transfer",
                "transfer",
                "stake",
                "stake",
                "transfer",
            ]
            assert calls[0][1] == SEED
            assert calls[1][1] == hop1
            assert calls[4][1] == hop2
            assert result["api_requests"] == 5
        finally:
            database.engine.dispose()


def test_balance_lookup_is_skipped_when_request_budget_is_used_up(monkeypatch):
    neighbors = [f"neighbor-{index}" for index in range(6)]
    balance_calls: list[list[str]] = []

    def fake_transfer(address, page):
        if address == SEED:
            return TransferPage(
                [transfer_row(index, SEED, neighbor) for index, neighbor in enumerate(neighbors)],
                None,
            )
        return TransferPage([], None)

    def fake_balance(_url, addresses):
        balance_calls.append(list(addresses))
        return {}

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(service_module, "get_stake_transfer_page", lambda *_args: TransferPage([], None))
    monkeypatch.setattr(service_module, "query_account_balances", fake_balance)

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "balance-skip.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = run_relations(monkeypatch, depth=2, service=service)

            assert result["api_requests"] == service_module.MAX_TAOSTATS_REQUESTS_PER_QUERY
            # 额度用尽后只保留起始地址余额，不再扩展到关联地址。
            assert balance_calls == [[SEED]]
            assert any("已跳过关联地址余额查询" in warning for warning in result["warnings"])
            assert result["exchange_balance_checks"] == 0
        finally:
            database.engine.dispose()


def test_balance_lookup_covers_related_addresses_when_budget_remains(monkeypatch):
    balance_calls: list[list[str]] = []

    def fake_transfer(address, page):
        if address == SEED:
            return TransferPage([transfer_row(1, SEED, RELATED, amount=500_000_000)], None)
        return TransferPage([], None)

    def fake_balance(_url, addresses):
        balance_calls.append(list(addresses))
        return {}

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(service_module, "get_stake_transfer_page", lambda *_args: TransferPage([], None))
    monkeypatch.setattr(service_module, "query_account_balances", fake_balance)

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "balance-keep.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = run_relations(monkeypatch, depth=2, service=service)

            assert result["api_requests"] < service_module.MAX_TAOSTATS_REQUESTS_PER_QUERY
            assert balance_calls and balance_calls[-1] == [SEED, RELATED]
            assert not any("已跳过关联地址余额查询" in warning for warning in result["warnings"])
        finally:
            database.engine.dispose()


def test_finalize_caps_balance_addresses_when_many_related(monkeypatch):
    """跑完多轮后关联地址很多时，余额 RPC 只查前 N 个，避免结果迟迟不返回。"""
    neighbors = [f"neighbor-{index}" for index in range(60)]
    balance_calls: list[list[str]] = []

    def fake_transfer(address, page):
        if address == SEED:
            return TransferPage(
                [transfer_row(index, SEED, neighbor) for index, neighbor in enumerate(neighbors)],
                None,
            )
        return TransferPage([], None)

    def fake_balance(_url, addresses, **_kwargs):
        balance_calls.append(list(addresses))
        return {}

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(service_module, "get_stake_transfer_page", lambda *_a: TransferPage([], None))
    monkeypatch.setattr(service_module, "query_account_balances", fake_balance)
    monkeypatch.setattr(service_module, "is_valid_ss58", lambda _address: True)

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "balance-cap.db")
        try:
            database.initialize()
            service = LinkService(database, "wss://example.invalid")
            result = run_relations(monkeypatch, depth=1, service=service)

            assert len(result["related"]) == 60
            assert balance_calls
            assert all(
                len(calls) <= service_module.MAX_BALANCE_ADDRESSES_PER_RESULT
                for calls in balance_calls
            )
            assert any("只读取前" in warning for warning in result["warnings"])
        finally:
            database.engine.dispose()
