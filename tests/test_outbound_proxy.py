# -*- coding: utf-8 -*-
"""出站代理选择与「连不上 ≠ 没有 key」的区分（0.8.16）。

背景：这台机器直连 taostats.io 会超时，必须走本机代理。但 httpx 不读
Windows 系统代理设置，于是出现「浏览器能开、服务查询一直超时」；查询静默
退回本地索引后，页面还把原因报成「没有可用的 API key」，把排查带错方向。
"""

from __future__ import annotations

import httpx
import pytest

from app import taostats


@pytest.fixture(autouse=True)
def clean_proxy_state(monkeypatch):
    """每个用例都从干净的决策状态开始，别让拉黑记录互相污染。

    conftest 为了让测试不触网，全局设了 ``WALLET_LINK_HTTP_PROXY=none``；
    这组用例专门验证候选顺序，必须把它清掉，否则永远只返回「强制直连」。
    """

    monkeypatch.delenv(taostats._PROXY_OVERRIDE_ENV, raising=False)
    taostats._reset_proxy_decision()
    yield
    taostats._reset_proxy_decision()


class TestProxyServerParsing:
    def test_plain_host_port_gets_scheme(self):
        assert taostats._normalize_proxy_server("127.0.0.1:7890") == "http://127.0.0.1:7890"

    def test_per_protocol_form_prefers_https(self):
        raw = "http=127.0.0.1:7890;https=127.0.0.1:7891"
        assert taostats._normalize_proxy_server(raw) == "http://127.0.0.1:7891"

    def test_per_protocol_form_falls_back_to_http(self):
        raw = "http=127.0.0.1:7890;socks=127.0.0.1:7892"
        assert taostats._normalize_proxy_server(raw) == "http://127.0.0.1:7890"

    def test_blank_returns_none(self):
        assert taostats._normalize_proxy_server("   ") is None
        assert taostats._normalize_proxy_server("") is None

    def test_existing_scheme_is_kept(self):
        assert taostats._normalize_proxy_server("socks5://127.0.0.1:7891") == "socks5://127.0.0.1:7891"


class TestCandidateOrder:
    def test_system_proxy_comes_first(self, monkeypatch):
        monkeypatch.setattr(taostats, "_wininet_proxy_url", lambda: "http://127.0.0.1:7890")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:50806")
        candidates = taostats._proxy_candidates()
        assert candidates[0] == ("http://127.0.0.1:7890", "系统代理")
        assert candidates[1] == ("http://127.0.0.1:50806", "环境变量")
        assert candidates[-1] == (None, "直连")

    def test_env_override_wins_over_system_proxy(self, monkeypatch):
        monkeypatch.setattr(taostats, "_wininet_proxy_url", lambda: "http://127.0.0.1:7890")
        monkeypatch.setenv(taostats._PROXY_OVERRIDE_ENV, "127.0.0.1:9999")
        assert taostats._proxy_candidates() == [("http://127.0.0.1:9999", "env-override")]

    def test_env_override_can_force_direct(self, monkeypatch):
        monkeypatch.setattr(taostats, "_wininet_proxy_url", lambda: "http://127.0.0.1:7890")
        monkeypatch.setenv(taostats._PROXY_OVERRIDE_ENV, "none")
        assert taostats._proxy_candidates() == [(None, "env-override")]

    def test_no_proxy_anywhere_falls_back_to_direct(self, monkeypatch):
        monkeypatch.setattr(taostats, "_wininet_proxy_url", lambda: None)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(taostats._PROXY_OVERRIDE_ENV, raising=False)
        assert taostats._proxy_candidates() == [(None, "直连")]


