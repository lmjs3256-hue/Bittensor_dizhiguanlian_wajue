"""测试环境：默认放行离线授权，并隔离本机设置文件。

授权校验本身在 test_license.py 里用临时密钥对单独测试；其余测试关注业务逻辑，
不需要每台跑测试的机器都真的激活过。

设置文件（Taostats key 等）也必须隔离：否则测试会读写开发机上的
``data/settings.json``，既污染本机，也让结果依赖环境。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _license_gate_open(monkeypatch, tmp_path):
    monkeypatch.setattr("app.license.is_licensed", lambda: True)
    # 导出内容不要受跑测试这台机器上的 license.key 影响，水印单独在
    # test_license.py 里验证。
    monkeypatch.setattr("app.license.watermark", lambda: "")
    # 每个测试一份独立的设置文件；同时清掉进程内缓存的 key。
    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    from app import taostats

    taostats.clear_api_key()
    yield
    taostats.clear_api_key()
