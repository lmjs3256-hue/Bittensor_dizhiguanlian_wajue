"""主机「远程访问 / 公网隧道」的页面开关（0.8.48 起不再重启服务）。

0.8.45–0.8.47 反复踩同一个死结：按钮要切换模式就得重启服务，而启动器是服务
自己 spawn 的子进程，服务一退出/被杀，启动器就会被连带清理，状态永远停在
launching。这里改成**运行时切换**：

* ``WALLET_LINK_REMOTE`` / ``WALLET_LINK_TRUST_PROXY`` 都是运行时读的环境变量，
  直接改就能开启口令校验与隧道来源判定，不需要重启 uvicorn；
* 公网入口只是额外拉起一个 cloudflared 进程、把分配到的网址写进
  ``logs/cloudflared.url``，服务本身不动；
* 监听地址仍是启动时决定的。服务若绑在回环上，按钮会明确提示先用
  「启动-远程访问.cmd」启动一次（一次性），而不是偷偷尝试重启。

状态写在 ``logs/remote-launch.json``，前端轮询 ``/api/access/status`` 读取。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "logs"
STATE_FILE = LOG_DIR / "remote-launch.json"
URL_FILE = LOG_DIR / "cloudflared.url"
CF_LOG = LOG_DIR / "cloudflared.log"
CF_OUT = LOG_DIR / "cloudflared.stdout.log"
CLOUDFLARED = ROOT_DIR / "tools" / "cloudflared.exe"
BIND_HOST_ENV = "WALLET_LINK_BIND_HOST"
VALID_MODES = frozenset({"remote", "tunnel"})
_LOCK = threading.Lock()
LAUNCH_STALE_SECONDS = 90
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_unlocked() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {
        "state": "idle",
        "mode": "",
        "message": "",
        "url": "",
        "updated_at": "",
    }


def _write_unlocked(data: dict[str, Any]) -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def read_state() -> dict[str, Any]:
    """读最近一次状态；文件坏了按 idle 处理，不让页面卡住。

    超过 ``LAUNCH_STALE_SECONDS`` 仍停在 launching 就自动判 error，
    页面一定能结束等待。
    """

    with _LOCK:
        data = _read_unlocked()
        if data.get("state") != "launching":
            return data
        started = str(data.get("started_at") or "")
        try:
            moment = datetime.fromisoformat(started.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - moment).total_seconds()
        except ValueError:
            age = 0.0
        if age > LAUNCH_STALE_SECONDS:
            data["state"] = "error"
            data["message"] = (
                "操作超过 90 秒没有完成。请查看 logs/cloudflared.log，"
                "或双击「启动-公网隧道.cmd」；服务本身没有中断，刷新页面即可继续使用。"
            )
            data["finished_at"] = _now()
            data["updated_at"] = _now()
            _write_unlocked(data)
        return data


def write_state(**fields: Any) -> dict[str, Any]:
    """合并写入状态字段，并自动带上更新时间。"""

    with _LOCK:
        data = _read_unlocked()
        data.update(fields)
        data["updated_at"] = _now()
        return _write_unlocked(data)


def _wait_tunnel_url(process: subprocess.Popen, timeout: int = 60) -> str:
    pattern = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            match = pattern.search(CF_LOG.read_text(encoding="utf-8", errors="ignore"))
            if match:
                return match.group(0)
        except OSError:
            pass
        if process.poll() is not None:
            return ""
        time.sleep(0.4)
    return ""


def _start_tunnel(port: int) -> str:
    if not CLOUDFLARED.exists():
        raise RuntimeError(
            "找不到 tools/cloudflared.exe，请先双击一次「启动-公网隧道.cmd」下载。"
        )
    # 只清空、不删除：cloudflared 还占着日志文件句柄，删文件会被
    # 环境的 safe-delete 钩子拦下；下面 open("wb") 本来就会截断日志。
    URL_FILE.write_text("", encoding="utf-8")
    try:
        subprocess.run(
            ["taskkill", "/IM", "cloudflared.exe", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001 - 清理是尽力而为
        pass
    time.sleep(1)
    with CF_OUT.open("wb") as out, CF_LOG.open("wb") as err:
        process = subprocess.Popen(
            [
                str(CLOUDFLARED),
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
            ],
            cwd=str(ROOT_DIR),
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
        )
    url = _wait_tunnel_url(process)
    if not url:
        raise RuntimeError(
            f"隧道没有在 60 秒内分配到网址，详见 {CF_LOG}"
            "（常见于出站 UDP 7844 被拦）。"
        )
    URL_FILE.write_text(url, encoding="utf-8")
    return url


def _finish_launch(mode: str, port: int) -> None:
    """后台线程：公网模式拉起隧道，远程模式直接完成。服务全程不重启。"""

    try:
        url = ""
        if mode == "tunnel":
            os.environ["WALLET_LINK_TRUST_PROXY"] = "1"
            url = _start_tunnel(port)
            message = f"公网隧道已就绪：{url}"
        else:
            message = "远程访问已开启"
        write_state(state="ready", message=message, url=url, finished_at=_now())
    except Exception as exc:  # noqa: BLE001 - 任何失败都要写回状态
        write_state(state="error", message=str(exc), finished_at=_now())


def start_launch(mode: str, port: int = 8766) -> dict[str, Any]:
    """运行时开启远程访问 / 公网隧道；不重启服务、不 spawn 启动器。"""

    if mode not in VALID_MODES:
        raise ValueError("模式只能是 remote 或 tunnel")
    if read_state().get("state") == "launching":
        raise RuntimeError("已有一次启动正在进行，请等它完成")

    bind_host = os.getenv(BIND_HOST_ENV, "").strip()
    if bind_host in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            "当前服务只监听本机，外部设备连不上。请先用「启动-远程访问.cmd」"
            "或「启动-公网隧道.cmd」启动一次服务；之后本按钮可秒级切换，不再重启。"
        )

    # remote_enabled() / trust_proxy() 都直接读环境变量，改完立即生效。
    os.environ["WALLET_LINK_REMOTE"] = "1"
    write_state(
        state="launching",
        mode=mode,
        message="正在建立公网隧道…" if mode == "tunnel" else "正在开启远程访问…",
        url="",
        started_at=_now(),
        finished_at="",
    )
    worker = threading.Thread(target=_finish_launch, args=(mode, port), daemon=True)
    worker.start()
    return {
        "ok": True,
        "mode": mode,
        "message": "正在准备公网入口…" if mode == "tunnel" else "远程访问已开启",
    }
