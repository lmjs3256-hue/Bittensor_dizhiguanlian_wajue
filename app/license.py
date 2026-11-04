"""离线授权：机器码 + Ed25519 签名的授权码。

为什么这样做
------------
工具是离线部署的，整目录可以被拷贝，所以“防泄漏”不能靠联网校验。可行的做法是
把授权和**这台电脑**绑定：

* 每台电脑有一个稳定的机器码（Windows 用注册表里的 MachineGuid 派生）。
* 授权码 = 团队名 + 允许的机器码 + 有效期，用私钥签名；程序里只放**公钥**。
  别人拿到安装包也改不出有效授权（Ed25519 没有私钥签不出来）。
* 没有授权时服务照常启动，但只显示激活页并给出本机机器码，所有业务接口返回 403。
  这样新电脑装好后把机器码报给管理员，拿到授权码粘贴即可。

局限（务必让使用者知道）
------------------------
离线软件不可能做到绝对不可破解：能改 Python 源码的人总能绕过校验。这套机制的真实
目标是——挡住“把文件夹拷给别人就能用”这种最常见的泄漏，并让留出的副本可追溯
（授权码里带团队名，页面与导出文件都会打上水印）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ROOT_DIR


# 公钥对应 tools/issue_license.py 使用的私钥。私钥不进仓库、不进安装包，
# 只由管理员单独保管；换了密钥对就要给所有人重新发授权码。
PUBLIC_KEY_B64 = "2dV1sB7tFtGdvQ9Uox5Q8yt/cZhbgdFzzMUp5aosNoU="
TOKEN_PREFIX = "WL1"
LICENSE_FILENAME = "license.key"
CLOCK_FILENAME = ".license-clock"
MAX_CLOCK_ROLLBACK = timedelta(hours=24)


class LicenseError(Exception):
    """授权文件缺失、格式错误、机器码不匹配或已过期。"""


@dataclass(frozen=True)
class License:
    team: str
    machines: tuple[str, ...]
    expires: date | None
    issued: str = ""
    note: str = ""
    token: str = field(default="", compare=False)

    def covers(self, code: str) -> bool:
        if "*" in self.machines:
            return True
        normalized = normalize_machine(code)
        return any(normalize_machine(item) == normalized for item in self.machines)

    @property
    def days_left(self) -> int | None:
        if self.expires is None:
            return None
        return (self.expires - date.today()).days


# -- 机器码 ---------------------------------------------------------------


def _windows_machine_guid() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip()
    except Exception:  # noqa: BLE001 - 非 Windows 或权限受限时走兜底
        return ""


def _fallback_fingerprint() -> str:
    import uuid

    parts = [platform.node(), f"{uuid.getnode():012x}", platform.machine()]
    return "|".join(parts)


def raw_machine_secret() -> str:
    """取本机稳定标识。Windows 的 MachineGuid 重装系统才会变。"""

    guid = _windows_machine_guid() if sys.platform == "win32" else ""
    if guid:
        return f"win:{guid.lower()}"
    return f"gen:{_fallback_fingerprint()}"


def machine_code(secret: str | None = None) -> str:
    """把本机标识派生成给人抄的机器码，形如 ABCD-EFGH-JKLM-NPQR。"""

    digest = hashlib.sha256((secret or raw_machine_secret()).encode("utf-8")).digest()
    text = base64.b32encode(digest).decode("ascii").rstrip("=")[:16]
    return "-".join(text[index : index + 4] for index in range(0, 16, 4))


def normalize_machine(value: str) -> str:
    return "".join(char for char in str(value).upper() if char.isalnum())


# -- 授权码解析 -----------------------------------------------------------


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _public_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))


def verify_token(token: str, *, check_machine: bool = True) -> License:
    cleaned = "".join(str(token).split())
    parts = cleaned.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise LicenseError("授权码格式不对，应当是 WL1.xxx.yyy 这样三段")

    body, signature = parts[1], parts[2]
    try:
        raw_signature = _b64decode(signature)
    except Exception as exc:  # noqa: BLE001
        raise LicenseError("授权码签名部分无法解码") from exc

    from cryptography.exceptions import InvalidSignature

    try:
        _public_key().verify(raw_signature, f"{TOKEN_PREFIX}.{body}".encode("ascii"))
    except InvalidSignature as exc:
        raise LicenseError("授权码签名校验失败：不是本工具签发的授权") from exc
    except Exception as exc:  # noqa: BLE001
        raise LicenseError(f"授权码校验失败：{exc}") from exc

    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LicenseError("授权码内容无法解析") from exc

    if int(payload.get("v", 0)) != 1:
        raise LicenseError("授权码版本不支持，请向管理员索取新版授权")

    machines = payload.get("machines") or []
    if isinstance(machines, str):
        machines = [machines]
    machines = tuple(str(item) for item in machines if str(item).strip())
    if not machines:
        raise LicenseError("授权码没有绑定任何机器")

    expires_text = str(payload.get("expires") or "").strip()
    expires: date | None = None
    if expires_text:
        try:
            expires = date.fromisoformat(expires_text)
        except ValueError as exc:
            raise LicenseError("授权码的有效期格式不对") from exc

    license_row = License(
        team=str(payload.get("team") or "").strip() or "未命名团队",
        machines=machines,
        expires=expires,
        issued=str(payload.get("issued") or ""),
        note=str(payload.get("note") or ""),
        token=cleaned,
    )

    if check_machine and not license_row.covers(machine_code()):
        raise LicenseError(
            "授权码不是给这台电脑签发的（机器码不匹配），请把本机机器码发给管理员重新申请"
        )
    if expires is not None and expires < date.today():
        raise LicenseError(f"授权已于 {expires.isoformat()} 到期，请联系管理员续期")
    return license_row


# -- 授权文件读写 ---------------------------------------------------------


def license_path() -> Path:
    override = os.getenv("WALLET_LINK_LICENSE")
    if override:
        return Path(override)
    return ROOT_DIR / LICENSE_FILENAME


def _clock_path() -> Path:
    return ROOT_DIR / "data" / CLOCK_FILENAME


def _check_clock() -> None:
    """挡住“把系统时间调回授权期内”这种绕过方式。"""

    path = _clock_path()
    now = datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001 - 首次运行或文件损坏都直接重建
        last = None
    if last is not None and last - now > MAX_CLOCK_ROLLBACK:
        raise LicenseError("检测到系统时间被调早，请先把系统时间校准到这个时间之后再用")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        pass


def read_token_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LicenseError(f"读取授权文件失败：{exc}") from exc
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{TOKEN_PREFIX}."):
            return line
    raise LicenseError("授权文件里没有找到授权码")


_cache: dict[str, Any] = {"stamp": None, "license": None, "error": None}


def _stamp(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def load(*, refresh: bool = False) -> License | None:
    """返回当前授权；没有授权返回 None，授权有问题抛 LicenseError。"""

    path = license_path()
    stamp = _stamp(path)
    # 注意 _cache 初始 stamp 也是 None，必须排除“还没读过”的状态，
    # 否则“文件不存在”会被误当成缓存命中返回 None。
    if not refresh and stamp is not None and _cache["stamp"] == stamp:
        if _cache["error"]:
            raise LicenseError(_cache["error"])
        return _cache["license"]

    try:
        if stamp is None:
            raise LicenseError("还没有激活：这台电脑上没有 license.key")
        row = verify_token(read_token_file(path))
        _check_clock()
    except LicenseError as exc:
        _cache.update({"stamp": stamp, "license": None, "error": str(exc)})
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都按未授权处理，避免放行
        message = f"授权校验异常：{exc}"
        _cache.update({"stamp": stamp, "license": None, "error": message})
        raise LicenseError(message) from exc

    _cache.update({"stamp": stamp, "license": row, "error": None})
    return row


def save_token(token: str) -> License:
    """把授权码写入 license.key（激活用），返回校验通过的授权。"""

    row = verify_token(token)
    path = license_path()
    header = [
        "# Wallet Link 授权文件，请勿修改。删除后工具将无法使用。",
        f"# 团队：{row.team}",
        f"# 到期：{row.expires.isoformat() if row.expires else '长期有效'}",
    ]
    path.write_text("\n".join(header + [row.token, ""]), encoding="utf-8")
    _cache.update({"stamp": _stamp(path), "license": row, "error": None})
    return row


def status() -> dict[str, Any]:
    """给前端和 /api/health 用的授权状态。"""

    code = machine_code()
    try:
        row = load()
    except LicenseError as exc:
        return {
            "licensed": False,
            "team": "",
            "machine": code,
            "expires": None,
            "days_left": None,
            "reason": str(exc),
            "watermark": "",
        }
    if row is None:  # 防御：理论上 load() 要么返回授权要么抛错
        raise LicenseError("授权状态异常，请重新激活")
    return {
        "licensed": True,
        "team": row.team,
        "machine": code,
        "expires": row.expires.isoformat() if row.expires else None,
        "days_left": row.days_left,
        "reason": "",
        "watermark": f"{row.team} · {normalize_machine(code)[-6:]}",
    }


def is_licensed() -> bool:
    try:
        return load() is not None
    except LicenseError:
        return False


def watermark() -> str:
    """导出文件与页面上的溯源标识；未授权时为空。"""

    try:
        row = load()
    except LicenseError:
        return ""
    if row is None:
        return ""
    return f"{row.team} · {normalize_machine(machine_code())[-6:]}"


if __name__ == "__main__":  # python -m app.license 直接打印本机机器码
    print("本机机器码（发给管理员换授权码）：")
    print(machine_code())
