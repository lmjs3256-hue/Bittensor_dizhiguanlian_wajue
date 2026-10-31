"""Input adapters for address libraries and indexed transfer data."""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator

from .relation_config import AddressRecord, LibraryEntry, TransferRecord


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
        return [
            AddressRecord(address=value, category="SEED")
            for value in parse_plain_addresses(text)
        ]
    raise InputError("address file must be .json, .csv or .txt")


def state_category(order: Iterable[str]) -> str:
    """Resolve a duplicated imported address with a strict precedence."""

    priority = {"BLACKLIST": 3, "WATCH": 2, "SEED": 1}
    return max(order, key=lambda value: priority.get(value, 0))
