"""本机专属面板与接口：远程设备看不到、也改不了。

**用户报障（原话）**：「远程访问板块现在不仅仅本机可见了，远程的电脑手机都能
看到。另外 APIkey 也要只本机可见。」

排查发现这不止是「面板可见」，而是**来源判定被绕过**：

- 服务用 ``--remote`` 启动（不是 ``--tunnel``），于是 ``trust_proxy()`` 为
  False，不采信 ``CF-Connecting-IP``；
- cloudflared 从 127.0.0.1 连进来，对端是回环 → ``is_local()`` 为 True；
- 结果：任何拿到隧道网址的人都被当成「坐在主机前」。

实测（经隧道、不带任何 Cookie）：

    GET /api/access/status  → local: true，endpoints 里含完整访问口令
    GET /api/access/token   → 直接返回 "JX9C-GVKL-JWGS-EXBA"
    POST /api/settings/taostats/rate → 200，能改额度

修法两层：

1. **来源判定不再依赖启动参数**：对端回环 **且** 带转发头 ⇒ 经代理进来。
   本机浏览器不会带 ``CF-Connecting-IP``，所以不会误判（反向边界有用例守）。
2. **管理面留在主机**：key 池的读、写、限速、清空全部加本机校验；前端把
   「远程访问」面板和「Taostats API key」面板整块隐藏（不是打码）。

这两层要同时存在：只隐藏 UI 挡不住直接请求；只挡接口则远程仍能看到主机
地址清单和口令提示。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

TUNNEL_PEER = ("127.0.0.1", 54123)
LOCAL_PEER = ("127.0.0.1", 54123)
PUBLIC_IP = "198.51.100.7"


def _client(peer=LOCAL_PEER):
    from fastapi.testclient import TestClient

    import app.main as main_module

    return TestClient(main_module.app, client=peer)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """远程模式 + 一个已签发的口令（不设置 TRUST_PROXY，模拟 --remote 启动）。"""

    from app import access, settings_store

    path = tmp_path / "settings.json"
    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(path))
    monkeypatch.setenv("WALLET_LINK_REMOTE", "1")
    monkeypatch.delenv("WALLET_LINK_TRUST_PROXY", raising=False)

    from app import license as license_mod
    from app import taostats

    monkeypatch.setattr(license_mod, "is_licensed", lambda: True)

    # 限速是**进程级全局状态**，不跟着 settings 文件走。
    # 这里会调 POST /api/settings/taostats/rate 真改它；不还原的话，后面所有
    # 「每分钟 5 次」口径的测试（relation_jobs / request_priority / taostats /
    # seed_profile）全会被带跑——实测把 8 条 job 测试跑成 "waiting → complete"。
    original_rate = taostats.requests_per_minute()
    token, _ = access.ensure_token()
    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == token
    try:
        yield {"token": token, "settings": path}
    finally:
        taostats.set_requests_per_minute(original_rate)


def _tunnel_headers(token: str | None = None) -> dict[str, str]:
    headers = {"CF-Connecting-IP": PUBLIC_IP}
    if token:
        headers["X-Access-Token"] = token
    return headers


# -- 第一层：来源判定 -------------------------------------------------------
def test_tunnel_without_tunnel_flag_is_not_local(env):
    """--remote 启动、隧道另起：隧道请求不能被当成本机。"""

    payload = _client().get("/api/access/status", headers=_tunnel_headers()).json()
    assert payload["local"] is False
    assert payload["via_tunnel"] is True
    assert payload["client"] == PUBLIC_IP
    # 未过口令时连口令线索都不给
    assert "token_hint" not in payload
    assert "endpoints" not in payload


def test_tunnel_cannot_read_access_token_without_password(env):
    """最高危：能远程读到口令，这道门就等于不存在。"""

    assert _client().get("/api/access/token", headers=_tunnel_headers()).status_code == 401


def test_tunnel_cannot_read_access_token_even_when_logged_in(env):
    client = _client()
    login = client.post(
        "/api/access/login", json={"token": env["token"]}, headers=_tunnel_headers()
    )
    assert login.status_code == 200
    assert client.get("/api/access/token", headers=_tunnel_headers()).status_code == 403
    assert (
        client.post("/api/access/token/rotate", headers=_tunnel_headers()).status_code
        == 403
    )


def test_local_browser_still_no_password(env):
    """反向边界：主机自己的浏览器（回环、无转发头）必须照旧免口令。"""

    payload = _client().get("/api/access/status").json()
    assert payload["local"] is True
    assert payload["endpoints"]["token"] == env["token"]


# -- 第二层：key 管理接口只给本机 -------------------------------------------
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/settings/taostats/keys", None),
        ("post", "/api/settings/taostats/keys", {"keys": []}),
        ("post", "/api/settings/taostats/rate", {"per_minute": 60}),
        ("delete", "/api/settings/taostats", None),
        ("post", "/api/settings/taostats", {"api_key": "abc"}),
    ],
)
def test_key_management_is_local_only(env, method, path, body):
    """已登录的远程也不许碰 key：知道口令不等于能改配置。"""

    client = _client()
    client.post("/api/access/login", json={"token": env["token"]}, headers=_tunnel_headers())
    headers = _tunnel_headers()
    call = getattr(client, method)
    response = call(path, headers=headers, **({"json": body} if body is not None else {}))
    assert response.status_code == 403
    assert "那台电脑" in response.json()["detail"]


def test_key_management_works_for_local(env):
    """本机必须照常可用，别为了挡远程把主机自己也挡了。"""

    client = _client()
    assert client.get("/api/settings/taostats/keys").status_code == 200
    assert (
        client.post("/api/settings/taostats/rate", json={"per_minute": 60}).status_code
        == 200
    )


def test_unauthorized_tunnel_is_401_before_403(env):
    """没口令的隧道访客先被口令挡住（401），不该先看到 403 里的管理面信息。"""

    assert _client().get("/api/settings/taostats/keys", headers=_tunnel_headers()).status_code == 401


# -- 前端契约 ---------------------------------------------------------------
def test_frontend_hides_local_only_panels():
    assert "function applyLocalOnlyVisibility(" in APP_JS
    assert '"#hero-panel"' in APP_JS
    assert '".api-key-panel"' in APP_JS
    start = APP_JS.index("function applyLocalOnlyVisibility(")
    body = APP_JS[start : start + 900]
    assert "state.isLocal" in body
    assert "classList.toggle(\"hidden\", !state.isLocal)" in body


def test_frontend_does_not_fetch_key_pool_when_remote():
    """远程别去撞 403：key 池接口不请求，状态用 overview 的布尔值。"""

    start = APP_JS.index("async function loadKeyPool()")
    body = APP_JS[start : start + 700]
    assert "if (!state.isLocal)" in body
    assert body.index("if (!state.isLocal)") < body.index("/api/settings/taostats/keys")


def test_frontend_keeps_is_local_state():
    assert "isLocal: true," in APP_JS
    assert "state.isLocal = status ? status.local !== false : true;" in APP_JS


# -- overview 也不再给远程管理细节 -----------------------------------------
def test_overview_hides_key_storage_from_remote(env, monkeypatch):
    """远程能用 Taostats，但不需要知道 key 是 DPAPI 还是明文存的。"""

    # storage_kind 读的是磁盘上真实保存记录；临时环境里没有，先钉一个值，
    # 否则"远程返回空"和"本来就没存过"无法区分，测试会假通过。
    from app import main as main_module

    monkeypatch.setattr(main_module.settings_store, "storage_kind", lambda: "dpapi")

    client = _client()
    client.post("/api/access/login", json={"token": env["token"]}, headers=_tunnel_headers())

    remote = client.get("/api/overview", headers=_tunnel_headers()).json()
    assert remote["taostats_key_storage"] == ""
    assert "taostats_configured" in remote  # 这个仍然要给：决定数据源能不能用

    local = _client().get("/api/overview").json()
    assert local["taostats_key_storage"] == "dpapi"

