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
from decimal import Decimal, InvalidOperation
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


_RATE_FIELD = "taostats_requests_per_minute"


def save_requests_per_minute(value: int) -> int:
    """保存每把 key 的每分钟额度（本地保护限速）。"""

    limit = max(1, min(int(value), 600))
    data = _read()
    data[_RATE_FIELD] = limit
    data["version"] = 1
    _write(data)
    return limit


def load_requests_per_minute() -> int | None:
    """读回保存过的额度；没保存过返回 None（沿用默认 5）。"""

    value = _read().get(_RATE_FIELD)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(limit, 600))


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


# -- 扫描阈值（单边最低 / 累计最低 TAO） ------------------------------------
_SCAN_THRESHOLDS_FIELD = "scan_thresholds"

# 默认就是页面出厂值：单边 1、累计 1（TAO）。
# 把层级放大到 4～10 级时，浅层的零星小额会把结果灌满，用户会把这两个值
# 抬上去（例如 10 / 50），抬完还要每次重开页面重新填一遍太麻烦，所以给一个
# 显式的「保存」按钮落盘——只在用户点保存时写，不自动记住输入过程。
DEFAULT_SCAN_THRESHOLDS: dict[str, Any] = {
    "min_edge_tao": "1",
    "min_total_tao": "1",
}


def _clean_threshold(value: Any, fallback: str) -> str:
    """把阈值规整成十进制字符串；非法或负数一律回落 ``fallback``。

    用字符串而不是 float：阈值要和 ``LinkPolicy`` 的 ``Decimal`` 对齐，
    走 float 会出现 0.10000000000000001 这种写回页面的脏数字。
    """

    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return fallback
    if not number.is_finite() or number < 0:
        return fallback
    # 写成十进制普通写法（不带指数），并去掉多余的尾零：10.00 → 10、0.50 → 0.5。
    return format(number.normalize(), "f")


def load_scan_thresholds() -> dict[str, str]:
    """读取上次保存的单边 / 累计最低 TAO；没保存过就是 1 / 1。"""

    raw = _read().get(_SCAN_THRESHOLDS_FIELD)
    values = dict(DEFAULT_SCAN_THRESHOLDS)
    if isinstance(raw, dict):
        for key in DEFAULT_SCAN_THRESHOLDS:
            values[key] = _clean_threshold(raw.get(key), DEFAULT_SCAN_THRESHOLDS[key])
    return values


def save_scan_thresholds(min_edge_tao: Any, min_total_tao: Any) -> dict[str, str]:
    """保存单边 / 累计最低 TAO，返回真正写入的值。

    非法输入（空、非数字、负数）**不覆盖**上次保存的值——用户误触「保存」
    时应该保持原设置，而不是被悄悄改成出厂值 1 / 1。
    """

    current = load_scan_thresholds()
    values = {
        "min_edge_tao": _clean_threshold(min_edge_tao, current["min_edge_tao"]),
        "min_total_tao": _clean_threshold(min_total_tao, current["min_total_tao"]),
    }
    data = _read()
    data["version"] = 1
    data[_SCAN_THRESHOLDS_FIELD] = values
    _write(data)
    return values


# -- 地址库巡检（自动发现新地址并入库） --------------------------------------
_WATCH_FIELD = "address_watch"

# 默认：开启、每天一轮、单笔或累计 30 TAO 触发、含代币转移、每天最多 700 次请求。
# 700 是按「全库 690 个地址 × 1 次」估的，开代币转移实际会到 1380，
# 所以每日预算到点就停、第二天接着轮转，不会超发。
DEFAULT_WATCH_CONFIG: dict[str, Any] = {
    "enabled": True,
    "interval_minutes": 1440,
    "threshold_tao": 30.0,
    "include_stake": True,
    "daily_budget": 700,
    # 一次巡检推进几个地址（每个地址约 2 次请求）。0 = 跟随每分钟额度自动算。
    "addresses_per_tick": 0,
    "auto_import": True,
    # 自动入库的地址默认不再继续巡检，否则会顺着转账一路扩散、把库撑爆。
    "follow_auto_imported": False,
    # 本地索引优先：本地已覆盖的区间不发 Taostats 请求（不花额度）。
    # 本地索引由链上 RPC 直采，完全不消耗 Taostats 配额。
    "prefer_local_index": True,
    # 本地索引流水线：拿本地索引的新块去撞地址库，命中即入库。
    # 与上面 prefer_local_index 的区别——那个是「逐地址巡检时先查本地」，
    # 这个是完全独立的一条采集链路，零 API、可单独开关。
    "sweep_enabled": True,
}


def load_watch_config() -> dict[str, Any]:
    raw = _read().get(_WATCH_FIELD)
    config = dict(DEFAULT_WATCH_CONFIG)
    if isinstance(raw, dict):
        for key in DEFAULT_WATCH_CONFIG:
            if key in raw:
                config[key] = raw[key]
    try:
        config["interval_minutes"] = max(15, int(config["interval_minutes"]))
        config["daily_budget"] = max(0, int(config["daily_budget"]))
        config["addresses_per_tick"] = max(0, min(int(config["addresses_per_tick"]), 50))
        config["threshold_tao"] = max(0.0, float(config["threshold_tao"]))
    except (TypeError, ValueError):
        return dict(DEFAULT_WATCH_CONFIG)
    config["enabled"] = bool(config["enabled"])
    config["include_stake"] = bool(config["include_stake"])
    config["auto_import"] = bool(config["auto_import"])
    config["follow_auto_imported"] = bool(config["follow_auto_imported"])
    config["prefer_local_index"] = bool(config["prefer_local_index"])
    config["sweep_enabled"] = bool(config["sweep_enabled"])
    return config


