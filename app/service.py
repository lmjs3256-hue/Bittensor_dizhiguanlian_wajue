"""Address library import, audit and chain scan orchestration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .collector import RPCCollector, query_account_balances
from .crawl import CrawlState, TaostatsCrawl
from .crawl import stake_counterparty as _stake_counterparty
from .db import (
    AddressLibrary,
    ChainCursor,
    Database,
    RelationArchive,
    ScanJob,
    ScanLock,
    Transfer,
    to_relation_records,
    utcnow,
)
from .inputs import InputError, parse_plain_addresses, read_addresses
from .relation import discover, exchange_balance_candidates
from .relation_config import LibraryEntry, LinkPolicy
from .taostats import (
    LOCAL_REQUESTS_PER_MINUTE,
    TaostatsAuthError,
    TaostatsRateLimitError,
    TaostatsUnavailableError,
    get_active_validator_addresses,
    get_exchange_addresses,
    get_stake_transfer_page,
    get_transfer_page,
)
from .validation import is_valid_ss58


MAX_MANUAL_SCAN_BLOCKS = 16
MAX_TAOSTATS_REQUESTS_PER_QUERY = 5
MAX_EXCHANGE_BALANCE_CHECKS_PER_QUERY = 5
PUBLIC_MONTHLY_CREDIT_REFERENCE = 10_000
# 历史记录只保留最近的若干条 / 总体积上限，避免一次重扫就把 SQLite 撑大。
MAX_ARCHIVE_ROWS = 500
MAX_ARCHIVE_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_SYNC_ADDRESSES = 5000
SYNC_CATEGORIES = {"SEED", "WATCH", "BLACKLIST", "EXCHANGE", "VALIDATOR"}


@dataclass
class RelationContext:
    """一次关联查询中与「爬取」无关的固定上下文。

    分轮作业会把这份上下文和 :class:`CrawlState` 一起保留，下一轮只推进
    爬取状态，不重新读地址库、不重复查验证者名单。
    """

    seed: str
    depth: int
    data_source: str
    policy: LinkPolicy
    filter_exchanges: bool
    filter_validators: bool
    library: list[LibraryEntry]
    first_block: int | None
    last_block: int | None
    local_transfer_rows: list[Any]
    warnings: list[str] = field(default_factory=list)
    exchange_addresses: set[str] = field(default_factory=set)
    unconfirmed_exchange_addresses: set[str] = field(default_factory=set)
    manual_validator_addresses: set[str] = field(default_factory=set)
    auto_validator_addresses: set[str] = field(default_factory=set)
    excluded_addresses: set[str] = field(default_factory=set)
    validator_api_requests: int = 0
    validator_cache_hits: int = 0


def _extend_unique(target: list[str], items: list[str]) -> None:
    """追加提示语并保持原有顺序去重，避免分轮作业重复堆同样的告警。"""

    for item in items:
        if item not in target:
            target.append(item)


class ScanBusyError(RuntimeError):
    pass


def job_dict(job: ScanJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "rpc_url": job.rpc_url,
        "head_block": job.head_block,
        "start_block": job.start_block,
        "end_block": job.end_block,
        "scanned_blocks": job.scanned_blocks,
        "transfer_count": job.transfer_count,
        "status": job.status,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def address_dict(row: AddressLibrary) -> dict[str, Any]:
    label = row.label.strip()
    if not label:
        raw_source = row.source or ""
        source = Path(raw_source).stem
        if source.lower().startswith("dsv"):
            label = "DSV"
        elif source and raw_source not in {"manual", "relation-note"} and not raw_source.startswith("taostats:"):
            label = source
    return {
        "id": row.id,
        "address": row.address,
        "label": label,
        "category": row.category,
        "note": row.note,
        "source": row.source,
        "exchange_confirmed": bool(row.exchange_confirmed),
        "exchange_rejected": bool(row.exchange_rejected),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def select_exportable_addresses(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """从关联结果挑出可导出的地址。

    已人工确认的交易所地址和验证者地址不导出，避免把公共地址当成待跟进
    钱包继续扩散；疑似交易所（待确认）保留，方便人工核查。
    """

    keep: list[str] = []
    seen: set[str] = set()
    skipped_exchange = 0
    skipped_validator = 0
    for row in rows or []:
        category = str(row.get("library_category") or "").upper()
        if (
            category == "EXCHANGE"
            and row.get("exchange_confirmed")
            and not row.get("exchange_rejected")
        ):
            skipped_exchange += 1
            continue
        if category == "VALIDATOR":
            skipped_validator += 1
            continue
        address = str(row.get("address") or "")
        if not address or address in seen:
            continue
        seen.add(address)
        keep.append(address)
    return {
        "addresses": keep,
        "skipped_exchange": skipped_exchange,
        "skipped_validator": skipped_validator,
    }


def _parse_upload(filename: str, text: str) -> list[dict[str, str]]:
    import csv
    import io

    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return [
            {"address": address, "category": "WATCH", "label": "", "note": ""}
            for address in parse_plain_addresses(text)
        ]
    if suffix == ".json":
        raw = json.loads(text)
        values = raw.get("addresses") if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            raise InputError("JSON must contain an addresses array")
    elif suffix == ".csv":
        values = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    else:
        raise InputError("upload must be .txt, .csv or .json")
    result = []

    def boolean(row: dict, name: str, default: bool = False) -> bool:
        value = row.get(name)
        if value is None or str(value).strip() == "":
            return default
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y", "是", "已确认"}:
            return True
        if normalized in {"0", "false", "no", "n", "否", "未确认"}:
            return False
        raise InputError(f"row {position}: {name} must be true or false")

    for position, row in enumerate(values, start=1):
        if isinstance(row, str):
            row = {"address": row}
        if not isinstance(row, dict):
            raise InputError(f"row {position} is not an address object")
        address = str(row.get("address") or row.get("coldkey") or "").strip()
        if not address:
            raise InputError(f"row {position} has no address")
        category = str(row.get("category") or "WATCH").strip().upper()
        if category not in {"SEED", "WATCH", "BLACKLIST", "EXCHANGE", "VALIDATOR"}:
            raise InputError(
                f"row {position}: category must be SEED, WATCH, BLACKLIST, EXCHANGE or VALIDATOR"
            )
        result.append(
            {
                "address": address,
                "category": category,
                "label": str(row.get("label") or "").strip(),
                "note": str(row.get("note") or "").strip(),
                "exchange_confirmed": boolean(row, "exchange_confirmed"),
                "exchange_rejected": boolean(row, "exchange_rejected"),
            }
        )
    return result


class LinkService:
    def __init__(self, database: Database, rpc_url: str) -> None:
        self.database = database
        self.rpc_url = rpc_url
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._continuous = False

    def import_rows(self, rows: list[dict[str, Any]], source: str) -> dict[str, int]:
        inserted = updated = skipped = 0
        with self.database.session() as db:
            for position, row in enumerate(rows, start=1):
                address = str(row["address"]).strip()
                if not is_valid_ss58(address):
                    raise InputError(f"row {position}: invalid SS58 address")
                current = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == address)
                )
                if current is None:
                    category = row.get("category", "WATCH").upper()
                    rejected = bool(row.get("exchange_rejected", False))
                    confirmed = bool(
                        row.get("exchange_confirmed", category == "EXCHANGE")
                    )
                    if rejected:
                        confirmed = False
                    db.add(
                        AddressLibrary(
                            address=address,
                            label=row.get("label", ""),
                            category=category,
                            note=row.get("note", ""),
                            source=source,
                            exchange_confirmed=(category == "EXCHANGE" and confirmed),
                            exchange_rejected=(category == "EXCHANGE" and rejected),
                        )
                    )
                    inserted += 1
                    continue
                changed = False
                for field in ("label", "category", "note"):
                    incoming = row.get(field, "")
                    if incoming and getattr(current, field) != incoming:
                        setattr(current, field, incoming)
                        changed = True
                if current.category == "EXCHANGE":
                    rejected = bool(row.get("exchange_rejected", current.exchange_rejected))
                    confirmed = bool(row.get("exchange_confirmed", current.exchange_confirmed))
                    if rejected:
                        confirmed = False
                    if current.exchange_confirmed != confirmed:
                        current.exchange_confirmed = confirmed
                        changed = True
                    if current.exchange_rejected != rejected:
                        current.exchange_rejected = rejected
                        changed = True
                incoming_source = row.get("source", "")
                if incoming_source and incoming_source.startswith("taostats:"):
                    if current.source != incoming_source:
                        current.source = incoming_source
                        changed = True
                if changed:
                    updated += 1
                else:
                    skipped += 1
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    def import_upload(self, filename: str, text: str) -> dict[str, int]:
        return self.import_rows(_parse_upload(filename, text), filename)

    def import_path(self, path: Path) -> dict[str, int]:
        parsed = read_addresses(path)
        rows = [
            {
                "address": row.address,
                "category": row.category,
                "label": row.label,
                "note": row.note,
            }
            for row in parsed
        ]
        return self.import_rows(rows, path.name)

    def save_note(self, address: str, note: str) -> dict[str, Any]:
        """Save a user note without changing an existing library category."""

        address = address.strip()
        note = note.strip()
        if not is_valid_ss58(address):
            raise InputError("invalid SS58 address")
        with self.database.session() as db:
            current = db.scalar(
                select(AddressLibrary).where(AddressLibrary.address == address)
            )
            created = False
            if current is None:
                if not note:
                    return {"saved": True, "created": False, "item": None}
                current = AddressLibrary(
                    address=address,
                    label="",
                    category="WATCH",
                    note=note,
                    source="relation-note",
                )
                db.add(current)
                db.flush()
                created = True
            else:
                current.note = note
            return {"saved": True, "created": created, "item": address_dict(current)}

    def update_library_note(self, row_id: int, note: str) -> dict[str, Any]:
        with self.database.session() as db:
            current = db.get(AddressLibrary, row_id)
            if current is None:
                raise InputError("address not found")
            current.note = note.strip()
            return {"saved": True, "item": address_dict(current)}

    def decide_exchange(self, row_id: int, decision: str) -> dict[str, Any]:
        with self.database.session() as db:
            current = db.get(AddressLibrary, row_id)
            if current is None:
                raise InputError("address not found")
            if current.category != "EXCHANGE":
                raise InputError("只有 EXCHANGE 分类地址可以确认交易所身份")
            if decision not in {"confirm", "reject", "reset"}:
                raise InputError("交易所状态只能是 confirm、reject 或 reset")
            current.exchange_confirmed = decision == "confirm"
            current.exchange_rejected = decision == "reject"
            return {"saved": True, "item": address_dict(current)}

    def confirm_exchange(self, row_id: int, confirmed: bool = True) -> dict[str, Any]:
        # Backward-compatible endpoint behavior: false clears a prior
        # confirmation and returns the row to the pending state.
        return self.decide_exchange(row_id, "confirm" if confirmed else "reset")

    def decide_exchange_address(
        self,
        address: str,
        decision: str,
        label: str = "人工确认交易所",
    ) -> dict[str, Any]:
        address = address.strip()
        if not is_valid_ss58(address):
            raise InputError("invalid SS58 address")
        with self.database.session() as db:
            current = db.scalar(select(AddressLibrary).where(AddressLibrary.address == address))
            if decision not in {"confirm", "reject", "reset"}:
                raise InputError("交易所状态只能是 confirm、reject 或 reset")
            if current is None and decision == "reset":
                return {"saved": True, "item": None}
            if current is None:
                current = AddressLibrary(
                    address=address,
                    label=(label.strip() or (
                        "人工确认交易所" if decision == "confirm" else "人工否定交易所"
                    )),
                    category="EXCHANGE",
                    note=(
                        "由关联扫描结果人工确认"
                        if decision == "confirm"
                        else "由关联扫描结果人工否定交易所候选"
                    ),
                    source=(
                        "manual:exchange-confirmation"
                        if decision == "confirm"
                        else "manual:exchange-rejection"
                    ),
                    exchange_confirmed=decision == "confirm",
                    exchange_rejected=decision == "reject",
                )
                db.add(current)
                db.flush()
            else:
                current.category = "EXCHANGE"
                current.label = label.strip() or current.label or (
                    "人工确认交易所" if decision == "confirm" else "人工否定交易所"
                )
                current.source = (
                    "manual:exchange-confirmation"
                    if decision == "confirm"
                    else "manual:exchange-rejection"
                )
                current.exchange_confirmed = decision == "confirm"
                current.exchange_rejected = decision == "reject"
            return {"saved": True, "item": address_dict(current)}

    def confirm_exchange_address(self, address: str, label: str = "人工确认交易所") -> dict[str, Any]:
        return self.decide_exchange_address(address, "confirm", label)

    def sync_exchange_addresses(self) -> dict[str, Any]:
        """Import the provider's public exchange directory into the library."""

        try:
            rows, from_cache = get_exchange_addresses()
        except (TaostatsAuthError, TaostatsRateLimitError, TaostatsUnavailableError) as exc:
            if getattr(exc, "request_made", False):
                self.database.record_api_request(
                    "/v1/exchange-addresses",
                    status_code=getattr(exc, "status_code", None),
                    success=False,
                )
            raise
        if not from_cache:
            self.database.record_api_request("/v1/exchange-addresses", success=True)
        source = "taostats:/v1/exchange-addresses"
        valid_rows: list[dict[str, str]] = []
        invalid_count = 0
        for row in rows:
            address = row.get("address", "").strip()
            if not is_valid_ss58(address):
                invalid_count += 1
                continue
            valid_rows.append(
                {
                    "address": address,
                    "category": "EXCHANGE",
                    "label": row.get("name", "").strip(),
                    "note": "",
                    "source": source,
                    "exchange_confirmed": True,
                }
            )
        result: dict[str, Any] = self.import_rows(valid_rows, source)
        result.update(
            {
                "provider_count": len(rows),
                "invalid_count": invalid_count,
                "from_cache": from_cache,
                "source": source,
            }
        )
        return result

    def library(self) -> list[dict[str, Any]]:
        with self.database.session() as db:
            rows = db.scalars(
                select(AddressLibrary).order_by(
                    AddressLibrary.category, AddressLibrary.label, AddressLibrary.id
                )
            ).all()
            return [address_dict(row) for row in rows]

    def export_library(self, file_format: str) -> tuple[str, str]:
        """Return a portable address-library backup and its media type."""

        if file_format not in {"csv", "json"}:
            raise InputError("library export format must be csv or json")
        portable = [
            {
                "address": item["address"],
                "label": item["label"],
                "category": item["category"],
                "note": item["note"],
                "exchange_confirmed": item["exchange_confirmed"],
                "exchange_rejected": item["exchange_rejected"],
            }
            for item in self.library()
        ]
        if file_format == "json":
            import json

            payload = {
                "format": "bittensor-address-library",
                "schema_version": 1,
                "addresses": portable,
            }
            return (
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                "application/json; charset=utf-8",
            )

        import csv
        import io

        output = io.StringIO(newline="")
        fields = [
            "address",
            "label",
            "category",
            "note",
            "exchange_confirmed",
            "exchange_rejected",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(portable)
        return output.getvalue(), "text/csv; charset=utf-8"

    # -- 关联查询历史 -----------------------------------------------------
    def archive_relation(
        self,
        result: dict[str, Any],
        *,
        job_id: str = "",
        kind: str = "job",
        status: str = "complete",
        rounds: int = 0,
        requests: int = 0,
    ) -> int:
        """把一次关联查询的结果存进历史记录，返回记录 id。"""

        snapshot = dict(result)
        # 用量统计每次都会变，历史页展示没有意义，也省一份体积。
        snapshot.pop("api_usage", None)
        payload = json.dumps(snapshot, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_ARCHIVE_PAYLOAD_BYTES:
            snapshot.pop("edges", None)
            snapshot["edges_omitted"] = True
            payload = json.dumps(snapshot, ensure_ascii=False)
        related = result.get("related") or []
        hit_count = len(result.get("library_matches") or []) + len(
            result.get("seed_library_matches") or []
        )
        with self.database.session() as db:
            row = RelationArchive(
                job_id=job_id,
                kind=kind,
                seed=str(result.get("seed") or ""),
                status=status,
                data_source=str(result.get("data_source") or ""),
                depth=int(result.get("max_depth") or 0),
                rounds=int(rounds),
                requests=int(requests),
                related_count=len(related),
                library_hit_count=hit_count,
                payload=payload,
                finished_at=utcnow(),
            )
            db.add(row)
            db.flush()
            archive_id = row.id
            self._trim_archives(db)
        return archive_id

    @staticmethod
    def _trim_archives(db) -> None:
        """按条数和总体积淘汰最旧的记录，避免历史页把数据库撑爆。"""

        rows = (
            db.query(RelationArchive.id, func.length(RelationArchive.payload))
            .order_by(RelationArchive.id.desc())
            .all()
        )
        total_bytes = 0
        stale: list[int] = []
        for index, (row_id, size) in enumerate(rows):
            total_bytes += int(size or 0)
            if index >= MAX_ARCHIVE_ROWS or total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                stale.append(row_id)
        if stale:
            db.query(RelationArchive).filter(RelationArchive.id.in_(stale)).delete(
                synchronize_session=False
            )

    @staticmethod
    def _archive_summary(row: RelationArchive) -> dict[str, Any]:
        return {
            "id": row.id,
            "job_id": row.job_id,
            "kind": row.kind,
            "seed": row.seed,
            "status": row.status,
            "data_source": row.data_source,
            "depth": row.depth,
            "rounds": row.rounds,
            "requests": row.requests,
            "related_count": row.related_count,
            "library_hit_count": row.library_hit_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }

    def relation_history(self, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self.database.session() as db:
            total = db.query(RelationArchive).count()
            rows = (
                db.query(RelationArchive)
                .order_by(RelationArchive.id.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": [self._archive_summary(row) for row in rows],
            }

    def _load_archive(self, archive_id: int) -> RelationArchive:
        with self.database.session() as db:
            row = db.get(RelationArchive, archive_id)
            if row is None:
                raise InputError("历史记录不存在")
            return row

    def relation_history_detail(self, archive_id: int) -> dict[str, Any]:
        row = self._load_archive(archive_id)
        try:
            payload = json.loads(row.payload) if row.payload else None
        except ValueError:
            payload = None
        return {"item": self._archive_summary(row), "result": payload}

    def delete_relation_history(self, archive_ids: list[int]) -> int:
        ids = {item for item in archive_ids if item > 0}
        if not ids:
            return 0
        with self.database.session() as db:
            return int(
                db.query(RelationArchive)
                .filter(RelationArchive.id.in_(ids))
                .delete(synchronize_session=False)
            )

    def clear_relation_history(self) -> int:
        with self.database.session() as db:
            return int(
                db.query(RelationArchive).delete(synchronize_session=False)
            )

    def export_relation_history(
        self, archive_id: int, file_format: str = "txt"
    ) -> tuple[str, str, str]:
        """导出历史记录的关联地址（TXT）或完整结果（JSON）。"""

        if file_format not in {"txt", "json"}:
            raise InputError("导出格式只能是 txt 或 json")
        row = self._load_archive(archive_id)
        if file_format == "json":
            return (
                (row.payload or "{}") + "\n",
                "application/json; charset=utf-8",
                f"bittensor-link-archive-{archive_id}.json",
            )
        try:
            payload = json.loads(row.payload) if row.payload else {}
        except ValueError as exc:
            raise InputError("历史记录内容已损坏，无法导出地址") from exc
        selection = select_exportable_addresses(payload.get("related") or [])
        date = (row.finished_at or row.created_at).strftime("%Y%m%d")
        return (
            "\n".join(selection["addresses"]) + ("\n" if selection["addresses"] else ""),
            "text/plain; charset=utf-8",
            f"bittensor-addresses-{row.seed[:8]}-{date}.txt",
        )

    # -- 关联地址同步进地址库 ---------------------------------------------
    def sync_relation_addresses(
        self,
        *,
        seed: str,
        addresses: list[str],
        include_seed: bool = True,
        category: str = "WATCH",
        label: str = "",
        note: str = "",
        only_library_matched: bool = False,
    ) -> dict[str, Any]:
        """把关联地址和发起地址去重写入地址库。

        已存在的地址一律跳过，不覆盖原有分类、标签和备注；这样反复同步同一批
        地址也不会把人工维护过的记录改掉。
        """

        seed = (seed or "").strip()
        if seed and not is_valid_ss58(seed):
            raise InputError("发起地址不是合法的 SS58 地址")
        category = (category or "WATCH").upper()
        if category not in SYNC_CATEGORIES:
            raise InputError(f"分类只能是 {'、'.join(sorted(SYNC_CATEGORIES))}")
        if len(addresses) > MAX_SYNC_ADDRESSES:
            raise InputError(f"单次最多同步 {MAX_SYNC_ADDRESSES} 个地址")

        candidates: list[str] = []
        seen: set[str] = set()
        invalid = 0

        def push(value: str) -> None:
            nonlocal invalid
            value = (value or "").strip()
            if not value or value in seen:
                return
            if not is_valid_ss58(value):
                invalid += 1
                return
            seen.add(value)
            candidates.append(value)

        if include_seed and seed:
            push(seed)
        for address in addresses or []:
            push(address)
        related = [value for value in candidates if value != seed]

        with self.database.session() as db:
            existing: set[str] = set()
            for index in range(0, len(candidates), 400):
                chunk = candidates[index : index + 400]
                existing.update(
                    db.scalars(
                        select(AddressLibrary.address).where(
                            AddressLibrary.address.in_(chunk)
                        )
                    ).all()
                )
            matched = sum(1 for value in related if value in existing)
            seed_in_library = bool(seed) and seed in existing
            if only_library_matched:
                # 「命中即同步」：只保留已经在库里的关联地址和发起地址。
                # 命中地址本来就在库里会被跳过，实际新增的通常只有发起地址。
                keep = [value for value in related if value in existing]
                if include_seed and seed:
                    keep.insert(0, seed)
                candidates = list(dict.fromkeys(keep))
            if not candidates:
                return {
                    "total": 0,
                    "inserted": 0,
                    "existing": 0,
                    "matched": matched,
                    "seed_in_library": seed_in_library,
                    "invalid": invalid,
                    "category": category,
                    "label": label,
                    "added": [],
                }

            default_label = label.strip() or f"关联同步 · {seed[:8] or '未命名'}"
            note_text = note.strip() or (
                f"由关联扫描同步：发起地址 {seed}" if seed else "由关联扫描同步"
            )
            source = f"relation-sync:{seed[:8]}" if seed else "relation-sync"
            added: list[dict[str, Any]] = []
            inserted = 0
            for address in candidates:
                if address in existing:
                    continue
                db.add(
                    AddressLibrary(
                        address=address,
                        label=default_label,
                        category=category,
                        note=note_text,
                        source=source,
                        exchange_confirmed=category == "EXCHANGE",
                        exchange_rejected=False,
                    )
                )
                inserted += 1
                if len(added) < 20:
                    added.append({"address": address, "category": category})
            return {
                "total": len(candidates),
                "inserted": inserted,
                "existing": len(candidates) - inserted,
                "matched": matched,
                "seed_in_library": seed_in_library,
                "invalid": invalid,
                "category": category,
                "label": default_label,
                "added": added,
            }

    def remove_address(self, row_id: int) -> bool:
        with self.database.session() as db:
            row = db.get(AddressLibrary, row_id)
            if row is None:
                return False
            db.delete(row)
            return True

    def remove_addresses(self, row_ids: list[int]) -> int:
        ids = {row_id for row_id in row_ids if row_id > 0}
        if not ids:
            return 0
        with self.database.session() as db:
            return int(
                db.query(AddressLibrary)
                .filter(AddressLibrary.id.in_(ids))
                .delete(synchronize_session=False)
            )

    def seed_address(self) -> str | None:
        with self.database.session() as db:
            return db.scalar(
                select(AddressLibrary.address).where(
                    AddressLibrary.category == "SEED"
                ).order_by(AddressLibrary.id)
            )

    def index_summary(self) -> dict[str, Any]:
        with self.database.session() as db:
            count = db.scalar(select(func.count(Transfer.id))) or 0
            first = db.scalar(select(func.min(Transfer.block_number)))
            last = db.scalar(select(func.max(Transfer.block_number)))
            cursor = db.scalar(
                select(ChainCursor).where(ChainCursor.rpc_url == self.rpc_url)
            )
            latest = db.scalars(
                select(ScanJob).order_by(ScanJob.id.desc()).limit(6)
            ).all()
            return {
                "transfer_count": count,
                "first_block": first,
                "last_block": last,
                "cursor_block": cursor.last_block if cursor else None,
                "cursor_hash": cursor.last_hash if cursor else None,
                "coverage": "LOCAL_INDEX",
                "jobs": [job_dict(row) for row in latest],
            }

    def scan(self, blocks: int = 2, advance: bool = False) -> dict[str, Any]:
        blocks = max(1, min(int(blocks), MAX_MANUAL_SCAN_BLOCKS))
        with self.database.session() as db:
            now = datetime.now(timezone.utc)
            # Acquire a SQLite write reservation before checking or creating the
            # lease. This makes first-time lease creation atomic across workers.
            db.rollback()  # Release any SQLAlchemy transaction opened earlier.
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            lock = db.get(ScanLock, "chain-scan")
            lease = lock.lease_until.replace(tzinfo=timezone.utc) if lock else None
            if lock is not None and lease is not None and lease >= now:
                raise ScanBusyError("a scan job is already running")
            cursor = db.scalar(
                select(ChainCursor).where(ChainCursor.rpc_url == self.rpc_url)
            )
            last = cursor.last_block if cursor else None
            if lock is None:
                lock = ScanLock(lock_name="chain-scan", lease_until=now + timedelta(minutes=3))
                db.add(lock)
            else:
                lock.owner_job = None
                lock.lease_until = now + timedelta(minutes=3)
            # Commit creates a unique SQLite row before scanning starts. Other
            # worker processes see the lease and cannot start a concurrent scan.
            db.flush()
            job = ScanJob(
                rpc_url=self.rpc_url,
                status="running",
                start_block=(last + 1 if last is not None else 0),
            )
            db.add(job)
            db.flush()
            job_id = job.id
            lock.owner_job = job_id
        try:
            collector = RPCCollector(self.rpc_url)
            # Fetch current head once to set the default scan range, then use a
            # dedicated collection call. This keeps each request bounded.
            import httpx

            http_url = self.rpc_url.replace("wss://", "https://", 1).replace(
                "ws://", "http://", 1
            )
            response = httpx.post(
                http_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "chain_getHeader",
                    "params": [],
                },
                timeout=25,
            ).json()
            head = int(response["result"]["number"], 16)

            def commit_batch(rows: list[dict[str, Any]], block_number: int, block_hash: str) -> None:
                inserted = self.database.ingest_transfers(rows)
                with self.database.session() as db:
                    row = db.get(ScanJob, job_id)
                    if row:
                        row.head_block = head
                        row.scanned_blocks += 1
                        row.transfer_count += inserted
                        row.end_block = block_number
                    cursor = db.get(ChainCursor, self.rpc_url)
                    if cursor is None:
                        cursor = ChainCursor(rpc_url=self.rpc_url)
                        db.add(cursor)
                    cursor.last_block = block_number
                    cursor.last_hash = block_hash
                    cursor.updated_at = datetime.now(timezone.utc)
                    lock = db.get(ScanLock, "chain-scan")
                    if lock is not None and lock.owner_job == job_id:
                        lock.lease_until = datetime.now(timezone.utc) + timedelta(minutes=3)

            start = last + 1 if advance and last is not None else max(1, head - blocks + 1)
            end = min(head, start + blocks - 1)

            async def run_collector() -> dict[str, Any]:
                async def on_batch(rows, block_number, block_hash):
                    commit_batch(rows, block_number, block_hash)

                return await collector.collect_range(
                    start=start, end=end, on_batch=on_batch
                )

            result = asyncio.run(run_collector())
            with self.database.session() as db:
                row = db.get(ScanJob, job_id)
                if row:
                    row.status = "complete"
                    row.head_block = result["head_block"]
                    row.start_block = result["start_block"]
                    row.end_block = result["end_block"]
                    row.scanned_blocks = result["scanned_blocks"]
                    row.transfer_count = result["transfer_count"]
                    row.finished_at = datetime.now(timezone.utc)
                    lock = db.get(ScanLock, "chain-scan")
                    if lock is not None and lock.owner_job == job_id:
                        lock.owner_job = None
                        lock.lease_until = datetime.now(timezone.utc)
                    return job_dict(row)
            return {**result, "id": job_id, "status": "complete"}
        except Exception as exc:
            with self.database.session() as db:
                row = db.get(ScanJob, job_id)
                if row:
                    row.status = "failed"
                    row.error = f"{type(exc).__name__}: {exc}"
                    row.finished_at = datetime.now(timezone.utc)
                    lock = db.get(ScanLock, "chain-scan")
                    if lock is not None and lock.owner_job == job_id:
                        lock.owner_job = None
                        lock.lease_until = datetime.now(timezone.utc)
            raise

    def prepare_relation_context(
        self,
        *,
        seed: str,
        depth: int,
        min_edge_tao: str,
        min_total_tao: str,
        include_incoming: bool = True,
        include_outgoing: bool = True,
        data_source: str = "local",
        filter_exchanges: bool = False,
        filter_validators: bool = False,
    ) -> RelationContext:
        """收集一次查询中不随轮次变化的部分：地址库、本地索引、过滤名单。"""

        if data_source not in {"local", "taostats"}:
            raise ValueError("data_source must be local or taostats")
        policy = LinkPolicy.from_dict(
            {
                "min_edge_tao": min_edge_tao,
                "min_total_tao": min_total_tao,
                "include_incoming": include_incoming,
                "include_outgoing": include_outgoing,
            }
        )
        warnings: list[str] = []
        with self.database.session() as db:
            library = [
                LibraryEntry(
                    address=row.address,
                    label=address_dict(row)["label"],
                    category=row.category,
                    note=row.note,
                    source=row.source,
                    exchange_confirmed=bool(row.exchange_confirmed),
                    exchange_rejected=bool(row.exchange_rejected),
                )
                for row in db.scalars(select(AddressLibrary)).all()
            ]
            first_block = db.scalar(select(func.min(Transfer.block_number)))
            last_block = db.scalar(select(func.max(Transfer.block_number)))
            local_transfer_rows = to_relation_records(
                db.scalars(select(Transfer).order_by(Transfer.block_number)).all()
            )

        exchange_addresses = {
            entry.address
            for entry in library
            if entry.category == "EXCHANGE" and entry.exchange_confirmed
        }
        unconfirmed_exchange_addresses = {
            entry.address
            for entry in library
            if entry.category == "EXCHANGE" and not entry.exchange_confirmed
        }
        manual_validator_addresses = {
            entry.address for entry in library if entry.category == "VALIDATOR"
        }
        auto_validator_addresses: set[str] = set()
        validator_api_requests = 0
        validator_cache_hits = 0
        if filter_validators:
            try:
                auto_validator_addresses, from_cache = get_active_validator_addresses()
                if from_cache:
                    validator_cache_hits += 1
                else:
                    validator_api_requests += 1
                    self.database.record_api_request("/v1/validators/active", success=True)
            except (TaostatsAuthError, TaostatsRateLimitError, TaostatsUnavailableError) as exc:
                if getattr(exc, "request_made", False):
                    self.database.record_api_request(
                        "/v1/validators/active",
                        status_code=getattr(exc, "status_code", None),
                        success=False,
                    )
                warnings.append(f"自动验证者识别未完成：{exc}")
        excluded_addresses = set()
        if filter_exchanges:
            excluded_addresses.update(exchange_addresses)
        if filter_validators:
            excluded_addresses.update(manual_validator_addresses)
            excluded_addresses.update(auto_validator_addresses)
        if filter_exchanges and exchange_addresses:
            warnings.append(f"已启用交易所过滤，跳过 {len(exchange_addresses)} 个已人工确认地址。")
        if filter_exchanges and unconfirmed_exchange_addresses:
            warnings.append(
                f"有 {len(unconfirmed_exchange_addresses)} 个交易所候选尚未人工确认，已保留在结果中，未执行过滤。"
            )
        if filter_validators and (manual_validator_addresses or auto_validator_addresses):
            warnings.append(
                "已启用验证者过滤，跳过 "
                f"{len(manual_validator_addresses | auto_validator_addresses)} 个已识别地址"
                f"（手工 {len(manual_validator_addresses)}，自动 {len(auto_validator_addresses)}）。"
            )
        return RelationContext(
            seed=seed,
            depth=depth,
            data_source=data_source,
            policy=policy,
            filter_exchanges=filter_exchanges,
            filter_validators=filter_validators,
            library=library,
            first_block=first_block,
            last_block=last_block,
            local_transfer_rows=local_transfer_rows,
            warnings=warnings,
            exchange_addresses=exchange_addresses,
            unconfirmed_exchange_addresses=unconfirmed_exchange_addresses,
            manual_validator_addresses=manual_validator_addresses,
            auto_validator_addresses=auto_validator_addresses,
            excluded_addresses=excluded_addresses,
            validator_api_requests=validator_api_requests,
            validator_cache_hits=validator_cache_hits,
        )

    def new_crawl(self, context: RelationContext) -> TaostatsCrawl:
        """按上下文创建可续跑的爬取器（首次调用时才会排入起始地址）。"""

        state = CrawlState(
            seed=context.seed,
            depth=context.depth,
            policy=context.policy,
            terminal_exchange_addresses=set(context.exchange_addresses),
            excluded_addresses=set(context.excluded_addresses),
        )
        return TaostatsCrawl(
            state,
            fetch_transfer=lambda address, page: get_transfer_page(address, page),
            fetch_stake=lambda address, page: get_stake_transfer_page(address, page),
            record_request=self.database.record_api_request,
        )

    @staticmethod
    def pending_budget_note(crawl: TaostatsCrawl | None, *, validator_requests: int = 0) -> str:
        """只有真的把本轮额度用完、且还有待查任务时才提示「预算用尽」。

        鉴权失败或限流导致的中断不属于预算耗尽，由调用错误本身说明原因。
        """

        if crawl is None or not crawl.state.has_work:
            return ""
        if validator_requests + crawl.state.api_requests < MAX_TAOSTATS_REQUESTS_PER_QUERY:
            return ""
        parts = []
        if crawl.state.pending_transfer:
            parts.append(f"{crawl.state.pending_transfer} 个转账明细请求")
        if crawl.state.pending_stake:
            parts.append(f"{crawl.state.pending_stake} 个子网 stake 转移请求")
        return (
            f"本轮单次查询最多调用 {MAX_TAOSTATS_REQUESTS_PER_QUERY} 次 Taostats API，"
            "已按“一级关联转账 → 一级关联子网转移 → 更深层级”的优先级用完；"
            f"未执行：{'、'.join(parts)}。结果只包含已获取的部分历史。"
        )

    def relations(
        self,
        *,
        seed: str,
        depth: int,
        min_edge_tao: str,
        min_total_tao: str,
        include_incoming: bool = True,
        include_outgoing: bool = True,
        data_source: str = "local",
        filter_exchanges: bool = False,
        filter_validators: bool = False,
    ) -> dict[str, Any]:
        """单轮查询：只花掉一次请求预算，返回当前已取得的结果。"""

        context = self.prepare_relation_context(
            seed=seed,
            depth=depth,
            min_edge_tao=min_edge_tao,
            min_total_tao=min_total_tao,
            include_incoming=include_incoming,
            include_outgoing=include_outgoing,
            data_source=data_source,
            filter_exchanges=filter_exchanges,
            filter_validators=filter_validators,
        )
        crawl = None
        if data_source == "taostats" and seed not in context.excluded_addresses:
            crawl = self.new_crawl(context)
            budget = max(
                0, MAX_TAOSTATS_REQUESTS_PER_QUERY - context.validator_api_requests
            )
            crawl.run_round(budget)
        result = self.finalize_relations(
            context,
            crawl,
            pending_note=self.pending_budget_note(
                crawl, validator_requests=context.validator_api_requests
            ),
        )
        try:
            result["archive_id"] = self.archive_relation(
                result,
                kind="single",
                status="complete",
                rounds=1,
                requests=int(result.get("api_requests") or 0),
            )
        except Exception as exc:  # noqa: BLE001 - 记录失败不该让查询本身失败
            result["archive_id"] = None
            result.setdefault("warnings", []).append(f"查询记录保存失败：{exc}")
        return result

    def finalize_relations(
        self,
        context: RelationContext,
        crawl: TaostatsCrawl | None = None,
        *,
        pending_note: str = "",
        balance_budget_left: int | None = None,
    ) -> dict[str, Any]:
        """把上下文与已爬到的记录合成最终结果。"""

        seed = context.seed
        depth = context.depth
        policy = context.policy
        data_source = context.data_source
        library = context.library
        local_transfer_rows = context.local_transfer_rows
        exchange_addresses = context.exchange_addresses
        unconfirmed_exchange_addresses = context.unconfirmed_exchange_addresses
        manual_validator_addresses = context.manual_validator_addresses
        auto_validator_addresses = context.auto_validator_addresses
        excluded_addresses = context.excluded_addresses
        filter_exchanges = context.filter_exchanges
        filter_validators = context.filter_validators
        warnings = list(context.warnings)
        transfer_rows = list(local_transfer_rows)
        first_block = context.first_block
        last_block = context.last_block
        api_requests = context.validator_api_requests
        cache_hits = 0
        pagination_complete = True
        provider_window_truncated = False
        api_error = ""
        records_by_key: dict[str, Any] = {}
        if crawl is not None:
            state = crawl.state
            _extend_unique(warnings, state.warnings)
            records_by_key = state.records_by_key
            api_requests += state.api_requests
            cache_hits += state.cache_hits
            pagination_complete = state.pagination_complete
            provider_window_truncated = state.provider_window_truncated
            api_error = state.api_error
            if state.has_work:
                pagination_complete = False
                if pending_note:
                    _extend_unique(warnings, [pending_note])
            if api_error:
                _extend_unique(warnings, [api_error])
            remote_records = list(records_by_key.values())
            transfer_rows.extend(remote_records)
            if remote_records:
                remote_first = min(row.block_number for row in remote_records)
                remote_last = max(row.block_number for row in remote_records)
                first_block = remote_first if first_block is None else min(first_block, remote_first)
                last_block = remote_last if last_block is None else max(last_block, remote_last)
        if data_source == "local" and not transfer_rows:
            warnings.append("本地索引为空，请先扫描最新区块或导入索引数据。")
        if data_source == "taostats" and not records_by_key and not api_error:
            warnings.append("Taostats 本轮没有返回转账记录；请查看数据源状态和地址是否正确。")
        if data_source == "local":
            freshness = "LOCAL_INDEX" if local_transfer_rows else "EMPTY_INDEX"
            coverage = "LOCAL_INDEX" if local_transfer_rows else "NO_DATA"
        elif not pagination_complete:
            freshness = "TAOSTATS_PARTIAL" if records_by_key else (
                "LOCAL_FALLBACK" if local_transfer_rows else "TAOSTATS_UNAVAILABLE"
            )
            coverage = "TAOSTATS_AND_LOCAL" if records_by_key and local_transfer_rows else (
                "TAOSTATS_INDEX" if records_by_key else (
                    "LOCAL_INDEX" if local_transfer_rows else "NO_DATA"
                )
            )
        elif records_by_key:
            freshness = "TAOSTATS_INDEX"
            coverage = "TAOSTATS_AND_LOCAL" if local_transfer_rows else "TAOSTATS_INDEX"
        else:
            freshness = "LOCAL_INDEX" if local_transfer_rows else "EMPTY_INDEX"
            coverage = "LOCAL_INDEX" if local_transfer_rows else "NO_DATA"
        # 余额查询不占 Taostats 额度（走本地 RPC），但一律排在所有关联查询之后。
        # 本轮额度已被关联查询用尽时，只保留起始地址余额，其余余额留给下一轮。
        if balance_budget_left is None:
            balance_budget_left = MAX_TAOSTATS_REQUESTS_PER_QUERY - api_requests
        balance_budget_short = data_source == "taostats" and balance_budget_left <= 0
        detected_balance_candidates = [
            address
            for address in exchange_balance_candidates(transfer_rows, library)
            if is_valid_ss58(address)
        ]
        balance_candidates = (
            []
            if balance_budget_short
            else detected_balance_candidates[:MAX_EXCHANGE_BALANCE_CHECKS_PER_QUERY]
        )
        if balance_budget_short:
            if detected_balance_candidates:
                warnings.append(
                    "本轮 Taostats API 额度已全部用于关联查询，已跳过交易所候选余额检查；"
                    "本轮不会自动标记疑似交易所。"
                )
        elif len(detected_balance_candidates) > len(balance_candidates):
            warnings.append(
                f"本次仅检查前 {MAX_EXCHANGE_BALANCE_CHECKS_PER_QUERY} 个高频候选余额；其余地址不会自动标记疑似交易所。"
            )
        balances: dict[str, Any] = {}
        if balance_candidates:
            try:
                balances = query_account_balances(self.rpc_url, balance_candidates)
            except Exception as exc:
                warnings.append(f"交易所候选余额查询未完成：{exc}")
                balances = {}
        if balance_candidates and not balances:
            warnings.append(
                "检测到高频交易所候选，但未取得当前余额；系统不会据此自动标记疑似交易所。"
            )
        exchange_balance_checks = sum(
            value is not None for value in balances.values()
        )
        report = discover(
            seed,
            transfer_rows,
            library=library,
            max_depth=depth,
            policy=policy,
            data_cutoff_block=last_block,
            freshness=freshness,
            warnings=warnings,
            excluded_addresses=excluded_addresses,
            balances=balances,
        )
        if balance_budget_short:
            # 额度已用完：只读取起始地址的余额（单次 RPC），不扩展到关联地址。
            balance_addresses = (
                [seed] if is_valid_ss58(seed) and seed not in balances else []
            )
            if report.related:
                warnings.append(
                    "本轮 Taostats API 额度已全部用于关联查询，已跳过关联地址余额查询；"
                    "这些地址的余额栏会显示“未获取”。"
                )
        else:
            balance_addresses = list(dict.fromkeys(
                address
                for address in [seed, *(row.address for row in report.related)]
                if is_valid_ss58(address) and address not in balances
            ))
        if balance_addresses:
            try:
                balances.update(query_account_balances(self.rpc_url, balance_addresses))
            except Exception as exc:
                warnings.append(f"起始地址或关联地址的当前余额查询未完成：{exc}")
            if any(
                address not in balances or balances[address] is None
                for address in balance_addresses
            ):
                warnings.append(
                    "部分起始地址或关联地址未能读取当前余额；余额栏会显示“未获取”。"
                )
            report = discover(
                seed,
                transfer_rows,
                library=library,
                max_depth=depth,
                policy=policy,
                data_cutoff_block=last_block,
                freshness=freshness,
                warnings=warnings,
                excluded_addresses=excluded_addresses,
                balances=balances,
            )
        result = {
            "seed": report.seed,
            "seed_balance_tao": str(balances[seed]) if balances.get(seed) is not None else None,
            "max_depth": report.max_depth,
            "data_cutoff_block": report.data_cutoff_block,
            "data_first_block": first_block,
            "coverage": coverage,
            "data_source": data_source,
            "pagination_complete": pagination_complete if data_source == "taostats" else None,
            "provider_window_truncated": (
                provider_window_truncated if data_source == "taostats" else None
            ),
            "api_requests": api_requests,
            "api_request_budget": MAX_TAOSTATS_REQUESTS_PER_QUERY,
            "api_cache_hits": cache_hits,
            "validator_api_requests": context.validator_api_requests,
            "validator_cache_hits": context.validator_cache_hits,
            "filter_exchanges": filter_exchanges,
            "filter_validators": filter_validators,
            "exchange_addresses_filtered": len(exchange_addresses) if filter_exchanges else 0,
            "validator_addresses_filtered": (
                len(manual_validator_addresses | auto_validator_addresses)
                if filter_validators else 0
            ),
            "unconfirmed_exchange_candidates": len(unconfirmed_exchange_addresses),
            "exchange_balance_candidates": len(balance_candidates),
            "exchange_balance_checks": exchange_balance_checks,
            "local_transfer_count": len(local_transfer_rows),
            "taostats_transfer_count": (
                sum(1 for key in records_by_key if not key.startswith("stake:"))
                if data_source == "taostats" else 0
            ),
            "stake_transfer_count": sum(
                1 for row in transfer_rows if row.edge_type == "stake_transfer"
            ),
            "data_sources_used": (
                (["taostats"] if records_by_key else [])
                + (["local"] if local_transfer_rows else [])
            ),
            "freshness": report.freshness,
            "policy": {**report.policy.to_dict(), "min_edge_tao": str(policy.min_edge_tao), "min_total_tao": str(policy.min_total_tao)},
            "edges": [edge.__dict__ | {"amount_tao": str(edge.amount_tao)} for edge in report.edges],
            "related": [
                row.__dict__ | {
                    "incoming_tao": str(row.incoming_tao),
                    "outgoing_tao": str(row.outgoing_tao),
                    "total_tao": str(row.total_tao),
                    "balance_tao": str(row.balance_tao) if row.balance_tao is not None else None,
                }
                for row in report.related
            ],
            "library_matches": [
                row.__dict__ | {
                    "incoming_tao": str(row.incoming_tao),
                    "outgoing_tao": str(row.outgoing_tao),
                    "total_tao": str(row.total_tao),
                    "balance_tao": str(row.balance_tao) if row.balance_tao is not None else None,
                }
                for row in report.library_matches
            ],
            "seed_is_in_library": report.seed_is_in_library,
            "seed_library_matches": [
                {
                    "address": entry.address,
                    "label": entry.label,
                    "category": entry.category,
                    "note": entry.note,
                    "source": entry.source,
                    "exchange_confirmed": entry.exchange_confirmed,
                }
                for entry in report.seed_library_matches
            ],
            "api_usage": self.api_usage_summary(),
            "warnings": [
                warning
                for warning in report.warnings
                if warning != "No transfer records supplied; no relations can be discovered."
            ],
            "limitations": [
                (
                    "结果只覆盖本地索引区间和实际采集到的 Balances.Transfer 事件。"
                    if data_source == "local"
                    else "结果只覆盖 Taostats 本轮成功返回的分页和地址；免费额度或服务端分页窗口可能使三级关系不完整。"
                ),
                "转账关系只是行为证据，不等于同一地址、同一实体或真实身份。",
                "质押、子网 swap、代理调用等未纳入自动关联；需要单独建立语义。",
                "交易所疑似标记增加了当前 System.Account 可用余额和近 30 天时间条件；余额查询失败或余额偏低时不会自动标记为交易所。子网质押/解质押和购买事件仍未作为完整数据源接入，人工确认结果才允许过滤。",
            ],
        }
        return result

    def api_usage_summary(self) -> dict[str, Any]:
        return self.database.api_usage_summary(
            monthly_limit=PUBLIC_MONTHLY_CREDIT_REFERENCE,
            per_minute_limit=LOCAL_REQUESTS_PER_MINUTE,
        )
