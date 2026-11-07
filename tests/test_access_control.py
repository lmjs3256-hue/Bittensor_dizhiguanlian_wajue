"""远程访问口令（甲方案：一台主机跑服务，其余设备只当浏览器客户端）。

这些用例锁死三件事：

1. 只监听 127.0.0.1 时完全不校验——本机访问没有门槛，别把现有用法搞坏；
2. 开启远程模式后，非回环来源没有口令一律 401，健康检查仍放行；
3. 口令本身能生成、持久化、校验、重置，且只能在主机上查看。
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


REMOTE_CLIENT = ("203.0.113.9", 54123)
LOCAL_CLIENT = ("127.0.0.1", 54123)


def _client(remote_host: bool = False):
    from fastapi.testclient import TestClient

    import app.main as main_module

    target = REMOTE_CLIENT if remote_host else LOCAL_CLIENT
    return TestClient(main_module.app, client=target)


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    """把 settings.json 指到临时目录，避免碰到真实的口令与 key。"""

    from app import settings_store

    path = tmp_path / "settings.json"
    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(path))
    monkeypatch.delenv("WALLET_LINK_REMOTE", raising=False)
    return path


@pytest.fixture()
def licensed(monkeypatch):
    """业务接口只在已激活时可达，这里直接放行授权检查。"""

    from app import license as license_mod

    monkeypatch.setattr(license_mod, "is_licensed", lambda: True)
    return True


def _enable_remote(monkeypatch) -> None:
    monkeypatch.setenv("WALLET_LINK_REMOTE", "1")


def _issue_token(settings_path: Path) -> str:
    """在当前 settings 文件里生成并读回口令。"""

    from app import access

    token, _created = access.ensure_token()
    # 确认真的落盘了，而不是只在内存里。
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["access_token"] == token
    return token


def test_local_mode_needs_no_token(isolated_settings, licensed):
    """未开启远程模式：连外网来源也不校验（只有本机能连上来）。"""

    client = _client(remote_host=True)
    response = client.get("/api/library")
    assert response.status_code == 200


def test_remote_mode_rejects_remote_without_token(isolated_settings, licensed, monkeypatch):
    _enable_remote(monkeypatch)
    _issue_token(isolated_settings)

    response = _client(remote_host=True).get("/api/library")
    assert response.status_code == 401
    assert response.json()["code"] == "need_access_token"


def test_remote_mode_allows_loopback_without_token(isolated_settings, licensed, monkeypatch):
    """主机上打开浏览器不该要口令。"""

    _enable_remote(monkeypatch)
    _issue_token(isolated_settings)

    assert _client(remote_host=False).get("/api/library").status_code == 200


def test_remote_mode_accepts_header_token(isolated_settings, licensed, monkeypatch):
    _enable_remote(monkeypatch)
    token = _issue_token(isolated_settings)

    response = _client(remote_host=True).get(
        "/api/library", headers={"X-Access-Token": token}
    )
    assert response.status_code == 200


def test_health_stays_public_in_remote_mode(isolated_settings, licensed, monkeypatch):
    """健康检查要能在未登录时探测服务是否活着，否则客户端只会显示连接失败。"""

    _enable_remote(monkeypatch)
    _issue_token(isolated_settings)

    response = _client(remote_host=True).get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_wrong_token_is_rejected(isolated_settings, licensed, monkeypatch):
    _enable_remote(monkeypatch)
    _issue_token(isolated_settings)

    response = _client(remote_host=True).get(
        "/api/library", headers={"X-Access-Token": "AAAA-BBBB-CCCC-DDDD"}
    )
    assert response.status_code == 401


def test_login_sets_cookie_and_grants_access(isolated_settings, licensed, monkeypatch):
    _enable_remote(monkeypatch)
    token = _issue_token(isolated_settings)
    client = _client(remote_host=True)

    assert client.get("/api/library").status_code == 401

    login = client.post("/api/access/login", json={"token": token})
    assert login.status_code == 200
    assert login.json()["ok"] is True
    # HttpOnly：脚本读不到，避免被 XSS 顺走。
    cookie_header = login.headers.get("set-cookie", "")
    assert "wl_access=" in cookie_header
    assert "HttpOnly" in cookie_header

    # TestClient 会保留 Cookie，第二次请求自动带上。
    assert client.get("/api/library").status_code == 200


def test_login_accepts_lowercase_and_spaces(isolated_settings, licensed, monkeypatch):
    """手机上复制粘贴常带空格、大小写也会被输入法改掉。"""

    _enable_remote(monkeypatch)
    token = _issue_token(isolated_settings)
    typed = " " + token.lower().replace("-", " - ") + " "

    response = _client(remote_host=True).post("/api/access/login", json={"token": typed})
    assert response.status_code == 200


def test_login_rejects_wrong_token(isolated_settings, licensed, monkeypatch):
    _enable_remote(monkeypatch)
    _issue_token(isolated_settings)

    response = _client(remote_host=True).post(
        "/api/access/login", json={"token": "ZZZZ-ZZZZ-ZZZZ-ZZZZ"}
    )
    assert response.status_code == 401


def test_token_is_generated_once_and_persisted(isolated_settings):
    from app import access

    first, created = access.ensure_token()
    assert created is True
    assert len(first) == 19  # 16 位 + 3 个分隔横线
    assert isolated_settings.exists()

    second, created_again = access.ensure_token()
    assert second == first
    assert created_again is False


def test_rotate_invalidates_old_token(isolated_settings, licensed, monkeypatch):
    _enable_remote(monkeypatch)
    old = _issue_token(isolated_settings)

    rotate = _client(remote_host=False).post("/api/access/token/rotate")
    assert rotate.status_code == 200
    new = rotate.json()["token"]
    assert new != old

    # 旧口令立刻失效，新口令可用。
    assert (
        _client(remote_host=True)
        .get("/api/library", headers={"X-Access-Token": old})
        .status_code
        == 401
    )
    assert (
        _client(remote_host=True)
        .get("/api/library", headers={"X-Access-Token": new})
        .status_code
        == 200
    )


def test_token_only_viewable_from_host(isolated_settings, licensed, monkeypatch):
    """远程能读到口令的话，这道门等于不存在。

    两个层次的拒绝都要成立：未认证时先在中间件被 401 挡住（连「有口令这回事」
    都不确认），已认证的远程设备则明确 403——它进得来，但不许看口令本身。
    """

    _enable_remote(monkeypatch)
    token = _issue_token(isolated_settings)

    assert _client(remote_host=True).get("/api/access/token").status_code == 401

    logged_in = _client(remote_host=True)
    logged_in.post("/api/access/login", json={"token": token})
    assert logged_in.get("/api/access/token").status_code == 403
    assert logged_in.post("/api/access/token/rotate").status_code == 403

    payload = _client(remote_host=False).get("/api/access/token").json()
    assert payload["token"]
    assert payload["hint"].endswith(payload["token"][-4:])


def test_status_hides_hint_until_authenticated(isolated_settings, licensed, monkeypatch):
    _enable_remote(monkeypatch)
    token = _issue_token(isolated_settings)

    anonymous = _client(remote_host=True).get("/api/access/status").json()
    assert anonymous["required"] is True
    assert anonymous["authenticated"] is False
    assert "token_hint" not in anonymous  # 未认证不泄漏任何线索

    client = _client(remote_host=True)
    client.post("/api/access/login", json={"token": token})
    allowed = client.get("/api/access/status").json()
    assert allowed["required"] is False
    assert allowed["authenticated"] is True
    assert allowed["token_hint"]


def test_status_reports_remote_badge_flag(isolated_settings, licensed, monkeypatch):
    """前端靠 remote_mode + local 决定要不要显示「远程访问」角标。"""

    _enable_remote(monkeypatch)
    token = _issue_token(isolated_settings)

    client = _client(remote_host=True)
    client.post("/api/access/login", json={"token": token})
    payload = client.get("/api/access/status").json()
    assert payload["remote_mode"] is True
    assert payload["local"] is False
