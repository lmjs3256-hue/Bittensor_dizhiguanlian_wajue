"""Input adapters for address libraries and indexed transfer data."""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator

from .relation_config import AddressRecord, LibraryEntry, TransferRecord
from .validation import is_valid_ss58


class InputError(ValueError):
    pass


def _amount(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InputError(f"invalid amount: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise InputError(f"invalid amount: {value!r}")
    return result


def _string(row: dict, name: str, default: str = "") -> str:
    value = row.get(name, default)
    return "" if value is None else str(value).strip()


def iter_transfers_json(payload: dict | list) -> Iterator[TransferRecord]:
    if isinstance(payload, dict):
        raw = payload.get("transfers", [])
    elif isinstance(payload, list):
        raw = payload
    else:
        raise InputError("transfer JSON must be an array or contain a transfers array")
    if not isinstance(raw, list):
        raise InputError("transfers must be an array")
    for position, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            raise InputError(f"transfer {position} must be an object")
        try:
            block_number = int(row["block_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"transfer {position}: invalid block_number") from exc
        source = _string(row, "from") or _string(row, "from_address")
        target = _string(row, "to") or _string(row, "to_address")
        if not source or not target:
            raise InputError(f"transfer {position}: from and to are required")
        yield TransferRecord(
            tx_hash=_string(row, "tx_hash") or _string(row, "extrinsic_hash"),
            block_number=block_number,
            from_address=source,
            to_address=target,
            amount_tao=_amount(row.get("amount_tao", row.get("amount"))),
            block_hash=_string(row, "block_hash"),
            timestamp=_string(row, "timestamp"),
        )


def iter_transfers_csv(text: str) -> Iterator[TransferRecord]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"from", "to", "amount_tao", "block_number"}
    fieldnames = set(reader.fieldnames or [])
    if not required.issubset(fieldnames):
        raise InputError(
            "transfer CSV needs columns: from,to,amount_tao,block_number"
        )
    for position, row in enumerate(reader, start=2):
        source = _string(row, "from")
        target = _string(row, "to")
        if not source or not target:
            raise InputError(f"CSV row {position}: from and to are required")
        try:
            block_number = int(_string(row, "block_number"))
        except ValueError as exc:
            raise InputError(f"CSV row {position}: invalid block_number") from exc
        yield TransferRecord(
            tx_hash=_string(row, "tx_hash"),
            block_number=block_number,
            from_address=source,
            to_address=target,
            amount_tao=_amount(_string(row, "amount_tao")),
            block_hash=_string(row, "block_hash"),
            timestamp=_string(row, "timestamp"),
        )


def iter_addresses_json(payload: dict | list) -> Iterator[AddressRecord]:
    if isinstance(payload, dict):
        raw = payload.get("addresses", [])
    elif isinstance(payload, list):
        raw = payload
    else:
        raise InputError("address JSON must be an array or contain an addresses array")
    if not isinstance(raw, list):
        raise InputError("addresses must be an array")
    for position, row in enumerate(raw, start=1):
        if isinstance(row, str):
            yield AddressRecord(address=row.strip(), category="SEED")
            continue
        if not isinstance(row, dict):
            raise InputError(f"address {position} must be a string or object")
        address = _string(row, "address") or _string(row, "coldkey")
        if not address:
            raise InputError(f"address {position}: address is required")
        category = (_string(row, "category") or "SEED").upper()
        if category not in {"SEED", "WATCH", "BLACKLIST", "EXCHANGE", "VALIDATOR"}:
            raise InputError(
                f"address {position}: category must be SEED, WATCH, BLACKLIST, EXCHANGE or VALIDATOR"
            )
        yield AddressRecord(
            address=address,
            label=_string(row, "label"),
            category=category,
            note=_string(row, "note"),
        )


def parse_plain_addresses(text: str) -> list[str]:
    """Parse one SS58 address per line, tolerating an optional CSV suffix."""

    values = []
    for line_number, line in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        first_column = body.split(",", 1)[0].strip()
        if first_column.lower() in {"address", "coldkey"}:
            continue
        address = first_column.split()[0] if first_column else ""
        if not address:
            raise InputError(f"line {line_number}: address is too short")
        values.append(address)
    return values


# -- 容错导入：从杂乱文本里把地址抠出来 -------------------------------------
# 用户手上的地址清单往往是从各处复制拼接的：一行好几个、行尾多逗号、
# 地址后面跟着备注、粘贴时混进几个无关字符。旧解析器「只取第一列、只取
# 第一个空白前的 token」，遇到一行多个这种会**静默丢地址**（不报错，
# 用户以为全导进去了），备注也全丢。下面这组函数换个思路：
#
# 1. 先用正则把所有「形似 SS58」的片段抠出来（不依赖行/分隔符）；
# 2. 再交给 ``is_valid_ss58`` 做真校验（blake2b 校验和），垃圾片段自然被淘汰；
# 3. 地址后面紧跟的、不是地址的那段文字当备注。
#
# 这样「一行一个」「一行十个」「逗号/空格/制表符/中文逗号分隔」全都一致对待。

# SS58 的 base58 字母表（不含 0 O I l）。长度放宽到 35–50，
# 交给校验和去判真伪——宁可多抠几个让校验淘汰，也不要漏抠。
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SS58_RUN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{35,50}")
# 备注/标签里不该出现的东西：再出现一个地址就说明这是下一个地址了。
_SS58_LIKE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{35,50}")
# 分隔符：逗号（半角/全角）、顿号、分号（半角/全角）、制表符、竖线、空白。
_SEPARATORS = re.compile(r"[,，、;；|\t]+")
# 明显不是备注的东西（表头、纯标点）。
_NON_REMARK = re.compile(r"^[\s\-—_=*#•·.。]*$")


def _looks_like_address_run(token: str) -> bool:
    return bool(_SS58_RUN.fullmatch(token))


def _salvage(token: str) -> str:
    """地址后面粘了垃圾字符时，把头部的真地址抢救出来。

    ``5Doh…x5EWvH3o`` 这种：正则会把整串（含 ``vH3o``）一次吞掉，
    校验和自然不过，旧做法就整条丢掉——用户丢了一个真地址还不知道。
    SS58 的合法长度只有 47/48（少数网络前缀下 49），所以按这些长度
    从左往右试，第一个校验通过的就算数。
    """

    # 优先取更长的：如果 48 位能通过校验和，那它才是完整地址；
    # 只有当 48 位不通过时才退回 47。反过来会把地址末尾的一两位
    # 当成垃圾切掉，剩下的 1-2 个字符又变成莫名的备注。
    for length in (48, 49, 47):
        if len(token) > length:
            head = token[:length]
            if is_valid_ss58(head):
                return head
    return ""


def _remark_after(text: str, start: int, end: int, salvage_end: int = 0) -> str:
    """取地址结束后、同一行内紧跟的那段文字作为备注。

    ``text`` 是整行原文，``end`` 是地址在行内的结束下标。``salvage_end``
    给出"原匹配片段"的结束位置：地址尾部粘了垃圾时（``地址vH3o``），
    这段垃圾是粘贴残留，不该进备注，应当跳过它再取逗号后的真备注。

    遇到下一个地址就开始之前停下——一行的格式通常是
    ``地址,备注 地址 地址``，备注只属于它前面那个地址。
    """

    rest = text[end:]
    # 只在同一行里找：换行之后不属于这个地址。
    newline = rest.find("\n")
    if newline != -1:
        rest = rest[:newline]
    # 跳过粘在地址尾部的垃圾字符（如果调用方标出了原片段终点）。
    # 注意正则 `{35,50}` 是贪婪但有上限的：`地址48位 + vH3o` 共 52 字符，
    # 它只吞前 50 个，剩下的 `3o` 会漏到备注里。所以这里按 base58 字符集
    # 一路剥到底（上限 8 个，避免把紧跟的英文备注也当成垃圾）。
    if salvage_end > end:
        tail = rest
        cut = 0
        while cut < len(tail) and cut < 8 and tail[cut] in _B58:
            cut += 1
        rest = tail[cut:]
    rest = rest.lstrip(" \t")
    if not rest:
        return ""
    # 逗号/顿号/分号：字段分隔。取第一段就够——CSV 里后面还可能是
    # category / note 等列（``地址,主钱包,vH3o``），不该整串塞进备注。
    if rest[0] in ",，、;；|":
        # 首个字段是空串（分隔符紧贴地址），取第一个非空字段。
        parts = [part for part in _SEPARATORS.split(rest) if part.strip()]
        candidate = parts[0] if parts else ""
    else:
        candidate = rest
    # 不管哪种分隔，都不能把下一行的/下一个地址吞进来当备注。
    match = _SS58_LIKE.search(candidate)
    if match:
        candidate = candidate[: match.start()]
    candidate = candidate.strip(" \t,，、;；|")
    if _NON_REMARK.match(candidate):
        return ""
    if candidate.lower() in {"address", "coldkey", "地址"}:
        return ""
    return candidate


def _looks_like_paste_garbage(remark: str) -> bool:
    """这段「备注」像不像复制时粘上的残留？

    判据刻意保守——只有**短且全是大写字母数字小写混杂的 base58 风格片段**才
    算可疑（如 ``vH3o``），中文备注、带空格的句子、正常的英文名都不受影响。

    注意：仍然会把它存成备注（用户明确要「有备注仍然自动保存」），
    这里只是让界面能提示「有几条备注看着像粘贴垃圾，建议核对」。
    """

    if not remark or len(remark) > 8:
        return False
    if not remark.isascii():
        return False
    # 纯字母（大小写混杂）或字母数字，且不含空格/标点——正常备注通常是
    # 中文、带空格的短语或全小写英文词，很少是这种 4 位混合串。
    return bool(re.fullmatch(r"[A-Za-z0-9]{1,8}", remark)) and any(
        ch.isupper() for ch in remark
    ) and any(ch.islower() for ch in remark)


def parse_messy_addresses(
    text: str, *, remark_as: str = "label"
) -> tuple[list[dict[str, str]], dict[str, list[str]]]:
    """把杂乱地址文本解析成 (有效行, 问题汇总)。

    返回的每个有效行形如 ``{"address": ..., "label": ..., "note": ...}``：
    地址后面紧跟的那段文字放进 ``remark_as`` 指定的字段（默认 ``label``，
    与「地址,存储钱包」这种习惯一致），另一个留空。

    ``问题汇总`` 有四个键：

    - ``duplicate``：同一份文件里重复出现的地址（保留第一次出现的备注）；
    - ``invalid``：形似地址但校验和不通过 / 长度不对的片段（原样回传，
      让用户能看出是哪个）；
    - ``suspect``：认出来但**不像正常备注**的短片段（如复制残留的 ``vH3o``）。
      仍然保存为备注（用户要求「有备注仍然自动保存」），单独列出来是为了
      让用户能发现数据里的粘贴垃圾，而不是被悄悄写进备注；
    - ``remarks``：实际识别到的备注数量（仅用于回显，不计入问题）；
    - ``total``：文本里一共认出多少个候选片段。
    """

    body = text.lstrip("\ufeff")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    invalid: list[str] = []
    remarks: list[str] = []
    suspect: list[str] = []

    for line in body.splitlines():
        # 允许用 # 写整行注释，但要排除「地址#备注」把地址切断的情况：
        # 只有 # 前面没有地址时才当注释行。
        if "#" in line and not _SS58_RUN.search(line.split("#", 1)[0]):
            line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue
        # 表头行跳过（导出的 CSV 表头常被一起粘进来）。
        if line.split(",", 1)[0].strip().lower() in {"address", "coldkey", "地址"}:
            continue

        found = False
        for match in _SS58_RUN.finditer(line):
            token = match.group(0)
            end = match.end()
            if not is_valid_ss58(token):
                # 尾部粘了垃圾字符：抢救头部那个真地址，剩下的当备注来源。
                saved = _salvage(token)
                if not saved:
                    invalid.append(token)
                    continue
                end = match.start() + len(saved)
                token = saved
            found = True
            if token in seen:
                duplicates.append(token)
                continue
            seen.add(token)
            remark = _remark_after(line, match.start(), end, match.end())
            row = {"address": token, "label": "", "note": ""}
            if remark:
                row[remark_as] = remark
                remarks.append(remark)
                if _looks_like_paste_garbage(remark):
                    suspect.append(remark)
            entries.append(row)
        if not found:
            # 这一行没有合法地址：如果它长得像地址（长度接近），记进 invalid
            # 让用户知道有东西没导进去；纯说明文字就不吵他。
            for token in _SEPARATORS.split(line):
                token = token.strip()
                if token and _SS58_LIKE.fullmatch(token) and token not in invalid:
                    invalid.append(token)

    return entries, {
        "duplicate": duplicates,
        "invalid": invalid,
        "suspect": suspect,
        "remarks": remarks,
        "total": len(entries) + len(duplicates) + len(invalid),
    }



def read_transfers(path: Path) -> list[TransferRecord]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        import json

        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise InputError(f"cannot read JSON: {exc}") from exc
        return list(iter_transfers_json(payload))
    if suffix == ".csv":
        return list(iter_transfers_csv(path.read_text(encoding="utf-8-sig")))
    raise InputError("transfer file must be .json or .csv")


def read_addresses(path: Path) -> list[AddressRecord]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        import json

        try:
            payload = json.loads(text)
        except Exception as exc:
            raise InputError(f"cannot read JSON: {exc}") from exc
        return list(iter_addresses_json(payload))
    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        names = set(reader.fieldnames or [])
        if not names.intersection({"address", "coldkey"}):
            raise InputError("address CSV needs an address or coldkey column")
        return list(iter_addresses_json(list(reader)))
    if suffix == ".txt":
        # 与页面导入用同一套容错解析：一行多个、备注、空格都能处理。
        rows, _ = parse_messy_addresses(text)
        return [
            AddressRecord(
                address=row["address"],
                label=row.get("label") or "",
                category="SEED",
                note=row.get("note") or "",
            )
            for row in rows
        ]
    raise InputError("address file must be .json, .csv or .txt")


def state_category(order: Iterable[str]) -> str:
    """Resolve a duplicated imported address with a strict precedence."""

    priority = {"BLACKLIST": 3, "WATCH": 2, "SEED": 1}
    return max(order, key=lambda value: priority.get(value, 0))
