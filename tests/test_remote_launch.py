"""远程访问按钮 / 公网隧道的回归测试（0.8.48：按钮不再重启服务）。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import remote_launch

ROOT = Path(__file__).resolve().parent.parent
REMOTE_CLIENT = ("203.0.113.9", 54123)
LOCAL_CLIENT = ("127.0.0.1", 54123)


@pytest.fixture()
def isolated_launch(tmp_path, monkeypatch):
    """把状态与隧道文件指到临时目录，并禁止后台线程真的启动。"""

    state = tmp_path / "remote-launch.json"
    monkeypatch.setattr(remote_launch, "STATE_FILE", state)
    monkeypatch.setattr(remote_launch, "LOG_DIR", tmp_path)
    monkeypatch.setattr(remote_launch, "URL_FILE", tmp_path / "cloudflared.url")
    monkeypatch.setattr(remote_launch, "CF_LOG", tmp_path / "cloudflared.log")
    monkeypatch.setattr(remote_launch, "CF_OUT", tmp_path / "cloudflared.stdout.log")
    monkeypatch.setattr(remote_launch, "CLOUDFLARED", tmp_path / "cloudflared.exe")

    # start_launch() 用 os.environ[...] = "1" 直接赋值，monkeypatch 追踪不到这种
    # 写入；必须自己保存/恢复，否则 WALLET_LINK_REMOTE=1 会泄漏到后续测试文件，
    # 让所有接口测试被要求输访问口令（401）。
    env_names = (
        "WALLET_LINK_BIND_HOST",
        "WALLET_LINK_REMOTE",
        "WALLET_LINK_TRUST_PROXY",
    )
    saved = {name: os.environ.get(name) for name in env_names}
    for name in env_names:
        os.environ.pop(name, None)

    class NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    # 只替换 remote_launch 模块里看到的 threading 名字。直接改全局
    # threading.Thread 会把 FastAPI TestClient 的内部线程也一起废掉，接口测试会挂住。
    class ThreadingStub:
        Lock = threading.Lock
        Thread = NoopThread

    monkeypatch.setattr(remote_launch, "threading", ThreadingStub)
    yield state
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture()
def licensed(monkeypatch):
    from app import license as license_mod

    monkeypatch.setattr(license_mod, "is_licensed", lambda: True)


def _client(remote_host: bool = False):
    from fastapi.testclient import TestClient

    import app.main as main_module

    return TestClient(
        main_module.app,
        client=REMOTE_CLIENT if remote_host else LOCAL_CLIENT,
    )


# -- 运行时切换 ------------------------------------------------------------


def test_start_launch_rejects_unknown_mode(isolated_launch):
    with pytest.raises(ValueError):
        remote_launch.start_launch("anything")


def test_start_launch_enables_remote_without_restart(isolated_launch, monkeypatch):
    """0.8.48：只改环境变量，不 spawn 启动器、不重启服务。"""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("按钮不该再 spawn 任何重启进程")

    monkeypatch.setattr(remote_launch.subprocess, "Popen", forbidden)
    result = remote_launch.start_launch("remote")
    assert result["ok"] is True
    assert result["mode"] == "remote"
    assert os.environ.get("WALLET_LINK_REMOTE") == "1"
    assert result["message"] == "远程访问已开启"
    data = json.loads(isolated_launch.read_text(encoding="utf-8"))
    assert data["state"] == "launching"


def test_launch_on_loopback_bind_is_rejected(isolated_launch, monkeypatch):
    """绑在 127.0.0.1 时开远程没有意义，必须给一次性明确指引。"""

    monkeypatch.setenv("WALLET_LINK_BIND_HOST", "127.0.0.1")
    with pytest.raises(RuntimeError, match="只监听本机"):
        remote_launch.start_launch("remote")
    assert os.environ.get("WALLET_LINK_REMOTE") != "1"
    assert not isolated_launch.exists()


def test_second_launch_while_busy_is_rejected(isolated_launch):
    remote_launch.start_launch("remote")
    with pytest.raises(RuntimeError, match="已有一次启动"):
        remote_launch.start_launch("tunnel")


def test_finish_launch_remote_writes_ready(isolated_launch):
    remote_launch.write_state(state="launching", mode="remote", started_at=remote_launch._now())
    remote_launch._finish_launch("remote", 8766)
    data = remote_launch.read_state()
    assert data["state"] == "ready"
    assert data["message"] == "远程访问已开启"


def test_finish_launch_tunnel_sets_trust_proxy(isolated_launch, monkeypatch):
    monkeypatch.setattr(remote_launch, "_start_tunnel", lambda port: "https://x.trycloudflare.com")
    remote_launch.write_state(state="launching", mode="tunnel", started_at=remote_launch._now())
    remote_launch._finish_launch("tunnel", 8766)
    assert os.environ.get("WALLET_LINK_TRUST_PROXY") == "1"
    data = remote_launch.read_state()
    assert data["state"] == "ready"
    assert data["url"] == "https://x.trycloudflare.com"


# -- 状态机 ----------------------------------------------------------------


def test_read_state_survives_corrupt_file(isolated_launch):
    isolated_launch.write_text("{not json", encoding="utf-8")
    assert remote_launch.read_state()["state"] == "idle"


def test_write_state_merges_fields(isolated_launch):
    remote_launch.write_state(state="launching", mode="remote", started_at="T0")
    remote_launch.write_state(state="ready", message="ok")
    data = remote_launch.read_state()
    assert data["started_at"] == "T0"
    assert data["state"] == "ready"
    assert data["message"] == "ok"


def test_stale_launching_state_is_auto_resolved(isolated_launch):
    old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat(timespec="seconds")
    isolated_launch.write_text(
        json.dumps(
            {
                "state": "launching",
                "mode": "tunnel",
                "message": "正在建立…",
                "url": "",
                "started_at": old,
                "finished_at": "",
                "updated_at": old,
            }
        ),
        encoding="utf-8",
    )
    data = remote_launch.read_state()
    assert data["state"] == "error"
    assert "没有完成" in data["message"]


def test_fresh_launching_state_is_kept(isolated_launch):
    remote_launch.write_state(
        state="launching", mode="remote", started_at=remote_launch._now()
    )
    assert remote_launch.read_state()["state"] == "launching"


# -- HTTP 接口 -------------------------------------------------------------


def test_launch_endpoint_is_local_only(isolated_launch, licensed):
    with _client(remote_host=True) as client:
        response = client.post("/api/access/launch", json={"mode": "remote"})
    assert response.status_code == 403


def test_launch_endpoint_rejects_unknown_mode(isolated_launch, licensed):
    with _client() as client:
        response = client.post("/api/access/launch", json={"mode": "bogus"})
    assert response.status_code == 422


def test_launch_endpoint_accepts_valid_mode(isolated_launch, licensed):
    with _client() as client:
        response = client.post("/api/access/launch", json={"mode": "remote"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_access_status_exposes_launch_and_start_time(isolated_launch, licensed):
    remote_launch.write_state(state="ready", mode="tunnel", message="ok", url="https://x.trycloudflare.com")
    with _client() as client:
        payload = client.get("/api/access/status").json()
    assert payload["launch"]["state"] == "ready"
    assert payload["launch"]["url"] == "https://x.trycloudflare.com"
    assert payload["server_started_at"]


# -- 静态契约 --------------------------------------------------------------


def test_no_restart_launcher_contract():
    """代码里不能再有「spawn 启动器 / 服务自杀」那套东西。"""

    module = (ROOT / "app" / "remote_launch.py").read_text(encoding="utf-8")
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    run = (ROOT / "run.py").read_text(encoding="utf-8")
    assert "LAUNCH_SCRIPT" not in module
    assert "sys.executable" not in module
    assert "_schedule_self_shutdown" not in main
    assert "WALLET_LINK_BIND_HOST" in run
    assert "WALLET_LINK_BIND_HOST" in module


def test_remote_panel_has_both_launch_buttons():
    html = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/styles.css").read_text(encoding="utf-8")
    assert 'id="launch-remote"' in html
    assert 'id="launch-tunnel"' in html
    assert "/api/access/launch" in js
    assert "server_started_at" in js
    assert "服务不会重启" in js
    assert ".remote-actions" in css


def test_tunnel_script_writes_url_before_starting_service():
    """老顺序先启服务、后建隧道，服务启动时永远读不到新网址。"""

    text = (ROOT / "tunnel.ps1").read_text(encoding="utf-8-sig")
    write_at = text.index("Set-Content -LiteralPath $UrlFile")
    start_at = text.index("正在启动服务（公网隧道模式）")
    assert write_at < start_at
    assert "Remove-Item -LiteralPath $UrlFile" in text
