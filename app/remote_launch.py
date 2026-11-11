"""主机「远程访问 / 公网隧道」的页面启动器（0.8.45）。

页面上的两个按钮走这里。服务自己改不了 uvicorn 的监听地址（host 在启动时
定），所以切换模式必须重启：本模块 spawn 一个独立的 PowerShell 启动器，让它
停掉当前服务、按目标模式重启，公网模式还会先把 cloudflared 的网址拿到再启
服务。启动器开头先睡几秒，等这次 HTTP 响应发完再动手。

状态写在 ``logs/remote-launch.json``，前端轮询 ``/api/access/status`` 读取。
PowerShell 启动器是独立进程，即使服务重启期间接口不可用，状态文件也能在
新服务起来后被读到。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "logs"
STATE_FILE = LOG_DIR / "remote-launch.json"
LAUNCH_SCRIPT = ROOT_DIR / "launch_mode.ps1"
VALID_MODES = frozenset({"remote", "tunnel"})
_LOCK = threading.Lock()


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


def read_state() -> dict[str, Any]:
    """读最近一次启动状态；文件坏了按 idle 处理，不让页面卡住。"""

    with _LOCK:
        return _read_unlocked()


def write_state(**fields: Any) -> dict[str, Any]:
    """合并写入状态字段，并自动带上更新时间。"""

    with _LOCK:
        data = _read_unlocked()
        data.update(fields)
        data["updated_at"] = _now()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return data


def start_launch(mode: str) -> dict[str, Any]:
    """触发一次模式切换；真正的重启由独立进程完成，这里立刻返回。"""

    if mode not in VALID_MODES:
        raise ValueError("模式只能是 remote 或 tunnel")
    if not LAUNCH_SCRIPT.exists():
        raise RuntimeError(f"找不到启动器脚本：{LAUNCH_SCRIPT.name}")
    current = read_state()
    if current.get("state") == "launching":
        raise RuntimeError("已有一次启动正在进行，请等它完成")

    write_state(
        state="launching",
        mode=mode,
        message="正在切换访问模式，页面会短暂断开…",
        url="",
        started_at=_now(),
        finished_at="",
    )

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LAUNCH_SCRIPT),
        "-Mode",
        mode,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    try:
        subprocess.Popen(
            command,
            cwd=str(ROOT_DIR),
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        write_state(
            state="error",
            message=f"启动器没能启动：{exc}",
            finished_at=_now(),
        )
        raise RuntimeError(f"启动器没能启动：{exc}") from exc

    return {"ok": True, "mode": mode, "message": "已触发，服务将短暂重启"}
