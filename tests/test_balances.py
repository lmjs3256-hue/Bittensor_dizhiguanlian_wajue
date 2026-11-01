from decimal import Decimal

import app.collector as collector


class FakeSubstrate:
    def __init__(self, _url):
        self.runtime_calls = []

    async def initialize(self):
        return None

    async def query(self, module, storage, params):
        assert (module, storage, params) == ("System", "Account", ["5Fake"])
        return {"data": {"free": 2_000_000_000}}

    async def runtime_call(self, api, method, params):
        self.runtime_calls.append((api, method, params))
        if method == "get_stake_info_for_coldkey":
            return [
                {"netuid": 0, "stake": 500_000_000},
                {"netuid": 1, "stake": 2_000_000_000},
            ]
        assert (api, method) == ("SwapRuntimeApi", "current_alpha_price")
        return 500_000_000

    async def close(self):
        return None


def test_wallet_value_converts_alpha_positions_to_tao(monkeypatch):
    monkeypatch.setattr(collector, "AsyncSubstrateInterface", FakeSubstrate)

    result = collector.query_wallet_values("wss://example.invalid", ["5Fake"])

    assert result["5Fake"]["available_tao"] == Decimal("2")
    assert result["5Fake"]["staked_tao"] == Decimal("1.5")
    assert result["5Fake"]["total_value_tao"] == Decimal("3.5")
    assert result["5Fake"]["balance_status"] == "complete"


class PartialSubstrate(FakeSubstrate):
    async def runtime_call(self, api, method, params):
        if method == "get_stake_info_for_coldkey":
            raise RuntimeError("stake runtime unavailable")
        return await super().runtime_call(api, method, params)


def test_wallet_value_does_not_call_free_balance_a_complete_total(monkeypatch):
    monkeypatch.setattr(collector, "AsyncSubstrateInterface", PartialSubstrate)

    result = collector.query_wallet_values("wss://example.invalid", ["5Fake"])

    assert result["5Fake"]["available_tao"] == Decimal("2")
    assert result["5Fake"]["staked_tao"] is None
    assert result["5Fake"]["total_value_tao"] is None
    assert result["5Fake"]["balance_status"] == "partial"
