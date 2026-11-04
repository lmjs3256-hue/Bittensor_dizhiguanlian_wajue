"""测试环境：默认放行离线授权。

授权校验本身在 test_license.py 里用临时密钥对单独测试；其余测试关注业务逻辑，
不需要每台跑测试的机器都真的激活过。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _license_gate_open(monkeypatch):
    monkeypatch.setattr("app.license.is_licensed", lambda: True)
    # 导出内容不要受跑测试这台机器上的 license.key 影响，水印单独在
    # test_license.py 里验证。
    monkeypatch.setattr("app.license.watermark", lambda: "")
