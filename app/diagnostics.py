"""运行环境自检：一屏看清「代码版本 / 预置名单文件 / 落库情况 / 网络凭据」。

远端踩到过的问题都是同一类：代码已经拉下来了，但跑着的还是旧进程；或者
``app/data/builtin_validators.txt`` 没跟着更新，预置验证者读不到。这两种情况
在原来的界面上表现几乎一样（「地址库里没有预置地址」「同步还是 405」），
只能靠猜，于是把判断依据直接算出来：

- 版本号读 ``VERSION`` 文件，pid / root 用来区分「同一个服务」还是「另一个副本」；
- 预置名单直接读文件本身（是否存在、条数、修改时间）；
- 落库情况查 ``address_library``（内置来源条数、验证者条数）；
- 每条检查给出 pass / warn / fail，以及对应的修复动作。

预置名单和落库数量要扫上千行，界面上的「环境自检」会反复调用，因此按数据库
文件路径做一次短 TTL 缓存；命令行 ``python run.py --doctor`` 永远读实时数据。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .builtin_addresses import (
    VALIDATOR_SNAPSHOT_LABEL,
    VALIDATOR_SNAPSHOT_PATH,
    load_validator_snapshot,
)

# 预置名单条数低于这个数就说明文件是残缺的（正常快照 1000+ 条）。
SNAPSHOT_MIN_ROWS = 1000

# 进程启动时间：模块被导入的时刻≈服务启动时刻，用于和 pid 一起判断"是不是新进程"。
STARTED_AT = datetime.now(timezone.utc).astimezone()

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def snapshot_status(path: Path | None = None) -> dict[str, Any]:
    """预置验证者名单文件的状态：路径、是否存在、条数、修改时间。"""

    target = path or VALIDATOR_SNAPSHOT_PATH
    exists = target.is_file()
    count = len(load_validator_snapshot(target)) if exists else 0
    mtime = None
    if exists:
        try:
            mtime = datetime.fromtimestamp(target.stat().st_mtime).astimezone().isoformat(
                timespec="seconds"
            )
        except OSError:  # pragma: no cover - 极端文件系统异常
            mtime = None
    return {
        "path": str(target),
        "exists": exists,
        "count": count,
        "mtime": mtime,
    }


def library_status(database, *, ttl: float = 0.0) -> dict[str, Any]:
    """地址库里内置名单的落库情况（内置来源 / 验证者 / 预置标签）。"""

    from .db import AddressLibrary

    key = str(getattr(database, "path", "") or getattr(database, "url", ""))
    now = time.monotonic()
    if ttl > 0:
        cached = _cache.get(key)
        if cached and now - cached[0] < ttl:
            return dict(cached[1])

    payload: dict[str, Any] = {
        "total": 0,
        "builtin": 0,
        "builtin_validators": 0,
        "preset": 0,
        "validators": 0,
        "error": None,
    }
    try:
        with database.session() as db:
            payload["total"] = db.scalar(select(func.count()).select_from(AddressLibrary)) or 0
            payload["builtin"] = (
                db.scalar(
                    select(func.count())
                    .select_from(AddressLibrary)
                    .where(AddressLibrary.source.like("builtin:%"))
                )
                or 0
            )
            payload["builtin_validators"] = (
                db.scalar(
                    select(func.count())
                    .select_from(AddressLibrary)
                    .where(AddressLibrary.source == "builtin:validator")
                )
                or 0
            )
            payload["preset"] = (
                db.scalar(
                    select(func.count())
                    .select_from(AddressLibrary)
                    .where(AddressLibrary.label == VALIDATOR_SNAPSHOT_LABEL)
                )
                or 0
            )
            payload["validators"] = (
                db.scalar(
                    select(func.count())
                    .select_from(AddressLibrary)
                    .where(AddressLibrary.category == "VALIDATOR")
                )
                or 0
            )
    except Exception as exc:  # noqa: BLE001 - 库还没建表时不要炸掉自检
        payload["error"] = str(exc)

    if ttl > 0:
        _cache[key] = (now, dict(payload))
    return payload


def clear_cache() -> None:
    _cache.clear()


def _check(
    ident: str,
    title: str,
    status: str,
    detail: str,
    hint: str = "",
) -> dict[str, Any]:
    return {"id": ident, "title": title, "status": status, "detail": detail, "hint": hint}


def build_report(
    database,
    *,
    version: str,
    root: str | Path,
    running_version: str | None = None,
    db_path: str | Path | None = None,
    snapshot_path: Path | None = None,
    taostats_configured: bool | None = None,
    startup_builtin: dict[str, Any] | None = None,
    cache_ttl: float = 0.0,
    outbound_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """汇总一次自检结果：给界面和命令行共用。

    ``version`` 是磁盘上 VERSION 文件的版本，``running_version`` 是当前进程启动时
    的版本。两者不一致就意味着「代码更新了但服务没重启」，这是远端最常见、
    也最容易误判的一种情况。
    """

    snapshot = snapshot_status(snapshot_path)
    library = library_status(database, ttl=cache_ttl)

    checks: list[dict[str, Any]] = []
    running = str(running_version or version).strip()
    disk = str(version).strip()
    if running != disk:
        checks.append(
            _check(
                "version",
                "代码版本",
                "fail",
                f"磁盘上的 VERSION 是 {disk}，但这个进程跑的是 {running}"
                f"（pid {os.getpid()}，启动于 {STARTED_AT.strftime('%Y-%m-%d %H:%M:%S')}）",
                "代码已经更新，服务还是旧进程。双击「停止.cmd」关掉，再双击"
                "「启动.cmd」重新启动，新功能（含预置验证者名单）才会生效。",
            )
        )
    else:
        checks.append(
            _check(
                "version",
                "代码版本",
                "pass",
                f"VERSION 文件与运行进程都是 {disk}，pid {os.getpid()}，启动于 "
                f"{STARTED_AT.strftime('%Y-%m-%d %H:%M:%S')}",
                "刷新页面不改变运行的代码：升级后一定要停止再启动。",
            )
        )

    if snapshot["exists"] and snapshot["count"] >= SNAPSHOT_MIN_ROWS:
        checks.append(
            _check(
                "snapshot_file",
                "预置验证者文件",
                "pass",
                f"{snapshot['count']} 条，修改时间 {snapshot['mtime']}（{snapshot['path']}）",
            )
        )
    elif snapshot["exists"]:
        checks.append(
            _check(
                "snapshot_file",
                "预置验证者文件",
                "warn",
                f"只有 {snapshot['count']} 条，正常快照应有 {SNAPSHOT_MIN_ROWS}+ 条",
                "文件可能被截断：重新 git pull，或重新解压完整外发包。",
            )
        )
    else:
        checks.append(
            _check(
                "snapshot_file",
                "预置验证者文件",
                "fail",
                f"找不到 {snapshot['path']}",
                "app/data/builtin_validators.txt 缺失：重新 git pull 整个仓库，"
                "不要只拷贝 app/*.py；或用完整外发包覆盖升级。",
            )
        )

    if library["error"]:
        checks.append(
            _check(
                "library",
                "地址库",
                "fail",
                f"读取失败：{library['error']}",
                "数据库文件损坏或被占用：关闭服务后重试；仍失败则从备份恢复。",
            )
        )
    else:
        expected = snapshot["count"]
        # 判断「预置名单进没进库」要看验证者分类本身，不能只看 source：
        # 先同步过 Taostats 名单的机器，地址来源是 taostats:，预置写入按规则
        # 保留官方来源不覆盖，只补分类和标签，此时 builtin 计数会很小。
        covered = max(library["preset"], library["validators"])
        if library["total"] == 0:
            checks.append(
                _check(
                    "builtin_rows",
                    "预置名单落库",
                    "fail",
                    f"地址库是空的（0 条）",
                    "内置名单只在服务启动时写入：确认看到版本号更新后，"
                    "用「停止.cmd」关掉旧进程，再「启动.cmd」重新启动一次。",
                )
            )
        elif expected and covered < expected * 0.9:
            checks.append(
                _check(
                    "builtin_rows",
                    "预置名单落库",
                    "fail",
                    f"文件有 {expected} 条，库里验证者只有 {library['validators']} 条"
                    f"（带「预置验证者」标签 {library['preset']} 条）",
                    "库里没有预置名单：说明写入没执行。确认 pid/版本是刚启动的新进程；"
                    "若刚 pull 到新版本，停止再启动一次服务即可补进 1194 条。",
                )
            )
        else:
            detail = (
                f"验证者分类 {library['validators']} 条（查到即终止关联），其中带"
                f"「预置验证者」标签 {library['preset']} 条；内置来源合计 {library['builtin']} 条"
            )
            if library["builtin"] < library["preset"]:
                detail += "（已验证者曾从 Taostats 同步，保留了官方来源标记，属正常）"
            checks.append(_check("builtin_rows", "预置名单落库", "pass", detail))

    if startup_builtin is not None:
        checks.append(
            _check(
                "startup_write",
                "本次启动写入",
                "pass",
                f"新增 {startup_builtin.get('inserted', 0)} 条，补标记 "
                f"{startup_builtin.get('updated', 0)} 条，格式无效跳过 "
                f"{startup_builtin.get('skipped', 0)} 条",
                "新增 0 条说明库里已经齐了，不需要反复重启。",
            )
        )

    if taostats_configured is None:
        checks.append(
            _check(
                "taostats_key",
                "Taostats API key",
                "warn",
                "未检测",
                "",
            )
        )
    elif taostats_configured:
        checks.append(
            _check(
                "taostats_key",
                "Taostats API key",
                "pass",
                "已配置，可同步交易所/验证者名单、拉取历史转账",
            )
        )
    else:
        checks.append(
            _check(
                "taostats_key",
                "Taostats API key",
                "warn",
                "未配置：联网同步验证者会失败，但查询本地索引、使用预置验证者名单不受影响",
                "需要同步官方名单或新地址时再填 key；只想用已有数据可以直接查询。",
            )
        )

    if outbound_probe is not None:
        # 出站可达性：直连 taostats.io 不通、系统代理也不通时，查询会退回本地
        # 索引。0.8.16 之前这种故障会被页面误报成『没有可用的 API key』。
        active = outbound_probe.get("active") or {}
        results = outbound_probe.get("results") or []
        working = [item for item in results if item.get("ok")]
        current_ok = any(
            item.get("ok") for item in results if item.get("proxy") == active.get("url")
        )
        if current_ok:
            target = active.get("url") or "直连"
            checks.append(
                _check(
                    "outbound",
                    "出站网络（taostats.io）",
                    "pass",
                    f"当前走{active.get('source') or ''}（{target}）可达",
                )
            )
        elif working:
            fallback = working[0]
            checks.append(
                _check(
                    "outbound",
                    "出站网络（taostats.io）",
                    "warn",
                    f"当前路径（{active.get('source') or ''}）不通，但 "
                    f"{fallback.get('source') or ''}"
                    f"（{fallback.get('proxy') or '直连'}）可用，重试后会自动切换",
                    "刚发生过一次失败切换属正常；持续出现说明本机代理软件在反复掉线。",
                )
            )
        else:
            paths = "、".join(
                f"{item.get('source')}（{item.get('proxy') or '直连'}）：{item.get('error') or item.get('status_code')}"
                for item in results
            )
            checks.append(
                _check(
                    "outbound",
                    "出站网络（taostats.io）",
                    "fail",
                    f"所有出站路径都不通——{paths}",
                    "检查本机代理软件（Clash 等）是否在运行、系统代理是否开启；"
                    "公司网络可能直接拦掉了 taostats.io。此时查询只能用本地索引。",
                )
            )

    failed = [item for item in checks if item["status"] == "fail"]
    warned = [item for item in checks if item["status"] == "warn"]
    return {
        "ok": not failed,
        "status": "fail" if failed else ("warn" if warned else "pass"),
        "version": disk,
        "running_version": running,
        "stale": running != disk,
        "pid": os.getpid(),
        "root": str(root),
        "db_path": str(db_path) if db_path is not None else None,
        "started_at": STARTED_AT.isoformat(timespec="seconds"),
        "snapshot": snapshot,
        "library": library,
        "checks": checks,
        "failed": len(failed),
        "warned": len(warned),
    }


def format_report(report: dict[str, Any]) -> str:
    """命令行自检的可读输出（纯 ASCII 边框，Windows 控制台不会乱码）。"""

    mark = {"pass": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    lines = [
        "Bittensor 地址关联挖掘 · 环境自检",
        "-" * 78,
        f"版本      : {report['version']}",
        f"进程      : pid {report['pid']}，启动于 {report['started_at']}",
        f"项目目录  : {report['root']}",
        f"数据库    : {report['db_path'] or '未提供'}",
        "-" * 78,
    ]
    for item in report["checks"]:
        lines.append(f"{mark.get(item['status'], '[ ?  ]')} {item['title']}")
        lines.append(f"         {item['detail']}")
        if item["hint"] and item["status"] != "pass":
            lines.append(f"         处理建议：{item['hint']}")
    lines.append("-" * 78)
    if report["ok"]:
        lines.append("结论：环境正常，预置验证者名单可用。")
    else:
        lines.append(f"结论：有 {report['failed']} 项检查未通过，按上面的处理建议操作后重新自检。")
    if report["warned"]:
        lines.append(f"      另有 {report['warned']} 项提醒（不影响离线使用）。")
    return "\n".join(lines)