def save_watch_config(config: dict[str, Any]) -> bool:
    merged = load_watch_config()
    for key in DEFAULT_WATCH_CONFIG:
        if key in config:
            merged[key] = config[key]
    merged = {key: merged[key] for key in DEFAULT_WATCH_CONFIG}
    data = _read()
    data["version"] = 1
    data[_WATCH_FIELD] = merged
    return _write(data)


# -- 远程访问口令 ----------------------------------------------------------
# 甲方案下主机要监听 0.0.0.0 才能被 Tailscale / 局域网访问，这道口令就是
# 除「能连上这个网」之外的第二道门。它不是机密，和 key 放在同一个文件里，
# 换电脑时一起带走，等于把「哪些设备能进」也一起迁移过去。
_ACCESS_TOKEN_FIELD = "access_token"


def load_access_token() -> str | None:
    value = _read().get(_ACCESS_TOKEN_FIELD)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def save_access_token(token: str) -> bool:
    value = token.strip()
    if not value:
        return False
    data = _read()
    data["version"] = 1
    data[_ACCESS_TOKEN_FIELD] = value
    return _write(data)


# -- 多把 API key（key 池）--------------------------------------------------
#
# 单把 key 时，5 次/分钟的额度是硬上限，多人同时查会互相挤（先到的人占满、
# 后到的人直接 429）。多把 key 是唯一能线性扩容的办法。
#
# 存储形态：`settings.json` 的 `taostats_api_keys` 是一个数组，每项
# `{"enc": "dpapi"|"plain", "data": "<base64>", "label": "主号"}`。
# 每把 key **独立加密**，而不是把整个数组序列化后加密一次——这样将来要
# 单独删掉某一把时，不用解开全部再重新加密。
#
# 老字段 `taostats_api_key`（单个 key）保持兼容：读的时候如果新字段为空，
# 就把老字段当成池里唯一那把，用户升级后不用重新填。

_KEYS_FIELD = "taostats_api_keys"


def _encode_secret(secret: str) -> dict[str, str]:
    protected = _dpapi_protect(secret.encode("utf-8"))
    if protected is not None:
        return {"enc": "dpapi", "data": base64.b64encode(protected).decode("ascii")}
    return {"enc": "plain", "data": base64.b64encode(secret.encode("utf-8")).decode("ascii")}


def _decode_secret(record: Any) -> str | None:
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


def save_api_keys(entries: list[dict[str, str]]) -> str:
    """保存 key 池。``entries`` 形如 ``[{"key": "...", "label": "主号"}]``。

    返回存储方式：有任一把用了明文就是 ``plain``，全加密是 ``dpapi``，
    什么都没存是 ``none``。
    """

    records: list[dict[str, str]] = []
    encs: set[str] = set()
    for item in entries or []:
        secret = str((item or {}).get("key") or "").strip()
        if not secret:
            continue
        record = _encode_secret(secret)
        record["label"] = str((item or {}).get("label") or "").strip()[:40]
        records.append(record)
        encs.add(record["enc"])
    data = _read()
    data["version"] = 1
    if records:
        data[_KEYS_FIELD] = records
    else:
        data.pop(_KEYS_FIELD, None)
    # 同步老字段：让只认单个 key 的旧代码（导出、诊断）仍能工作。
    if records:
        data[_SECRET_FIELD] = {
            "enc": records[0]["enc"],
            "data": records[0]["data"],
        }
    else:
        data.pop(_SECRET_FIELD, None)
    if not _write(data):
        return "none"
    if not records:
        return "none"
    return "plain" if "plain" in encs else "dpapi"


def load_api_keys() -> list[dict[str, str]]:
    """读出 key 池，返回 ``[{"key": "...", "label": "..."}]``。

    新字段为空时回退到老的单 key 字段——升级用户不会突然"没配置 key"。
    """

    records = _read().get(_KEYS_FIELD)
    result: list[dict[str, str]] = []
    if isinstance(records, list):
        for record in records:
            secret = _decode_secret(record)
            if not secret:
                continue
            label = ""
            if isinstance(record, dict):
                label = str(record.get("label") or "").strip()
            result.append({"key": secret, "label": label})
    if result:
        return result
    single = load_api_key()
    if single:
        return [{"key": single, "label": ""}]
    return []


def api_keys_storage_kind() -> str:
    """key 池的存储方式（诊断用）。"""

    records = _read().get(_KEYS_FIELD)
    if not isinstance(records, list) or not records:
        return storage_kind()
    encs = {str(r.get("enc")) for r in records if isinstance(r, dict)}
    if "plain" in encs:
        return "plain"
    return "dpapi" if "dpapi" in encs else ""
