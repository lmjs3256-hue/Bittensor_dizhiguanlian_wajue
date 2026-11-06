"""本机设置持久化（目前只存 Taostats API key）。

key 只落在这台电脑上，不上传、不回显：

- Windows 下优先用 DPAPI 加密（绑定当前 Windows 用户，拷到别的电脑解不开）；
- 加密不可用时退化成明文并打标记，保证离线环境仍能保存；
- 文件放在 ``data/settings.json``，可用环境变量 ``WALLET_LINK_SETTINGS`` 指定。

之所以要落盘：这个 key 以前只放在进程内存里，服务一重启（或换一份副本）
就丢了，页面不会拦，后端会静默退回本地索引，最后得到 0 个关联地址——
用户只会看到一张空表，很难判断是"没数据"还是"没配置"。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import DEFAULT_DATA_DIR


FILENAME = "settings.json"
_SECRET_FIELD = "taostats_api_key"
_DPAPI_ENTROPY = b"wallet-link-taostats-key"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def settings_path() -> Path:
    override = os.getenv("WALLET_LINK_SETTINGS")
    if override:
        return Path(override)
    return DEFAULT_DATA_DIR / FILENAME


# -- Windows DPAPI -------------------------------------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _make_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi_protect(data: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_in, _buffer = _make_blob(data)
        entropy, _entropy_buffer = _make_blob(_DPAPI_ENTROPY)
        blob_out = _DataBlob()
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            "wallet-link",
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:  # noqa: BLE001 - 加密失败就降级为明文，不影响主流程
        return None


def _dpapi_unprotect(data: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_in, _buffer = _make_blob(data)
        entropy, _entropy_buffer = _make_blob(_DPAPI_ENTROPY)
        blob_out = _DataBlob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:  # noqa: BLE001
        return None


# -- 读写 ---------------------------------------------------------------
def _read() -> dict[str, Any]:
    path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> bool:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".settings-", dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False


def save_api_key(api_key: str) -> str:
    """保存 key，返回存储方式：``dpapi`` / ``plain`` / ``none``（写失败）。"""

    key = api_key.strip()
    if not key:
        return "none"
    protected = _dpapi_protect(key.encode("utf-8"))
    if protected is not None:
        record = {"enc": "dpapi", "data": base64.b64encode(protected).decode("ascii")}
    else:
        record = {"enc": "plain", "data": base64.b64encode(key.encode("utf-8")).decode("ascii")}
    data = _read()
    data["version"] = 1
    data[_SECRET_FIELD] = record
    if not _write(data):
        return "none"
    return record["enc"]


def load_api_key() -> str | None:
    record = _read().get(_SECRET_FIELD)
    if not isinstance(record, dict):
        return None
    raw = record.get("data")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        blob = base64.b64decode(raw.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if record.get("enc") == "dpapi":
        plain = _dpapi_unprotect(blob)
        if plain is None:
            return None
        try:
            return plain.decode("utf-8") or None
        except UnicodeDecodeError:
            return None
    try:
        return blob.decode("utf-8") or None
    except UnicodeDecodeError:
        return None


def storage_kind() -> str:
    """当前磁盘上的保存方式：``dpapi`` / ``plain`` / ``""``（未保存）。"""

    record = _read().get(_SECRET_FIELD)
    if not isinstance(record, dict):
        return ""
    value = record.get("enc")
    return value if value in {"dpapi", "plain"} else ""


def clear_api_key() -> None:
    data = _read()
    if _SECRET_FIELD not in data:
        return
    data.pop(_SECRET_FIELD, None)
    _write(data)


# -- 官方交易所名单同步状态 ------------------------------------------------
_EXCHANGE_SYNC_FIELD = "exchange_sync"


def load_exchange_sync_state() -> dict[str, Any]:
    """读取官方交易所名单的最近同步状态（时间、新增地址、待提示列表）。

    这不是机密信息，和 key 放在同一个 settings.json 里，随文件一起搬到
    别的电脑也能看出上次同步到哪一步。
    """

    state = _read().get(_EXCHANGE_SYNC_FIELD)
    return dict(state) if isinstance(state, dict) else {}


def save_exchange_sync_state(state: dict[str, Any]) -> bool:
    data = _read()
    data["version"] = 1
    data[_EXCHANGE_SYNC_FIELD] = dict(state or {})
    return _write(data)
