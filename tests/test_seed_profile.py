# -*- coding: utf-8 -*-
"""起始地址画像：注册日期 / 质押操作次数 / 日均频次 / 余额。"""

from datetime import date, timedelta

import pytest

from app import service as service_module
from app import taostats
from http_stub import as_http_get


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"


class _Row:
    """最小转账行：_seed_profile 只用 from_address / to_address。"""

    def __init__(self, source, target):
        self.from_address = source
        self.to_address = target


def _profile(monkeypatch, **kwargs):
    """直接调 _seed_profile：它是纯汇总逻辑，不依赖数据库。"""

    service = object.__new__(service_module.LinkService)
    params = {
        "transfer_rows": [],
        "data_source": "taostats",
        "include": True,
        "warnings": [],
    }
    params.update(kwargs)
    return service._seed_profile(SEED, **params)


@pytest.fixture(autouse=True)
def reset_taostats():
    taostats.reset_client_state_for_tests()
    yield
    taostats.reset_client_state_for_tests()


def test_rao_conversion():
    assert taostats._rao_to_tao_text("10224272802210") == "10224.27280221"
    assert taostats._rao_to_tao_text("0") == "0"
    assert taostats._rao_to_tao_text(None) is None
    assert taostats._rao_to_tao_text("") is None
    assert taostats._rao_to_tao_text("not-a-number") is None


def test_profile_combines_account_and_transfer_total(monkeypatch):
    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(
            created_on_date="2025-09-17",
            created_on_network="finney",
            balance_free_tao="10228.71",
            balance_staked_tao="6908.53",
            balance_total_tao="17137.24",
        ),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (1000, False))

    profile = _profile(monkeypatch)

    assert profile["registered_on"] == "2025-09-17"
    assert profile["registered_on_network"] == "finney"
    assert profile["stake_event_count"] == 1000
    assert profile["stake_event_scope"] == "taostats"
    # 兼容字段与主字段同值，避免读到 None。
    assert profile["transfer_count"] == 1000
    assert profile["balance_total_tao"] == "17137.24"
    assert profile["source"] == "taostats"
    # 日均频次 = 笔数 / 注册至今的天数，天数至少按 1 天算。
    expected_days = max((date.today() - date(2025, 9, 17)).days, 1)
    assert profile["active_days"] == expected_days
    assert profile["daily_frequency"] == round(1000 / expected_days, 2)


def test_profile_without_key_returns_empty_fields(monkeypatch):
    """没配 key 时四个指标都是 None，界面显示「未获取」，不能拿 0 顶替。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: False)
    profile = _profile(monkeypatch)
    assert profile["registered_on"] is None
    assert profile["transfer_count"] is None
    assert profile["daily_frequency"] is None
    assert profile["balance_total_tao"] is None
    assert profile["source"] is None


def test_profile_swallows_api_failures(monkeypatch):
    """接口报错只写 warning，主查询照常返回。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)

    def boom(address):
        raise taostats.TaostatsRateLimitError("限流", request_made=True)

    monkeypatch.setattr(service_module, "get_account_profile", boom)
    monkeypatch.setattr(service_module, "get_stake_event_total", boom)
    warnings: list[str] = []

    profile = _profile(monkeypatch, warnings=warnings)

    assert profile["registered_on"] is None
    assert profile["transfer_count"] is None
    assert any("注册日期" in item for item in warnings)
    assert any("质押操作次数" in item for item in warnings)


def test_profile_does_not_fake_stake_count_from_transfers(monkeypatch):
    """配了 key 但质押接口失败时，不能拿「本地转账 1 笔」冒充「质押 1 次」。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date="2025-09-17"),
    )

    def boom(address):
        raise taostats.TaostatsRateLimitError("限流", request_made=True)

    monkeypatch.setattr(service_module, "get_stake_event_total", boom)
    warnings: list[str] = []

    profile = _profile(
        monkeypatch, transfer_rows=[_Row(SEED, "A")], data_source="taostats", warnings=warnings
    )

    assert profile["stake_event_count"] is None
    assert profile["stake_event_scope"] is None
    # 次数拿不到，日均频次也不能拿转账笔数算出来。
    assert profile["daily_frequency"] is None
    assert any("质押操作次数未获取" in item for item in warnings)


def test_profile_falls_back_to_local_count(monkeypatch):
    """完全没 key 时用本地索引笔数，并标明口径（不是质押次数）。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: False)
    rows = [_Row(SEED, "A"), _Row("B", SEED), _Row("C", "D")]

    profile = _profile(monkeypatch, transfer_rows=rows, data_source="local")

    assert profile["stake_event_count"] == 2
    assert profile["stake_event_scope"] == "local_index"
    # 没有注册日期就算不出日均频次。
    assert profile["daily_frequency"] is None


def test_profile_respects_include_flag(monkeypatch):
    """停止续查时不该再花请求去取画像。"""

    called = []
    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module, "get_account_profile", lambda address: called.append(address)
    )

    profile = _profile(monkeypatch, include=False)

    assert called == []
    assert profile["transfer_count"] is None


