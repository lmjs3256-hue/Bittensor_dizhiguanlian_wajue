from __future__ import annotations

import hashlib
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


PROJECT_OWNER_BOOK = re.compile(r"^SN\s*(\d+)", re.IGNORECASE)
PROJECT_OWNER_MIN_NETUID = 1
PROJECT_OWNER_MAX_NETUID = 127
MANUAL_NOTE_SEPARATOR = re.compile(r"[,，;；、\n]+")
TAG_EDGE = "【】[] "

ADDRESS_EVENT_NAMES = {
    ("Balances", "Transfer"),
    ("SubtensorModule", "StakeAdded"),
    ("SubtensorModule", "StakeRemoved"),
    ("SubtensorModule", "StakeTransferred"),
    ("SubtensorModule", "StakeAndHotkeyTransferred"),
}


def mask_address(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return text
    return f"{text[:4]}…{text[-4:]}"


def _subnet_label(value: Any) -> str:
    try:
        return f"SN{int(value)}"
    except (TypeError, ValueError):
        return "SN待定"


def _project_owner_netuid(item: dict[str, Any]) -> int | None:
    # Project-owner libraries also collect ordinary auto-imported addresses.
    # Only the current SubnetOwner row is official; a matching SN book alone is
    # not enough to colour every member purple.
    if str(item.get("address_role") or "") != "project_owner":
        return None
    book = str(item.get("library_book") or "").strip()
    match = PROJECT_OWNER_BOOK.match(book)
    if not match:
        return None
    netuid = int(match.group(1))
    if PROJECT_OWNER_MIN_NETUID <= netuid <= PROJECT_OWNER_MAX_NETUID:
        return netuid
    return None


def project_owner_tags(watched_addresses: list[dict[str, Any]]) -> list[str]:
    netuids = sorted({
        netuid for item in watched_addresses
        if (netuid := _project_owner_netuid(item)) is not None
    })
    return [f"SN{netuid}项目方" for netuid in netuids]


def bracket_tag(value: Any) -> str:
    """Return one display tag with Chinese square brackets and no duplicates."""

    text = str(value or "").strip()
    while len(text) >= 2 and text[0] in "【[" and text[-1] in "】]":
        text = text[1:-1].strip()
    return f"【{text}】" if text else ""


def is_system_auto_address_label(value: Any, address: Any) -> bool:
    """Detect labels that are just a copy of the address suffix."""

    label = str(value or "").strip().strip(TAG_EDGE)
    account = str(address or "").strip()
    if not label or label in {"未命名地址", account, mask_address(account)}:
        return True
    return len(label) <= 4 and bool(account) and account.endswith(label)


def manual_note_tags(value: Any) -> list[str]:
    """Split a free-form manual note into stable, display-ready tags."""

    text = str(value or "").strip()
    if not text:
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for part in MANUAL_NOTE_SEPARATOR.split(text):
        clean = part.strip().strip(TAG_EDGE)
        if not clean:
            continue
        tag = bracket_tag(clean)
        key = re.sub(r"\s+", "", clean).casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def append_project_owner_suffix(summary: str, tags: list[str]) -> str:
    if not tags:
        return summary
    suffix = "".join(bracket_tag(tag) for tag in tags)
    text = str(summary or "").rstrip()
    legacy_suffix = f"（{'、'.join(tags)}）"
    if text.endswith(legacy_suffix):
        text = text[:-len(legacy_suffix)].rstrip()
    return text if text.endswith(suffix) else f"{text}{suffix}"


def project_owner_tags_for_raw_event(
    raw: dict[str, Any],
    watched_addresses: list[dict[str, Any]],
) -> list[str]:
    module = str(raw.get("module") or "")
    event_name = str(raw.get("event_name") or "")
    values = _ordered_values(raw)
    accounts: list[Any] = []
    if module == "Balances" and event_name == "Transfer":
        accounts = [_field(values, "from", 0), _field(values, "to", 1)]
    elif module == "SubtensorModule" and event_name == "ColdkeySwapped":
        accounts = [_field(values, "old_coldkey", 0), _field(values, "new_coldkey", 1)]
    elif event_name in {"StakeAdded", "StakeRemoved"}:
        accounts = [_field(values, "coldkey", 0), _field(values, "hotkey", 1)]
    elif event_name == "StakeTransferred":
        accounts = [
            _field(values, "origin_coldkey", 0),
            _field(values, "destination_coldkey", 1),
            _field(values, "hotkey", 2),
        ]
    elif event_name == "StakeAndHotkeyTransferred":
        accounts = [
            _field(values, "origin_coldkey", 0),
            _field(values, "destination_coldkey", 1),
            _field(values, "origin_hotkey", 2),
            _field(values, "destination_hotkey", 3),
        ]
    matched = [
        item for item in watched_addresses
        if any(_same_account(item.get("address"), account) for account in accounts if account)
    ]
    return project_owner_tags(matched)


def address_event_record(
    raw: dict[str, Any],
    *,
    network: str,
    watched_addresses: list[dict[str, Any]],
    block_hash: str,
    height: int,
    spec_version: int | None,
    event_at: str,
) -> dict[str, Any] | None:
    module = str(raw.get("module") or "")
    event_name = str(raw.get("event_name") or "")
    if (module, event_name) not in ADDRESS_EVENT_NAMES:
        return None
    values = _ordered_values(raw)
    accounts: list[tuple[str, str]] = []
    event_type = ""
    netuid: int | None = None
    summary = ""
    details: dict[str, Any] = {}
    transfer_amount_rao: int | None = None
    summary_subnet = ""
    summary_amount = ""
    summary_action = ""
    address_action = "transfer"

    if module == "Balances" and event_name == "Transfer":
        sender = _field(values, "from", 0)
        recipient = _field(values, "to", 1)
        amount_value = _field(values, "amount", 2)
        amount = _tao_amount_2(amount_value)
        transfer_amount_rao = _integer(amount_value)
        accounts = [("from", str(sender)), ("to", str(recipient))]
        event_type = "普通转账"
        summary_subnet = "全链"
        summary_amount = f"{amount} TAO"
        address_action = "transfer"
        details = {
            "from": sender,
            "to": recipient,
            "amount_rao": transfer_amount_rao,
            "amount_tao": amount,
        }
    elif event_name in {"StakeAdded", "StakeRemoved"}:
        coldkey = _field(values, "coldkey", 0)
        hotkey = _field(values, "hotkey", 1)
        tao_amount = _amount(_field(values, "tao_amount", 2))
        display_tao_amount = _tao_amount_2(_field(values, "tao_amount", 2))
        alpha_amount = _amount(_field(values, "alpha_amount", 3))
        netuid = _integer(_field(values, "netuid", 4))
        verb = "质押" if event_name == "StakeAdded" else "解质押"
        summary_subnet = _subnet_label(netuid)
        summary_amount = f"{display_tao_amount} TAO"
        # 方向只通过绿色/红色表示，摘要不再写“买入/卖出”。
        summary_action = ""
        address_action = "buy" if event_name == "StakeAdded" else "sell"
        event_type = (
            "零号子网质押" if netuid == 0 and event_name == "StakeAdded"
            else "零号子网解质押" if netuid == 0
            else verb
        )
        accounts = [("coldkey", str(coldkey)), ("hotkey", str(hotkey))]
        details = {
            "coldkey": coldkey,
            "hotkey": hotkey,
            "tao_amount": tao_amount,
            "alpha_amount": alpha_amount,
            "netuid": netuid,
        }
    elif event_name == "StakeTransferred":
        origin = _field(values, "origin_coldkey", 0)
        destination = _field(values, "destination_coldkey", 1)
        hotkey = _field(values, "hotkey", 2)
        origin_netuid = _integer(_field(values, "origin_netuid", 3))
        destination_netuid = _integer(_field(values, "destination_netuid", 4))
        tao_amount = _tao_amount_2(_field(values, "amount", 5))
        netuid = destination_netuid
        event_type = "代币转移"
        summary_subnet = f"{_subnet_label(origin_netuid)}→{_subnet_label(destination_netuid)}"
        summary_amount = f"{tao_amount} TAO 等值"
        address_action = "transfer"
        accounts = [
            ("origin_coldkey", str(origin)),
            ("destination_coldkey", str(destination)),
            ("hotkey", str(hotkey)),
        ]
        details = {
            "origin_coldkey": origin,
            "destination_coldkey": destination,
            "hotkey": hotkey,
            "origin_netuid": origin_netuid,
            "destination_netuid": destination_netuid,
            "tao_amount": tao_amount,
        }
    elif event_name == "StakeAndHotkeyTransferred":
        origin = _field(values, "origin_coldkey", 0)
        destination = _field(values, "destination_coldkey", 1)
        origin_hotkey = _field(values, "origin_hotkey", 2)
        destination_hotkey = _field(values, "destination_hotkey", 3)
        origin_netuid = _integer(_field(values, "origin_netuid", 4))
        destination_netuid = _integer(_field(values, "destination_netuid", 5))
        tao_amount = _tao_amount_2(_field(values, "amount", 6))
        netuid = destination_netuid
        event_type = "代币转移"
        summary_subnet = f"{_subnet_label(origin_netuid)}→{_subnet_label(destination_netuid)}"
        summary_amount = f"{tao_amount} TAO 等值"
        address_action = "transfer"
        accounts = [
            ("origin_coldkey", str(origin)),
            ("destination_coldkey", str(destination)),
            ("origin_hotkey", str(origin_hotkey)),
            ("destination_hotkey", str(destination_hotkey)),
        ]
        details = {
            "origin_coldkey": origin,
            "destination_coldkey": destination,
            "origin_hotkey": origin_hotkey,
            "destination_hotkey": destination_hotkey,
            "origin_netuid": origin_netuid,
            "destination_netuid": destination_netuid,
            "tao_amount": tao_amount,
        }

    matched = [
        watched
        for watched in watched_addresses
        if any(_same_account(watched.get("address"), address) for _, address in accounts)
    ]
    if not matched:
        return None
    if event_type == "普通转账" and transfer_amount_rao is not None:
        matched = [
            watched for watched in matched
            if _passes_transfer_threshold(watched, transfer_amount_rao)
        ]
        if not matched:
            return None
    # When both the project owner and an ordinary auto-imported counterparty
    # are watched, show the project owner as the primary address. Otherwise the
    # faster newly-added counterparty row can hide who actually triggered the event.
    matched.sort(key=lambda item: (
        0 if (
            item.get("address_role") == "project_owner"
            or project_owner_tags([item])
        ) else 1,
        str(item.get("created_at") or ""),
    ))
    owner_tags = project_owner_tags(matched)
    primary = matched[0]
    primary_address = str(primary.get("address") or "").strip()
    display_tags: list[str] = []
    seen_tags: set[str] = set()
    matched_details: list[dict[str, Any]] = []
    for item in matched:
        item_tags: list[str] = []
        label = str(item.get("label") or "").strip()
        address = str(item.get("address") or "").strip()
        library_book = str(item.get("library_book") or "").strip()
        if library_book:
            item_tags.append(bracket_tag(library_book))
        elif not is_system_auto_address_label(label, address):
            item_tags.append(bracket_tag(label))
        note_tags = manual_note_tags(item.get("manual_note"))
        item_tags.extend(note_tags)
        unique_item_tags: list[str] = []
        item_seen_tags: set[str] = set()
        for tag in item_tags:
            compact = re.sub(r"\s+", "", tag).casefold()
            if not compact or compact in item_seen_tags:
                continue
            item_seen_tags.add(compact)
            unique_item_tags.append(tag)
            if compact not in seen_tags:
                seen_tags.add(compact)
                display_tags.append(tag)
        matched_details.append({
            "address_id": item.get("address_id"),
            "address": item.get("address"),
            "masked": mask_address(item.get("address")),
            "label": label or "未命名地址",
            "library_book": item.get("library_book") or "",
            "manual_note": str(item.get("manual_note") or ""),
            "manual_note_tags": note_tags,
            "address_role": str(item.get("address_role") or ""),
            "official_owner": str(item.get("address_role") or "") == "project_owner",
            "display_tags": unique_item_tags,
        })

    summary_parts = [summary_subnet]
    if summary_action:
        summary_parts.append(f"{summary_action} {summary_amount}")
    else:
        summary_parts.append(summary_amount)
    if event_type == "普通转账":
        summary_address = (
            f"{mask_address(details.get('from'))} → {mask_address(details.get('to'))}"
        )
    elif event_type == "代币转移":
        origin = details.get("origin_coldkey")
        destination = details.get("destination_coldkey")
        summary_address = f"{mask_address(origin)} → {mask_address(destination)}"
    else:
        summary_address = mask_address(primary_address)
    summary_parts.append(summary_address)
    # 地址标签（项目方、手动备注）只作为结构化 UI 标签展示，不再拼进持久化摘要，
    # 避免旧前端重复追加尾注，也便于后续修改备注时实时刷新历史事件。
    summary = " · ".join(part for part in summary_parts if part)
    raw_id = str(raw["raw_id"])
    event_id = hashlib.sha256(f"address:{raw_id}".encode("utf-8")).hexdigest()
    return {
        "event_id": event_id,
        "network": network,
        "netuid": netuid,
        "event_type": event_type,
        "event_name": f"{module}.{event_name}",
        "source": "address",
        "source_url": (
            "https://polkadot.js.org/apps/"
            "?rpc=wss%3A%2F%2Fentrypoint-finney.opentensor.ai%3A443"
            f"#/explorer/query/{block_hash}"
        ),
        "event_at": event_at,
        "summary": summary,
        "details": {
            "address_monitor": True,
            "project_owner_tags": owner_tags,
            "address_action": address_action,
            "summary_subnet": summary_subnet,
            "summary_amount": summary_amount,
            "primary_address": primary_address,
            "manual_note_tags": [
                tag for item in matched for tag in manual_note_tags(item.get("manual_note"))
            ],
            "display_tags": display_tags,
            "matched_addresses": matched_details,
            **details,
        },
        "raw_data": raw.get("raw") or {},
        "block_number": height,
        "block_hash": block_hash,
        "event_index": raw.get("event_index"),
        "runtime_spec_version": spec_version,
        "chain_status": "observed",
        "enrichment_status": "complete",
        "confidence": "high",
    }


def _ordered_values(raw: dict[str, Any]) -> dict[str, Any]:
    raw_data = raw.get("raw") or {}
    attributes = raw_data.get("attributes") or {}
    result = dict(attributes) if isinstance(attributes, dict) else {}
    if isinstance(attributes, list):
        result.update({str(index): item for index, item in enumerate(attributes)})
    value = raw_data.get("value") or {}
    if isinstance(value, dict):
        nested = value.get("attributes") or value.get("params") or value
        if isinstance(nested, dict):
            result.update(nested)
        if isinstance(nested, list):
            result.update({
                str(index): item for index, item in enumerate(nested)
            })
    return result


def _field(values: dict[str, Any], name: str, index: int) -> Any:
    aliases = (name, str(index), index)
    for key in aliases:
        if key in values:
            return values[key]
    return None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tao_amount_2(value: Any) -> str:
    number = _integer(value)
    if number is None:
        return "—"
    amount = Decimal(max(0, number)) / Decimal(1_000_000_000)
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _amount(value: Any) -> str:
    number = _integer(value)
    if number is None:
        return "—"
    whole, fraction = divmod(max(0, number), 1_000_000_000)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:09d}".rstrip("0")


def _ss58_account_bytes(value: str) -> bytes:
    from ..validation import _decode

    raw = _decode(str(value or "").strip())
    prefix_length = 1 if raw and raw[0] < 64 else 2
    return raw[prefix_length:-2]


def _same_account(expected: Any, actual: Any) -> bool:
    expected_text = str(expected or "").strip()
    actual_text = str(actual or "").strip()
    if expected_text and actual_text and expected_text == actual_text:
        return True
    try:
        return _ss58_account_bytes(expected_text) == _ss58_account_bytes(actual_text)
    except Exception:
        return False


def _passes_transfer_threshold(watched: dict[str, Any], amount_rao: int) -> bool:
    try:
        minimum_tao = Decimal(str(watched.get("min_transfer_tao") or 0))
    except Exception:
        minimum_tao = Decimal(0)
    minimum_rao = max(Decimal(0), minimum_tao) * Decimal(1_000_000_000)
    return Decimal(max(0, amount_rao)) >= minimum_rao
