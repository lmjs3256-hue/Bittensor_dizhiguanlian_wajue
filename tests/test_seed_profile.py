# -*- coding: utf-8 -*-
"""起始地址画像：注册日期 / 历史交易次数 / 日均频次 / 余额。"""

from datetime import date, timedelta

import pytest

from app import service as service_module
from app import taostats


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
    monkeypatch.setattr(service_module, "get_transfer_total", lambda address: (1000, False))

    profile = _profile(monkeypatch)

    assert profile["registered_on"] == "2025-09-17"
    assert profile["registered_on_network"] == "finney"
    assert profile["transfer_count"] == 1000
    assert profile["transfer_count_scope"] == "taostats"
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
    monkeypatch.setattr(service_module, "get_transfer_total", boom)
    warnings: list[str] = []

    profile = _profile(monkeypatch, warnings=warnings)

    assert profile["registered_on"] is None
    assert profile["transfer_count"] is None
    assert any("注册日期" in item for item in warnings)
    assert any("交易次数" in item for item in warnings)


def test_profile_falls_back_to_local_count(monkeypatch):
    """Taostats 不可用时，用本地索引里的笔数并标明口径。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: False)
    rows = [_Row(SEED, "A"), _Row("B", SEED), _Row("C", "D")]

    profile = _profile(monkeypatch, transfer_rows=rows, data_source="local")

    assert profile["transfer_count"] == 2
    assert profile["transfer_count_scope"] == "local_index"
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
    """注册日期格式异常时只跳过日均频次，不影响交易次数。"""

    monkeypatch.setattr(service_module, "taostats_is_configured", lambda: True)
    monkeypatch.setattr(
        service_module,
        "get_account_profile",
        lambda address: taostats.AccountProfile(created_on_date="N/A"),
    )
    monkeypatch.setattr(service_module, "get_transfer_total", lambda address: (50, False))

    profile = _profile(monkeypatch)

    assert profile["transfer_count"] == 50
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
    monkeypatch.setattr(service_module, "get_transfer_total", lambda address: (7, False))

    profile = _profile(monkeypatch)

    assert profile["active_days"] == 1
    assert profile["daily_frequency"] == 7.0
