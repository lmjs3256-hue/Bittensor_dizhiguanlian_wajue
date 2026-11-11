"""0.8.45：远程访问启动按钮 + 公网隧道网址获取顺序的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import remote_launch

ROOT = Path(__file__).resolve().parent.parent
REMOTE_CLIENT = ("203.0.113.9", 54123)
LOCAL_CLIENT = ("127.0.0.1", 54123)


@pytest.fixture()
def isolated_launch(tmp_path, monkeypatch):
    """把状态文件和启动器指到临时目录，测试绝不触发真实重启。"""

    state = tmp_path / "remote-launch.json"
    script = tmp_path / "launch_mode.ps1"
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(remote_launch, "STATE_FILE", state)
    monkeypatch.setattr(remote_launch, "LOG_DIR", tmp_path)
    monkeypatch.setattr(remote_launch, "LAUNCH_SCRIPT", script)
    return state


@pytest.fixture()
def fake_popen(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))

    monkeypatch.setattr(remote_launch.subprocess, "Popen", FakePopen)
    return calls


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


# -- 启动器模块 ------------------------------------------------------------


def test_start_launch_rejects_unknown_mode(isolated_launch):
    with pytest.raises(ValueError):
        remote_launch.start_launch("anything")


def test_start_launch_spawns_detached_launcher(isolated_launch, fake_popen):
    result = remote_launch.start_launch("tunnel")
    assert result == {
        "ok": True,
        "mode": "tunnel",
        "message": "已触发，服务将短暂重启",
    }
    command, kwargs = fake_popen[0]
    assert command[0] == "powershell.exe"
    assert command[-2:] == ["-Mode", "tunnel"]
    assert kwargs["close_fds"] is True
    state = json.loads(isolated_launch.read_text(encoding="utf-8"))
    assert state["state"] == "launching"
    assert state["mode"] == "tunnel"
    assert state["url"] == ""


def test_second_launch_while_busy_is_rejected(isolated_launch, fake_popen):
    remote_launch.start_launch("remote")
    with pytest.raises(RuntimeError, match="已有一次启动"):
        remote_launch.start_launch("tunnel")
    assert len(fake_popen) == 1


def test_missing_launcher_script_is_reported(isolated_launch, tmp_path, monkeypatch, fake_popen):
    monkeypatch.setattr(remote_launch, "LAUNCH_SCRIPT", tmp_path / "nope.ps1")
    with pytest.raises(RuntimeError, match="找不到启动器脚本"):
        remote_launch.start_launch("remote")
    assert remote_launch.read_state()["state"] == "idle"
    assert fake_popen == []


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
    assert data["updated_at"]


# -- HTTP 接口 -------------------------------------------------------------


def test_launch_endpoint_is_local_only(isolated_launch, fake_popen, licensed):
    with _client(remote_host=True) as client:
        response = client.post("/api/access/launch", json={"mode": "remote"})
    assert response.status_code == 403
    assert fake_popen == []


def test_launch_endpoint_rejects_unknown_mode(isolated_launch, fake_popen, licensed):
    with _client() as client:
        response = client.post("/api/access/launch", json={"mode": "bogus"})
    assert response.status_code == 422
    assert fake_popen == []


def test_launch_endpoint_accepts_valid_mode(isolated_launch, fake_popen, licensed):
    with _client() as client:
        response = client.post("/api/access/launch", json={"mode": "tunnel"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(fake_popen) == 1


def test_access_status_exposes_launch_state(isolated_launch, licensed):
    remote_launch.write_state(
        state="ready", mode="tunnel", message="公网隧道已就绪", url="https://x.trycloudflare.com"
    )
    with _client() as client:
        payload = client.get("/api/access/status").json()
    assert payload["launch"]["state"] == "ready"
    assert payload["launch"]["url"] == "https://x.trycloudflare.com"


# -- 前端与脚本契约 ---------------------------------------------------------


def test_remote_panel_has_both_launch_buttons():
    html = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/styles.css").read_text(encoding="utf-8")
    assert 'id="launch-remote"' in html
    assert 'id="launch-tunnel"' in html
    assert "/api/access/launch" in js
    assert "renderLaunchStatus" in js
    assert ".remote-actions" in css


def test_tunnel_script_writes_url_before_starting_service():
    """老顺序先启服务、后建隧道，服务启动时永远读不到新网址。"""

    text = (ROOT / "tunnel.ps1").read_text(encoding="utf-8-sig")
    write_at = text.index("Set-Content -LiteralPath $UrlFile")
    start_at = text.index("正在启动服务（公网隧道模式）")
    assert write_at < start_at
    assert "Remove-Item -LiteralPath $UrlFile" in text


def test_launch_mode_script_exists_and_has_bom():
    raw = (ROOT / "launch_mode.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "PS 5.1 需要 UTF-8 BOM，否则中文乱码"
    text = raw.decode("utf-8-sig")
    assert "Set-LaunchState" in text
    assert "Wait-TunnelUrl" in text
    assert "Start-Sleep -Seconds 3" in text