class TestFailover:
    def test_failed_path_is_banned_and_next_is_used(self, monkeypatch):
        monkeypatch.setattr(
            taostats,
            "_proxy_candidates",
            lambda: [("http://p1", "系统代理"), ("http://p2", "环境变量"), (None, "直连")],
        )
        assert taostats._active_proxy() == ("http://p1", "系统代理")
        taostats._note_proxy_failure("http://p1", "系统代理", httpx.ConnectTimeout("boom"))
        assert taostats._active_proxy() == ("http://p2", "环境变量")

    def test_all_paths_banned_retries_from_the_top(self, monkeypatch):
        monkeypatch.setattr(
            taostats,
            "_proxy_candidates",
            lambda: [("http://p1", "系统代理"), (None, "直连")],
        )
        taostats._note_proxy_failure("http://p1", "系统代理", httpx.ConnectTimeout("a"))
        taostats._note_proxy_failure(None, "直连", httpx.ConnectTimeout("b"))
        # 全被拉黑时不能返回空——退回第一条，宁可再试一次也不要卡死。
        assert taostats._active_proxy() == ("http://p1", "系统代理")

    def test_ban_expires(self, monkeypatch):
        monkeypatch.setattr(
            taostats,
            "_proxy_candidates",
            lambda: [("http://p1", "系统代理"), (None, "直连")],
        )
        taostats._note_proxy_failure("http://p1", "系统代理", httpx.ConnectTimeout("a"))
        # 把拉黑截止时间拨到过去，模拟 10 分钟已过。
        with taostats._proxy_state_lock:
            taostats._proxy_state["banned"]["http://p1"] = 0.0
        assert taostats._decide_proxy()[0] == "http://p1"


class TestRequestGetFailover:
    def test_transport_error_switches_path_and_retries(self, monkeypatch):
        monkeypatch.setattr(
            taostats,
            "_proxy_candidates",
            lambda: [("http://dead", "系统代理"), (None, "直连")],
        )
        attempts: list[str | None] = []

        class FakeClient:
            def __init__(self, proxy):
                self.proxy = proxy

            def get(self, url, **kwargs):
                attempts.append(self.proxy)
                if self.proxy == "http://dead":
                    raise httpx.ConnectTimeout("dead proxy")
                return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

        monkeypatch.setattr(taostats, "_client_for", lambda proxy: FakeClient(proxy))
        monkeypatch.setattr(taostats.time, "sleep", lambda _seconds: None)

        response = taostats._request_get("https://api.example/x", api_key="k")
        assert response.status_code == 200
        # 第一条路失败后应换到直连再试，而不是在死代理上重试两次。
        assert attempts == ["http://dead", None]

    def test_http_status_is_not_retried(self, monkeypatch):
        attempts: list[int] = []

        class FakeClient:
            def __init__(self, proxy):
                self.proxy = proxy

            def get(self, url, **kwargs):
                attempts.append(1)
                return httpx.Response(
                    401, json={"error": "nope"}, request=httpx.Request("GET", url)
                )

        monkeypatch.setattr(taostats, "_client_for", lambda proxy: FakeClient(proxy))
        response = taostats._request_get("https://api.example/x", api_key="k")
        # 401 是业务状态码，交给调用方判定，不能当网络抖动重试。
        assert response.status_code == 401
        assert len(attempts) == 1


class TestProbeReport:
    def test_probe_reports_each_candidate(self, monkeypatch):
        monkeypatch.setattr(
            taostats,
            "_proxy_candidates",
            lambda: [("http://good", "系统代理"), (None, "直连")],
        )

        class FakeClient:
            def __init__(self, proxy):
                self.proxy = proxy

            def head(self, url, timeout=None):
                if self.proxy == "http://good":
                    return httpx.Response(200, request=httpx.Request("HEAD", url))
                raise httpx.ConnectTimeout("nope")

        monkeypatch.setattr(taostats, "_client_for", lambda proxy: FakeClient(proxy))
        report = taostats.probe_outbound()
        by_proxy = {item["proxy"]: item for item in report["results"]}
        assert by_proxy["http://good"]["ok"] is True
        assert by_proxy[None]["ok"] is False
        assert "ConnectTimeout" in by_proxy[None]["error"]
