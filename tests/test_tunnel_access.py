"""公网隧道模式（cloudflared）的来源判定。

隧道把所有外部请求都变成「从 127.0.0.1 进来」。若沿用「对端是回环就当作
主机本机」的老判断，公网访客会直接免口令进站，甚至能读走访问口令本身。

这三件事必须同时成立：

1. 隧道模式下，回环连接但携带外网来源头 → 仍然要口令；
2. 非回环连接伪造同样的头 → 依然不算本机（头只在回环对端时才采信）；
3. 主机自己的浏览器（回环、无代理头）→ 保持免口令。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


TUNNEL_PEER = ("127.0.0.1", 54123)
LAN_PEER = ("192.168.0.50", 54123)
PUBLIC_IP = "198.51.100.7"


def _client(peer=TUNNEL_PEER):
    from fastapi.testclient import TestClient

    import app.main as main_module

    return TestClient(main_module.app, client=peer)


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    from app import settings_store

    path = tmp_path / "settings.json"
    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(path))
    monkeypatch.delenv("WALLET_LINK_REMOTE", raising=False)
    monkeypatch.delenv("WALLET_LINK_TRUST_PROXY", raising=False)
    return path


@pytest.fixture()
def licensed(monkeypatch):
    from app import license as license_mod

    monkeypatch.setattr(license_mod, "is_licensed", lambda: True)


def _enable_tunnel(monkeypatch) -> None:
    """--tunnel 启动时的环境：远程模式 + 采信代理头。"""

    monkeypatch.setenv("WALLET_LINK_REMOTE", "1")
    monkeypatch.setenv("WALLET_LINK_TRUST_PROXY", "1")


def _issue_token(settings_path: Path) -> str:
    from app import access

    token, _created = access.ensure_token()
    assert json.loads(settings_path.read_text(encoding="utf-8"))["access_token"] == token
    return token


def test_tunnel_request_needs_token(isolated_settings, licensed, monkeypatch):
    """经隧道进来的公网访客不能被当成主机本机。"""

    _enable_tunnel(monkeypatch)
    _issue_token(isolated_settings)

    response = _client().get("/api/library", headers={"CF-Connecting-IP": PUBLIC_IP})
    assert response.status_code == 401
    assert response.json()["code"] == "need_access_token"


def test_tunnel_request_accepts_header_token(isolated_settings, licensed, monkeypatch):
    _enable_tunnel(monkeypatch)
    token = _issue_token(isolated_settings)

    response = _client().get(
        "/api/library",
        headers={"CF-Connecting-IP": PUBLIC_IP, "X-Access-Token": token},
    )
    assert response.status_code == 200


def test_tunnel_login_cookie_still_works(isolated_settings, licensed, monkeypatch):
    """手机输一次口令后靠 Cookie 记住，隧道下这条路径不能坏。"""

    _enable_tunnel(monkeypatch)
    token = _issue_token(isolated_settings)
    headers = {"CF-Connecting-IP": PUBLIC_IP}
    client = _client()

    assert client.get("/api/library", headers=headers).status_code == 401
    login = client.post("/api/access/login", json={"token": token}, headers=headers)
    assert login.status_code == 200
    assert client.get("/api/library", headers=headers).status_code == 200


def test_tunnel_cannot_read_access_token(isolated_settings, licensed, monkeypatch):
    """最高危的一条：能远程读到口令，这道门就等于不存在。"""

    _enable_tunnel(monkeypatch)
    token = _issue_token(isolated_settings)
    headers = {"CF-Connecting-IP": PUBLIC_IP}
    client = _client()

    assert client.get("/api/access/token", headers=headers).status_code == 401

    client.post("/api/access/login", json={"token": token}, headers=headers)
    # 进得来，但不许看口令本身。
    assert client.get("/api/access/token", headers=headers).status_code == 403
    assert client.post("/api/access/token/rotate", headers=headers).status_code == 403


def test_lan_peer_cannot_forge_local(isolated_settings, licensed, monkeypatch):
    """局域网设备直连 0.0.0.0 时，带「我是 127.0.0.1」的头没有用。

    头只在回环对端时才采信——否则任何能连上 8766 的人都能自己发一个头免口令。
    """

    _enable_tunnel(monkeypatch)
    _issue_token(isolated_settings)

    response = _client(LAN_PEER).get(
        "/api/library", headers={"CF-Connecting-IP": "127.0.0.1"}
    )
    assert response.status_code == 401


def test_host_browser_stays_local(isolated_settings, licensed, monkeypatch):
    """主机上的浏览器没有代理头，仍按本机放行（不然本机也得输口令）。"""

    _enable_tunnel(monkeypatch)
    _issue_token(isolated_settings)

    assert _client().get("/api/library").status_code == 200


def test_x_forwarded_for_is_used_as_fallback(isolated_settings, licensed, monkeypatch):
    """没有 CF-Connecting-IP 时退回 X-Forwarded-For，取第一段（最初的客户端）。"""

    _enable_tunnel(monkeypatch)
    _issue_token(isolated_settings)

    response = _client().get(
        "/api/library", headers={"X-Forwarded-For": f"{PUBLIC_IP}, 172.70.1.1"}
    )
    assert response.status_code == 401


def test_proxy_headers_ignored_without_tunnel_flag(isolated_settings, licensed, monkeypatch):
    """未开隧道时不看请求头：普通 --remote 的行为必须原样保留。"""

    monkeypatch.setenv("WALLET_LINK_REMOTE", "1")
    _issue_token(isolated_settings)

    response = _client().get("/api/library", headers={"CF-Connecting-IP": PUBLIC_IP})
    assert response.status_code == 200


def test_status_reports_real_client_and_tunnel_flag(isolated_settings, licensed, monkeypatch):
    """界面上不该显示 127.0.0.1——那会让人以为是从主机本机访问的。"""

    _enable_tunnel(monkeypatch)
    _issue_token(isolated_settings)

    payload = _client().get("/api/access/status", headers={"CF-Connecting-IP": PUBLIC_IP}).json()
    assert payload["remote_mode"] is True
    assert payload["trust_proxy"] is True
    assert payload["via_tunnel"] is True
    assert payload["local"] is False
    assert payload["client"] == PUBLIC_IP
    assert payload["required"] is True