def test_profile_handles_bad_registration_date(monkeypatch):
    """注册日期格式异常时只跳过日均频次，不影响质押操作次数。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date="N/A"),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (50, False))

    profile = _profile(monkeypatch)

    assert profile["stake_event_count"] == 50
    assert profile["daily_frequency"] is None
    assert profile["active_days"] is None


def test_profile_daily_frequency_uses_one_day_floor(monkeypatch):
    """当天注册时天数按 1 天算，避免除以 0 或算出天文数字。"""

    today = date.today().isoformat()
    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date=today),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (7, False))

    profile = _profile(monkeypatch)

    assert profile["active_days"] == 1
    assert profile["daily_frequency"] == 7.0


# -- 质押操作次数：走 /v1/subnets/stake-events 的 action=all -----------------

STAKE_URL = taostats.STAKE_EVENTS_URL


def _stake_response(total_items, status=200):
    import httpx

    return httpx.Response(
        status,
        json={"data": [], "pagination": {"total_items": total_items, "per_page": 1}},
        request=httpx.Request("GET", STAKE_URL),
    )


def test_stake_event_total_requests_action_all_with_limit_one(monkeypatch):
    """必须用 action=all + limit=1：一次请求拿到 stake+unstake 的真实总量。"""

    monkeypatch.setenv("TAOSTATS_API_KEY", "test-key")
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return _stake_response(906)

    monkeypatch.setattr(taostats, "_http_get", as_http_get(fake_get))

    total, cached = taostats.get_stake_event_total(SEED)

    assert total == 906
    assert cached is False
    assert len(calls) == 1
    url, params = calls[0]
    assert url == STAKE_URL
    assert params["coldkey"] == SEED
    assert params["action"] == "all"
    # 只要分页里的 total_items，不必真取数据行。
    assert params["limit"] == 1
    assert params["page"] == 1
    # 不能带 is_transfer，那会把「质押转移」也算进口径。
    assert "is_transfer" not in params


def test_stake_event_total_is_cached(monkeypatch):
    monkeypatch.setenv("TAOSTATS_API_KEY", "test-key")
    calls = []

    def fake_get(url, **kwargs):
        calls.append(1)
        return _stake_response(12)

    monkeypatch.setattr(taostats, "_http_get", as_http_get(fake_get))

    first, cached_first = taostats.get_stake_event_total(SEED)
    second, cached_second = taostats.get_stake_event_total(SEED)

    assert (first, cached_first) == (12, False)
    assert (second, cached_second) == (12, True)
    assert len(calls) == 1


def test_stake_event_total_raises_on_rate_limit(monkeypatch):
    monkeypatch.setenv("TAOSTATS_API_KEY", "test-key")
    monkeypatch.setattr(taostats, "_http_get", as_http_get(lambda *a, **k: _stake_response(0, status=429)))

    with pytest.raises(taostats.TaostatsRateLimitError):
        taostats.get_stake_event_total(SEED)


def test_stake_event_total_requires_key(monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)

    with pytest.raises(taostats.TaostatsAuthError):
        taostats.get_stake_event_total(SEED)


def test_profile_stake_count_and_frequency(monkeypatch):
    """质押 475 + 解质押 431 = 906 次，日均 = 906 / 天数。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date="2025-09-17"),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (906, False))

    profile = _profile(monkeypatch)

    assert profile["stake_event_count"] == 906
    assert profile["stake_event_scope"] == "taostats"
    days = max((date.today() - date(2025, 9, 17)).days, 1)
    assert profile["daily_frequency"] == round(906 / days, 2)


# -- 本地索引也要走 API 拿质押次数 ------------------------------------------


def test_local_source_still_uses_api_for_stake_count(monkeypatch):
    """本地索引没有 stake/unstake 事件，质押次数只能由 API 给。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date="2025-09-17"),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (906, False))

    profile = _profile(monkeypatch, data_source="local", transfer_rows=[_Row(SEED, "A")])

    # 不该因为选了本地索引就退回「本地转账 1 笔」。
    assert profile["stake_event_count"] == 906
    assert profile["stake_event_scope"] == "taostats"


def test_profile_counts_only_real_network_requests(monkeypatch):
    """缓存命中不算请求数：前端靠它扣预算，算多了会白扣额度。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(
            created_on_date="2025-09-17", from_cache=True
        ),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (906, True))

    profile = _profile(monkeypatch)

    assert profile["api_requests"] == 0


def test_profile_reports_two_requests_on_full_miss(monkeypatch):
    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date="2025-09-17"),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (906, False))

    profile = _profile(monkeypatch)

    # 注册日期/余额 1 次 + 质押总量 1 次，固定 2 次。
    assert profile["api_requests"] == 2


def test_profile_records_api_usage(monkeypatch):
    """画像请求要计入「Taostats 查询次数」统计，否则用量面板会少算。"""

    recorded: list[tuple[str, bool]] = []

    class _FakeDb:
        def record_api_request(self, endpoint, *, status_code=None, success=False):
            recorded.append((endpoint, success))

    service = object.__new__(service_module.LinkService)
    service.database = _FakeDb()
    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date="2025-09-17"),
    )
    monkeypatch.setattr(service_module, "get_stake_event_total", lambda address: (906, False))

    service._seed_profile(
        SEED,
        transfer_rows=[],
        data_source="taostats",
        include=True,
        warnings=[],
    )

    assert recorded == [
        ("/api/account/latest/v1", True),
        ("/v1/subnets/stake-events", True),
    ]
