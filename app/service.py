"""Address library import, audit and chain scan orchestration."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

from .collector import RPCCollector, query_account_balances
from .crawl import CrawlState, TaostatsCrawl
from .crawl import stake_counterparty as _stake_counterparty
from .db import (
    AddressBook,
    AddressCategory,
    AddressLibrary,
    ChainCursor,
    Database,
    ImportBatch,
    RelationArchive,
    ScanJob,
    ScanLock,
    SYSTEM_CLASSIFICATION,
    Transfer,
    install_seed_books,
    seed_book_status,
    to_relation_records,
    utcnow,
)
from . import license as license_mod
from . import interlink
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
# 结果合成阶段最多查这么多个地址的当前余额（起始地址优先，再按层级/金额）。
# 分轮续查跑十几轮后关联地址可能上百个，逐个走 RPC 会让结果迟迟出不来。
MAX_BALANCE_ADDRESSES_PER_RESULT = 40
PUBLIC_MONTHLY_CREDIT_REFERENCE = 10_000
# 历史记录只保留最近的若干条 / 总体积上限，避免一次重扫就把 SQLite 撑大。
MAX_ARCHIVE_ROWS = 500
MAX_ARCHIVE_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_SYNC_ADDRESSES = 5000
# 用户分类（大V / 群友 / 其他 + 自建）之外的系统分类：只用于“过滤交易所/
# 验证者”，不提供在分类下拉里给用户挑，避免关联地址被误标成公共地址。
SYSTEM_CATEGORIES = {"EXCHANGE", "VALIDATOR"}
SYSTEM_CATEGORY_LABELS = {"EXCHANGE": "交易所（系统）", "VALIDATOR": "验证者（系统）"}
# 「系统」是用户可选、可改的预置分类：与交易所一样能过滤，并且始终是关联终点。
SYSTEM_CLASSIFICATION_KEYS = {SYSTEM_CLASSIFICATION, "SYSTEM"}
# 0.4.x 及以前的英文分类，导入旧备份或旧 CSV 时自动折算到“其他”。
LEGACY_CATEGORY_MAP = {"WATCH": "其他", "SEED": "其他", "BLACKLIST": "其他"}
# 自建分类不能占用这些名字，否则迁移和系统判断会混乱。
RESERVED_CATEGORY_NAMES = {
    "WATCH",
    "SEED",
    "BLACKLIST",
    "EXCHANGE",
    "VALIDATOR",
    "SYSTEM",
    "ALL",
}
DEFAULT_CATEGORY = "其他"
CATEGORY_NAME_MAX_LENGTH = 12
BOOK_NAME_MAX_LENGTH = 60
MAX_BOOK_ADDRESSES = 5000


def normalize_category(value: str | None, *, allow_system: bool = True) -> str:
    """把输入折算成合法的分类名。

    兼容旧数据：WATCH / SEED / BLACKLIST 一律折成“其他”。系统分类
    （EXCHANGE / VALIDATOR）只在 ``allow_system`` 时保留，普通导入和同步
    不允许把地址标成公共地址。
    """

    raw = " ".join(str(value or "").split())
    if not raw:
        return DEFAULT_CATEGORY
    upper = raw.upper()
    # 「系统」是用户可选择的预置分类，任何导入/同步路径都允许。
    if upper in SYSTEM_CLASSIFICATION_KEYS:
        return SYSTEM_CLASSIFICATION
    if upper in LEGACY_CATEGORY_MAP:
        return LEGACY_CATEGORY_MAP[upper]
    if upper in SYSTEM_CATEGORIES:
        return upper if allow_system else DEFAULT_CATEGORY
    return raw[:CATEGORY_NAME_MAX_LENGTH]


def category_label(category: str) -> str:
    return SYSTEM_CATEGORY_LABELS.get(category, category)


def canonical_category(value: str | None) -> str:
    """多分类字段里的分类名归一（SYSTEM 与「系统」视为同一个）。"""

    name = " ".join(str(value or "").split())
    return SYSTEM_CLASSIFICATION if name.upper() in SYSTEM_CLASSIFICATION_KEYS else name


def row_categories(row: AddressLibrary) -> list[str]:
    """读取一条地址记录的全部分类，主分类永远排第一。"""

    raw = getattr(row, "categories", "") or ""
    parsed: list[Any] = []
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list):
                parsed = values
        except (TypeError, ValueError):
            parsed = []
    result: list[str] = []
    for value in [row.category, *parsed]:
        name = canonical_category(value)
        if name and name not in result:
            result.append(name)
    return result or [DEFAULT_CATEGORY]


def normalize_category_list(values: Iterable[Any]) -> list[str]:
    """人工编辑多分类：去重、限制数量，并拒绝 EXCHANGE/VALIDATOR 专用分类。"""

    result: list[str] = []
    for value in values or []:
        name = canonical_category(value)
        if not name or name.upper() in SYSTEM_CATEGORIES:
            continue
        name = normalize_category(name, allow_system=False)[:CATEGORY_NAME_MAX_LENGTH]
        if name not in result:
            result.append(name)
    return result[:10] or [DEFAULT_CATEGORY]


def encode_categories(categories: list[str]) -> str:
    return json.dumps(categories, ensure_ascii=False)



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
    removed_addresses: set[str] = field(default_factory=set)
    system_addresses: set[str] = field(default_factory=set)
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


def default_address_label(address: str) -> str:
    """标签没写时的默认名称：取地址后四位，方便在列表里一眼分辨。"""

    value = (address or "").strip()
    return value[-4:] if len(value) > 4 else value


def _library_row_snapshot(row: AddressLibrary) -> dict[str, Any]:
    """记录一条地址在批量导入前的字段，撤回时按原样还原。"""

    return {
        "label": row.label or "",
        "category": row.category or "",
        "categories": row.categories or "",
        "note": row.note or "",
        "book": getattr(row, "book", "") or "",
        "source": row.source or "",
        "exchange_confirmed": bool(row.exchange_confirmed),
        "exchange_rejected": bool(row.exchange_rejected),
    }


def _record_import_batch(
    db: Any,
    *,
    kind: str,
    summary: str,
    inserted: Iterable[str],
    updated: Iterable[dict[str, Any]],
    created_book: str | None = None,
    deleted: Iterable[dict[str, Any]] | None = None,
    removed_book: str | None = None,
    removed_books: Iterable[str] | None = None,
    created_books: Iterable[str] | None = None,
) -> None:
    """写入“撤回上次导入”的快照；只保留最新一条，空操作不覆盖上一条。

    ``deleted`` 是解散地址库时被整批删掉的地址快照，撤回时按它重建记录；
    ``removed_book`` 对应被删掉的库名；``removed_books`` 是合并群组时被并掉
    的多个库名；``created_books`` 是本次新建的多个库（重新导入预置库会一次
    建出好几个），撤回时一并删掉。
    """

    payload = json.dumps(
        {
            "inserted": list(inserted),
            "updated": list(updated),
            "deleted": list(deleted or []),
            "created_book": created_book or "",
            "created_books": list(created_books or []),
            "removed_book": removed_book or "",
            "removed_books": list(removed_books or []),
        },
        ensure_ascii=False,
    )
    for stale in db.scalars(select(ImportBatch)).all():
        db.delete(stale)
    db.flush()
    db.add(ImportBatch(id=1, kind=kind, summary=summary[:255], payload=payload))


def library_group_key(row: AddressLibrary) -> tuple[str, str]:
    """一条地址在「地址库清单」里属于哪个组。

    与前端 ``libraryTable`` 的归组口径保持一致：自建库（book）优先，
    其次是人工命名的标签组（label），最后是按分类兜底的未命名组。
    """

    book = (getattr(row, "book", "") or "").strip()
    if book:
        return ("book", book)
    label = (row.label or "").strip()
    if label:
        return ("label", label)
    return ("category", row.category)


def address_dict(row: AddressLibrary) -> dict[str, Any]:
    label = row.label.strip()
    return {
        "id": row.id,
        "address": row.address,
        # 未命名时展示地址后四位；label_custom 区分“人工命名”与“默认名”，
        # 地址库清单按它决定是否把同分类的未命名地址并成一组。
        "label": label or default_address_label(row.address),
        "label_custom": bool(label),
        "category": row.category,
        "category_label": category_label(row.category),
        "categories": row_categories(row),
        "note": row.note,
        "source": row.source,
        "book": getattr(row, "book", "") or "",
        "exchange_confirmed": bool(row.exchange_confirmed),
        "exchange_rejected": bool(row.exchange_rejected),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def select_exportable_addresses(
    rows: list[dict[str, Any]], *, depth: int | None = None
) -> dict[str, Any]:
    """从关联结果挑出可导出的地址。

    已人工确认的交易所地址和验证者地址不导出，避免把公共地址当成待跟进
    钱包继续扩散；疑似交易所（待确认）保留，方便人工核查。
    ``depth`` 为 1/2/3 时只保留该层级的关联地址（与结果页“层级筛选”口径
    一致，按地址首次被发现的层级算）；None 表示全部层级。
    """

    keep: list[str] = []
    seen: set[str] = set()
    skipped_exchange = 0
    skipped_validator = 0
    skipped_system = 0
    skipped_other_depth = 0
    for row in rows or []:
        if depth is not None:
            try:
                row_depth = int(row.get("depth") or 0)
            except (TypeError, ValueError):
                row_depth = 0
            if row_depth != int(depth):
                skipped_other_depth += 1
                continue
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
        categories = [str(value) for value in (row.get("library_categories") or [])]
        if any(
            canonical_category(value) == SYSTEM_CLASSIFICATION
            for value in [row.get("library_category") or "", *categories]
        ):
            skipped_system += 1
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
        "skipped_system": skipped_system,
        "skipped_other_depth": skipped_other_depth,
    }


def _parse_upload(filename: str, text: str) -> list[dict[str, str]]:
    import csv
    import io

    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return [
            {"address": address, "category": "", "label": "", "note": ""}
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
        category = str(row.get("category") or "").strip()
        raw_categories = row.get("categories")
        if isinstance(raw_categories, list):
            categories = [str(value).strip() for value in raw_categories if str(value).strip()]
        elif isinstance(raw_categories, str):
            categories = [value.strip() for value in raw_categories.split("|") if value.strip()]
        else:
            categories = []
        result.append(
            {
                "address": address,
                "category": category,
                "categories": categories,
                "label": str(row.get("label") or "").strip(),
                "note": str(row.get("note") or "").strip(),
                "book": str(row.get("book") or "").strip()[:BOOK_NAME_MAX_LENGTH],
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
        inserted_addresses: list[str] = []
        updated_snapshots: list[dict[str, Any]] = []
        with self.database.session() as db:
            for position, row in enumerate(rows, start=1):
                address = str(row["address"]).strip()
                if not is_valid_ss58(address):
                    raise InputError(f"row {position}: invalid SS58 address")
                raw_category = str(row.get("category") or "").strip()
                category = normalize_category(raw_category)
                if raw_category:
                    self._ensure_category(db, category)
                # 备份文件里的多分类（JSON 数组 / CSV 用 | 分隔）。
                incoming_categories = normalize_category_list(row.get("categories") or [])
                categories = (
                    incoming_categories if row.get("categories") else [category]
                )
                if categories[0] != category:
                    categories = [category, *[name for name in categories if name != category]]
                current = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == address)
                )
                if current is None:
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
                            categories=encode_categories(categories),
                            note=row.get("note", ""),
                            source=source,
                            book=str(row.get("book") or "")[:BOOK_NAME_MAX_LENGTH],
                            exchange_confirmed=(category == "EXCHANGE" and confirmed),
                            exchange_rejected=(category == "EXCHANGE" and rejected),
                        )
                    )
                    inserted += 1
                    inserted_addresses.append(address)
                    continue
                snapshot = _library_row_snapshot(current)
                changed = False
                incoming_book = str(row.get("book") or "").strip()
                if incoming_book and not current.book:
                    current.book = incoming_book[:BOOK_NAME_MAX_LENGTH]
                    changed = True
                for field in ("label", "note"):
                    incoming = row.get(field, "")
                    if incoming and getattr(current, field) != incoming:
                        setattr(current, field, incoming)
                        changed = True
                # 分类只在文件里明确写了才改，避免用纯地址 TXT 覆盖人工分类。
                if raw_category and current.category != category:
                    merged = [category, *[name for name in row_categories(current) if name != category]]
                    current.category = category
                    current.categories = encode_categories(merged)
                    changed = True
                elif row.get("categories"):
                    current.categories = encode_categories(categories)
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
                    updated_snapshots.append({"address": address, "snapshot": snapshot})
                else:
                    skipped += 1
            if inserted or updated:
                if source == "manual":
                    summary = f"手动添加地址 · 新增 {inserted} 个、更新 {updated} 个"
                else:
                    summary = f"导入「{Path(source).stem}」· 新增 {inserted} 个、更新 {updated} 个"
                _record_import_batch(
                    db,
                    kind="import",
                    summary=summary,
                    inserted=inserted_addresses,
                    updated=updated_snapshots,
                )
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    def import_upload(self, filename: str, text: str) -> dict[str, int]:
        return self.import_rows(_parse_upload(filename, text), filename)

    def add_group_addresses(
        self,
        addresses: str,
        *,
        book: str = "",
        label: str = "",
        category: str = "",
        note: str = "",
        move_existing: bool = False,
    ) -> dict[str, Any]:
        """把手动输入的地址加进某个标签组（自建库 / 标签组 / 分类兜底组）。

        默认只新增：已存在的地址跳过，不覆盖它原有的分类、组名和备注，
        避免“往这组加一条”把别处维护好的记录改掉；被跳过的地址若本来在
        别的组，会出现在 ``conflicts`` 里，前端据此提示“要移过来吗”。

        ``move_existing=True`` 时，已在别组的地址会移到本组（只改组名/归属，
        备注保留，目标组是分类组时才改分类），可撤回。

        往「人工确认交易所 / 人工否定交易所」这两个系统语义组里加地址，
        会同步设置交易所人工结论——组名本身就代表这个结论，否则加进去后
        扫描时并不会把它当交易所过滤。
        """

        tokens = [
            token
            for token in re.split(r"[\s,，;；、]+", str(addresses or "").strip())
            if token
        ]
        if not tokens:
            raise InputError("请先填写要添加的地址")
        unique: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for address in tokens:
            if address in seen:
                continue
            seen.add(address)
            if is_valid_ss58(address):
                unique.append(address)
            else:
                invalid.append(address)
        if not unique:
            raise InputError(f"没有有效的 SS58 地址（{len(invalid)} 个格式不对）")

        book_name = (book or "").strip()[:BOOK_NAME_MAX_LENGTH]
        group_label = (label or "").strip()[:160]
        group_category = normalize_category(category) if (category or "").strip() else ""
        group_note = (note or "").strip()[:2000]
        # 目标组：与前端地址库清单的归组口径一致（自建库 → 人工命名标签 → 分类兜底）。
        target = (
            ("book", book_name)
            if book_name
            else ("label", group_label)
            if group_label
            else ("category", group_category)
            if group_category
            else ("", "")
        )
        # 「人工确认交易所」这类组名自带交易所结论，加进去就同步标志位。
        semantic = book_name or group_label
        force_confirmed = semantic == "人工确认交易所"
        force_rejected = semantic == "人工否定交易所"
        inserted: list[str] = []
        moved: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        skipped = 0
        with self.database.session() as db:
            if group_category:
                self._ensure_category(db, group_category)
            for address in unique:
                current = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == address)
                )
                if current is None:
                    primary = group_category or "其他"
                    db.add(
                        AddressLibrary(
                            address=address,
                            label=group_label,
                            category=primary,
                            categories=encode_categories([primary]),
                            note=group_note,
                            source="manual:group-add",
                            book=book_name,
                            exchange_confirmed=force_confirmed,
                            exchange_rejected=force_rejected,
                        )
                    )
                    inserted.append(address)
                    continue
                current_group = library_group_key(current)
                if current_group == target:
                    # 本来就在这一组：重复贴进来不算错，静默算作跳过。
                    skipped += 1
                    continue
                if not move_existing:
                    skipped += 1
                    conflicts.append(
                        {
                            "address": address,
                            "group": current_group[1],
                            "kind": current_group[0],
                        }
                    )
                    continue
                snapshot = {
                    "label": current.label or "",
                    "category": current.category,
                    "categories": getattr(current, "categories", "") or "",
                    "note": current.note or "",
                    "book": getattr(current, "book", "") or "",
                    "source": current.source or "",
                    "exchange_confirmed": bool(current.exchange_confirmed),
                    "exchange_rejected": bool(current.exchange_rejected),
                }
                if target[0] == "book":
                    current.book = book_name
                    current.label = group_label or book_name
                elif target[0] == "label":
                    current.book = ""
                    current.label = group_label
                elif target[0] == "category":
                    current.book = ""
                    current.label = ""
                # 目标组带分类时同步分类（前端会把组的主分类一起传上来）；
                # 自建库/标签组的分类不动原记录的情况只在没传分类时发生。
                if group_category:
                    current.category = group_category
                    current.categories = encode_categories([group_category])
                if force_confirmed or force_rejected:
                    current.exchange_confirmed = force_confirmed
                    current.exchange_rejected = force_rejected
                    current.source = (
                        "manual:exchange-confirmation"
                        if force_confirmed
                        else "manual:exchange-rejection"
                    )
                moved.append(
                    {"address": address, "snapshot": snapshot, "from": current_group[1]}
                )
            if inserted or moved:
                group_name = book_name or group_label or group_category or "未命名"
                parts = []
                if inserted:
                    parts.append(f"新增 {len(inserted)} 个")
                if moved:
                    parts.append(f"转入 {len(moved)} 个")
                _record_import_batch(
                    db,
                    kind="import",
                    summary=f"手动添加到「{group_name}」· " + " / ".join(parts),
                    inserted=inserted,
                    updated=[{"address": item["address"], "snapshot": item["snapshot"]} for item in moved],
                )
        return {
            "inserted": len(inserted),
            "moved": len(moved),
            "skipped": skipped,
            "conflicts": conflicts[:20],
            "conflict_count": len(conflicts),
            "invalid": invalid[:20],
            "invalid_count": len(invalid),
            "group": book_name or group_label or group_category,
        }

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
                    category=DEFAULT_CATEGORY,
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

    def update_library_label(self, row_id: int, label: str) -> dict[str, Any]:
        """人工改名；传空字符串表示恢复默认（地址后四位）。"""

        value = " ".join(str(label or "").split())[:160]
        with self.database.session() as db:
            current = db.get(AddressLibrary, row_id)
            if current is None:
                raise InputError("address not found")
            current.label = value
            return {"saved": True, "item": address_dict(current)}

    def rename_label_group(self, old_label: str, new_label: str) -> dict[str, Any]:
        """整组改名：把同一个标签组下的地址一起改成新名字。"""

        old = " ".join(str(old_label or "").split())
        new = " ".join(str(new_label or "").split())
        if not old:
            raise InputError("原标签组名不能为空")
        if not new:
            raise InputError("新标签组名不能为空")
        if len(new) > 160:
            raise InputError("标签组名不能超过 160 个字符")
        with self.database.session() as db:
            rows = db.scalars(
                select(AddressLibrary).where(AddressLibrary.label == old)
            ).all()
            if not rows:
                raise InputError("没有找到这个标签组")
            for row in rows:
                row.label = new
            return {"renamed": len(rows), "label": new}

    def rename_address_book(self, old_name: str, new_name: str) -> dict[str, Any]:
        """自建地址库改名：库名、库内地址的 book 与同名标签一起更新。"""

        old = self._clean_book_name(old_name)
        new = self._clean_book_name(new_name)
        if old == new:
            return {"renamed": 0, "name": new}
        with self.database.session() as db:
            registry = db.scalar(select(AddressBook).where(AddressBook.name == old))
            if registry is None:
                raise InputError("地址库不存在")
            if db.scalar(select(AddressBook).where(AddressBook.name == new)) is not None:
                raise InputError("已有同名地址库")
            rows = db.scalars(
                select(AddressLibrary).where(AddressLibrary.book == old)
            ).all()
            registry.name = new
            for row in rows:
                row.book = new
                if row.label == old:
                    row.label = new
            return {"renamed": len(rows), "name": new}

    def merge_address_groups(
        self,
        sources: list[dict[str, str]],
        target_name: str,
    ) -> dict[str, Any]:
        """把几个地址库/标签组合并成一个库。

        使用场景：同一个种子扫出来的地址分散在几个库里，交叉重复命中，
        其实是一伙人。合并后统一挂在 ``target_name`` 名下（自建库），
        每条地址的分类、备注、交易所人工结论都原样保留，可撤回。

        ``sources`` 形如 ``[{"kind": "book"|"label", "name": "..."}]``；
        ``target_name`` 可以是一个新名字，也可以是所选组之一的名字（继承）。
        """

        if not sources or len(sources) < 2:
            raise InputError("至少要选两个组才能合并")
        target = self._clean_book_name(target_name)
        picked: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in sources:
            kind = str((item or {}).get("kind") or "").strip()
            name = " ".join(str((item or {}).get("name") or "").split())
            if kind not in {"book", "label"} or not name:
                raise InputError("合并来源只能是地址库或标签组")
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            picked.append(key)
        if len(picked) < 2:
            raise InputError("至少要选两个不同的组")

        snapshots: list[dict[str, Any]] = []
        source_books: list[str] = []
        addresses: list[str] = []
        with self.database.session() as db:
            for kind, name in picked:
                if kind == "book":
                    rows = db.scalars(
                        select(AddressLibrary).where(AddressLibrary.book == name)
                    ).all()
                    # 库存在但为空也允许（合并成一个空库没意义，这里按空组报错）
                    registry = db.scalar(select(AddressBook).where(AddressBook.name == name))
                    if registry is None and not rows:
                        raise InputError(f"地址库「{name}」不存在")
                    source_books.append(name)
                else:
                    rows = db.scalars(
                        select(AddressLibrary).where(
                            AddressLibrary.label == name, AddressLibrary.book == ""
                        )
                    ).all()
                    if not rows:
                        raise InputError(f"标签组「{name}」没有地址")
                if not rows:
                    raise InputError(f"组「{name}」没有地址，无法合并")
                for row in rows:
                    snapshots.append(
                        {
                            "address": row.address,
                            "snapshot": {
                                "label": row.label or "",
                                "category": row.category,
                                "categories": getattr(row, "categories", "") or "",
                                "note": row.note or "",
                                "book": getattr(row, "book", "") or "",
                                "source": row.source or "",
                                "exchange_confirmed": bool(row.exchange_confirmed),
                                "exchange_rejected": bool(row.exchange_rejected),
                            },
                        }
                    )
                    addresses.append(row.address)

            # 目标名已被别的库占用（且不在这次合并的来源里）就拒绝，避免吞掉别的库。
            existing = db.scalar(select(AddressBook).where(AddressBook.name == target))
            target_in_sources = target in source_books
            created_book = ""
            if existing is not None and not target_in_sources:
                raise InputError(f"已有同名地址库「{target}」，请换一个名字")
            if existing is None:
                db.add(AddressBook(name=target, note="由群组合并创建"))
                created_book = target

            removed_books: list[str] = []
            for name in source_books:
                if name == target:
                    continue
                registry = db.scalar(select(AddressBook).where(AddressBook.name == name))
                if registry is not None:
                    db.delete(registry)
                removed_books.append(name)

            for row in db.scalars(
                select(AddressLibrary).where(AddressLibrary.address.in_(addresses))
            ).all():
                row.book = target
                row.label = target

            _record_import_batch(
                db,
                kind="merge",
                summary=(
                    f"合并 {len(picked)} 个组到「{target}」· "
                    + "、".join(name for _, name in picked)
                ),
                inserted=[],
                updated=snapshots,
                created_book=created_book or None,
                removed_books=removed_books,
            )
        return {
            "merged": len(snapshots),
            "groups": len(picked),
            "name": target,
            "created_book": bool(created_book),
        }

    def last_import_batch(self) -> dict[str, Any] | None:
        """最近一次批量导入的摘要，供前端确认撤回内容。"""

        with self.database.session() as db:
            batch = db.get(ImportBatch, 1)
            if batch is None:
                return None
            data = json.loads(batch.payload or "{}")
            return {
                "kind": batch.kind,
                "summary": batch.summary,
                "created_at": batch.created_at.isoformat() if batch.created_at else None,
                "inserted": len(data.get("inserted") or []),
                "updated": len(data.get("updated") or []),
                "deleted": len(data.get("deleted") or []),
                "book": data.get("created_book") or "",
                "books": list(data.get("created_books") or []),
                "removed_book": data.get("removed_book") or "",
                "removed_books": list(data.get("removed_books") or []),
            }

    def undo_last_import(self) -> dict[str, Any]:
        """撤回上一次批量导入：删掉新导入的地址，并把被改写的记录还原。"""

        with self.database.session() as db:
            batch = db.get(ImportBatch, 1)
            if batch is None:
                raise InputError("没有可撤回的导入记录")
            summary = batch.summary
            data = json.loads(batch.payload or "{}")
            inserted = [value for value in (data.get("inserted") or []) if value]
            deleted = 0
            for index in range(0, len(inserted), 400):
                chunk = inserted[index : index + 400]
                for row in db.scalars(
                    select(AddressLibrary).where(AddressLibrary.address.in_(chunk))
                ).all():
                    db.delete(row)
                    deleted += 1
            restored = 0
            for item in data.get("updated") or []:
                address = str(item.get("address") or "")
                snapshot = item.get("snapshot") or {}
                if not address:
                    continue
                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == address)
                )
                if row is None:
                    continue
                row.label = snapshot.get("label", row.label) or ""
                row.category = snapshot.get("category", row.category) or DEFAULT_CATEGORY
                row.categories = snapshot.get("categories", row.categories) or ""
                row.note = snapshot.get("note", row.note) or ""
                row.book = snapshot.get("book", row.book) or ""
                row.source = snapshot.get("source", row.source) or ""
                row.exchange_confirmed = bool(snapshot.get("exchange_confirmed", False))
                row.exchange_rejected = bool(snapshot.get("exchange_rejected", False))
                restored += 1
            created_books = [
                name.strip()
                for name in (data.get("created_books") or [])
                if str(name).strip()
            ]
            book_name = (data.get("created_book") or "").strip()
            if book_name and book_name not in created_books:
                created_books.append(book_name)
            book_removed = False
            books_removed = 0
            for name in created_books:
                registry = db.scalar(select(AddressBook).where(AddressBook.name == name))
                if registry is not None:
                    db.delete(registry)
                    books_removed += 1
            book_removed = books_removed > 0
            # 解散地址库删掉的地址：按快照重建，连库名一起恢复。
            restored_deleted = 0
            for item in data.get("deleted") or []:
                address = str(item.get("address") or "")
                if not address:
                    continue
                if db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == address)
                ) is not None:
                    continue
                db.add(
                    AddressLibrary(
                        address=address,
                        label=str(item.get("label") or ""),
                        category=str(item.get("category") or DEFAULT_CATEGORY),
                        categories=str(item.get("categories") or ""),
                        note=str(item.get("note") or ""),
                        source=str(item.get("source") or "manual"),
                        book=str(item.get("book") or ""),
                        exchange_confirmed=bool(item.get("exchange_confirmed", False)),
                        exchange_rejected=bool(item.get("exchange_rejected", False)),
                    )
                )
                restored_deleted += 1
            removed_book = (data.get("removed_book") or "").strip()
            book_restored = False
            if removed_book and db.scalar(
                select(AddressBook).where(AddressBook.name == removed_book)
            ) is None:
                db.add(AddressBook(name=removed_book))
                book_restored = True
            # 合并群组时被并掉的库：撤回要把库名一起恢复，否则地址回来了却没有库。
            for name in data.get("removed_books") or []:
                name = str(name or "").strip()
                if not name:
                    continue
                if db.scalar(select(AddressBook).where(AddressBook.name == name)) is None:
                    db.add(AddressBook(name=name))
                    book_restored = True
            db.delete(batch)
            return {
                "summary": summary,
                "deleted": deleted,
                "restored": restored,
                "restored_deleted": restored_deleted,
                "book_removed": book_removed,
                "book_restored": book_restored,
            }

    def reinstall_seed_books(self) -> dict[str, Any]:
        """重新导入外发预置地址库：把预置组里地址库中缺失的地址补回来。

        用于「解散 / 删地址后想还原预置的几组」。已在地址库里的地址一律跳过，
        不会覆盖人工改过的分类、标签、备注；新导入的地址可点「撤回上次导入」撤销。
        """

        results = install_seed_books(self.database, force=True)
        if not results:
            raise InputError("外发包里没有预置地址库文件")
        inserted: list[str] = []
        created_books: list[str] = []
        groups: list[dict[str, Any]] = []
        for item in results:
            inserted.extend(item.pop("new_addresses", []) or [])
            if item.get("created"):
                created_books.append(str(item["name"]))
            groups.append(item)
        if inserted or created_books:
            detail = "、".join(
                f"{item['name']} +{item['inserted']}" for item in groups if item["inserted"]
            )
            summary = f"重新导入预置地址库：新增 {len(inserted)} 个地址"
            if detail:
                summary = f"{summary}（{detail}）"
            with self.database.session() as db:
                _record_import_batch(
                    db,
                    kind="seed",
                    summary=summary,
                    inserted=inserted,
                    updated=[],
                    created_books=created_books,
                )
        return {
            "groups": groups,
            "inserted": len(inserted),
            "created_books": created_books,
            "total": sum(int(item["total"]) for item in results),
        }

    def seed_books_info(self) -> dict[str, Any]:
        """预置地址库文件概况（组数、地址数），供界面显示按钮提示。"""

        return seed_book_status()

    def update_library_categories(
        self, row_id: int, categories: Iterable[str]
    ) -> dict[str, Any]:
        """人工修改一条地址的分类；支持一个地址同时属于多个分类。"""

        values = normalize_category_list(categories)
        with self.database.session() as db:
            current = db.get(AddressLibrary, row_id)
            if current is None:
                raise InputError("address not found")
            for name in values:
                self._ensure_category(db, name)
            current.category = values[0]
            current.categories = encode_categories(values)
            return {"saved": True, "item": address_dict(current)}

    def update_group_categories(
        self,
        categories: Iterable[str],
        *,
        book: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """整组改分类：把同一标签组（自建库或人工标签组）的地址一起改掉。

        分类是「组」的属性：单条地址不单独改分类，避免同组里出现不同分类。
        改动的旧值写进撤回快照，可用「撤回上次导入」还原。
        """

        values = normalize_category_list(categories)
        book_name = " ".join(str(book or "").split())
        label_name = " ".join(str(label or "").split())
        if not book_name and not label_name:
            raise InputError("请指定要改分类的标签组")
        with self.database.session() as db:
            query = select(AddressLibrary)
            if book_name:
                query = query.where(AddressLibrary.book == book_name)
            else:
                query = query.where(AddressLibrary.label == label_name)
            rows = db.scalars(query).all()
            if not rows:
                raise InputError("没有找到这个标签组")
            for name in values:
                self._ensure_category(db, name)
            # 系统分类（交易所/验证者）不参与人工分类：这些地址跳过，避免把角色信息冲掉。
            editable = [
                row
                for row in rows
                if not (row.exchange_confirmed or row.exchange_rejected)
                and not set(row_categories(row)) & {"EXCHANGE", "VALIDATOR"}
            ]
            if not editable:
                raise InputError("这一组都是交易所/验证者地址，不能改人工分类")
            updated: list[dict[str, Any]] = []
            for row in editable:
                updated.append({"address": row.address, "snapshot": _library_row_snapshot(row)})
                row.category = values[0]
                row.categories = encode_categories(values)
            _record_import_batch(
                db,
                kind="category",
                summary=f"整组改分类：{'、'.join(values)}（{len(editable)} 个地址）",
                inserted=[],
                updated=updated,
            )
            return {
                "saved": True,
                "updated": len(editable),
                "skipped": len(rows) - len(editable),
                "categories": values,
            }

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
        label: str = "",
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
            # 人工改判（确认 ↔ 否定）时不要把人给这一组起的名字冲掉：
            # 只有空名或系统自动写的“人工确认/否定交易所”才用默认名替换。
            auto_labels = {"", "人工确认交易所", "人工否定交易所"}
            default_label = {
                "confirm": "人工确认交易所",
                "reject": "人工否定交易所",
            }.get(decision, "")
            if current is None:
                current = AddressLibrary(
                    address=address,
                    label=(label.strip() or default_label or "人工否定交易所"),
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
                if label.strip():
                    current.label = label.strip()
                elif current.label.strip() in auto_labels and default_label:
                    current.label = default_label
                if decision == "reset":
                    current.source = "manual:exchange-reset"
                else:
                    current.source = (
                        "manual:exchange-confirmation"
                        if decision == "confirm"
                        else "manual:exchange-rejection"
                    )
                current.exchange_confirmed = decision == "confirm"
                current.exchange_rejected = decision == "reject"
            return {"saved": True, "item": address_dict(current)}

    def confirm_exchange_address(self, address: str, label: str = "") -> dict[str, Any]:
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

    # -- 地址库分类：大V / 群友 / 其他 + 自定义 -----------------------------
    def categories(self) -> list[dict[str, Any]]:
        """列出所有可选分类及各自地址数。

        表里没有、但地址记录里出现过的分类名（老 CSV 导入留下的）也会列出来，
        免得地址在清单里显示成一个选不到的分类。
        """

        with self.database.session() as db:
            rows = db.scalars(
                select(AddressCategory).order_by(
                    AddressCategory.sort_order, AddressCategory.id
                )
            ).all()
            counts = {
                name: int(count)
                for name, count in db.execute(
                    select(AddressLibrary.category, func.count(AddressLibrary.id)).group_by(
                        AddressLibrary.category
                    )
                ).all()
            }
        items = [
            {
                "name": row.name,
                "builtin": bool(row.builtin),
                "sort_order": row.sort_order,
                "count": counts.get(row.name, 0),
            }
            for row in rows
        ]
        known = {item["name"] for item in items}
        for name in sorted(name for name in counts if name and name not in known):
            if name.upper() in SYSTEM_CATEGORIES:
                continue
            items.append(
                {"name": name, "builtin": False, "sort_order": 999, "count": counts[name]}
            )
        return items

    def add_category(self, name: str) -> dict[str, Any]:
        value = " ".join(str(name or "").split())
        if not value:
            raise InputError("分类名称不能为空")
        if len(value) > CATEGORY_NAME_MAX_LENGTH:
            raise InputError(f"分类名称不能超过 {CATEGORY_NAME_MAX_LENGTH} 个字符")
        if value.upper() in RESERVED_CATEGORY_NAMES:
            raise InputError(f"「{value}」是系统保留名称，请换一个")
        with self.database.session() as db:
            existing = db.scalar(
                select(AddressCategory).where(AddressCategory.name == value)
            )
            if existing is not None:
                return {"created": False, "name": existing.name}
            top = db.scalar(select(func.max(AddressCategory.sort_order))) or 0
            db.add(AddressCategory(name=value, sort_order=int(top) + 1, builtin=False))
            return {"created": True, "name": value}

    def delete_category(self, name: str) -> dict[str, Any]:
        """删除自建分类；该分类下的地址回落到“其他”，不会丢地址。"""

        value = " ".join(str(name or "").split())
        if not value:
            raise InputError("分类名称不能为空")
        if value.upper() in SYSTEM_CATEGORIES:
            raise InputError("系统分类不能删除")
        with self.database.session() as db:
            row = db.scalar(select(AddressCategory).where(AddressCategory.name == value))
            if row is None:
                raise InputError("分类不存在")
            if row.builtin:
                raise InputError("预置分类不能删除，可以改用其他分类整理地址")
            moved = int(
                db.query(AddressLibrary)
                .filter(AddressLibrary.category == value)
                .update({AddressLibrary.category: DEFAULT_CATEGORY}, synchronize_session=False)
            )
            if db.scalar(
                select(AddressCategory).where(AddressCategory.name == DEFAULT_CATEGORY)
            ) is None:
                db.add(AddressCategory(name=DEFAULT_CATEGORY, sort_order=98, builtin=True))
            db.delete(row)
        return {"deleted": True, "name": value, "moved_to_default": moved}

    def _ensure_category(self, db, name: str) -> None:
        """导入时把没登记过的分类补进分类表，保证下拉里能选到。"""

        if not name or name.upper() in SYSTEM_CATEGORIES:
            return
        if db.scalar(select(AddressCategory).where(AddressCategory.name == name)) is not None:
            return
        top = db.scalar(select(func.max(AddressCategory.sort_order))) or 0
        db.add(AddressCategory(name=name[:CATEGORY_NAME_MAX_LENGTH], sort_order=int(top) + 1))

    def interlink_search(
        self,
        *,
        source: str,
        target: str = "",
        group_kind: str = "",
        group_name: str = "",
        max_hops: int = 3,
        max_pages: int = 3,
        min_tao: float = 0.0,
        use_local: bool = True,
        use_taostats: bool = True,
    ) -> dict[str, Any]:
        """两个地址之间（或地址与整个地址库之间）的转账明细检索。

        ``group_name`` 优先：给了它就走「地址 × 地址库」，否则用 ``target``
        走「地址 × 地址」，后者在没有直连时会尝试拼中间链路。
        """
        source = (source or "").strip()
        if not is_valid_ss58(source):
            raise InputError("起始地址不是合法的 SS58 地址")
        target = (target or "").strip()
        group_name = (group_name or "").strip()

        mode = "address"
        if group_name:
            mode = "group"
            kind = "book" if group_kind == "book" else "label"
            members = interlink.group_members(
                self.database, kind=kind, name=group_name, exclude=source
            )
            if not members:
                raise InputError(f"「{group_name}」里没有可检索的其他地址")
        elif target:
            if not is_valid_ss58(target):
                raise InputError("第二个地址不是合法的 SS58 地址")
            if target == source:
                raise InputError("两个地址相同，无法检索")
            members = [interlink.Member(address=target)]
        else:
            raise InputError("请选择一个地址库，或填写第二个地址")

        query = interlink.InterlinkQuery(
            source=source,
            members=members,
            mode=mode,
            max_hops=max(1, min(3, int(max_hops or 3))),
            max_pages=max(1, min(20, int(max_pages or 3))),
            min_tao=max(0.0, float(min_tao or 0.0)),
            use_local=bool(use_local),
            use_taostats=bool(use_taostats),
            group_name=group_name,
            group_kind="book" if group_kind == "book" else ("label" if group_name else ""),
        )
        result = interlink.search(self.database, query)
        # 把起始地址自己在地址库里的标签带上，方便界面上认人。
        with self.database.session() as db:
            row = db.scalar(
                select(AddressLibrary).where(AddressLibrary.address == source)
            )
            if row is not None:
                result["source_member"] = {
                    "address": row.address,
                    "label": row.label or "",
                    "group": row.book or row.label or "",
                    "category": row.category or "",
                }
        return result

    def library(self) -> list[dict[str, Any]]:
        order = {item["name"]: item["sort_order"] for item in self.categories()}
        with self.database.session() as db:
            rows = db.scalars(
                select(AddressLibrary).order_by(
                    AddressLibrary.category, AddressLibrary.label, AddressLibrary.id
                )
            ).all()
            return [
                address_dict(row)
                for row in sorted(
                    rows,
                    key=lambda row: (
                        order.get(row.category, 500),
                        row.label or "",
                        row.id,
                    ),
                )
            ]

    def export_library(self, file_format: str) -> tuple[str, str]:
        """Return a portable address-library backup and its media type."""

        if file_format not in {"csv", "json"}:
            raise InputError("library export format must be csv or json")
        portable = [
            {
                "address": item["address"],
                # 默认标签（地址后四位）不写进备份，导入后仍然跟随地址自动生成。
                "label": item["label"] if item.get("label_custom") else "",
                "category": item["category"],
                "categories": item.get("categories") or [item["category"]],
                "note": item["note"],
                "book": item["book"],
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
            licensed_to = license_mod.watermark()
            if licensed_to:
                # 只做溯源标记，导入时被忽略，不影响迁移。
                payload["licensed_to"] = licensed_to
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
            "categories",
            "note",
            "book",
            "exchange_confirmed",
            "exchange_rejected",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(
            {
                **item,
                "categories": "|".join(item.get("categories") or [item["category"]]),
            }
            for item in portable
        )
        return output.getvalue(), "text/csv; charset=utf-8"

    # -- 自建地址库：把一次查询结果导入成有名字的库 ----------------------
    @staticmethod
    def _clean_book_name(name: str) -> str:
        value = " ".join(str(name or "").split())
        if not value:
            raise InputError("地址库名称不能为空")
        if len(value) > BOOK_NAME_MAX_LENGTH:
            raise InputError(f"地址库名称不能超过 {BOOK_NAME_MAX_LENGTH} 个字符")
        return value

    @staticmethod
    def _book_dict(
        row: AddressBook | None, name: str, count: int, categories: dict[str, int]
    ) -> dict[str, Any]:
        return {
            "id": row.id if row is not None else None,
            "name": name,
            "note": row.note if row is not None else "",
            "seed": row.seed if row is not None else "",
            "count": count,
            "categories": categories,
            "created_at": (
                row.created_at.isoformat() if row is not None and row.created_at else None
            ),
            "updated_at": (
                row.updated_at.isoformat() if row is not None and row.updated_at else None
            ),
        }

    def address_books(self) -> list[dict[str, Any]]:
        """列出所有自建地址库，含按分类统计的地址数。"""

        with self.database.session() as db:
            rows = db.execute(
                select(
                    AddressLibrary.book,
                    AddressLibrary.category,
                    func.count(AddressLibrary.id),
                )
                .where(AddressLibrary.book != "")
                .group_by(AddressLibrary.book, AddressLibrary.category)
            ).all()
            registry = {row.name: row for row in db.scalars(select(AddressBook)).all()}
        counts: dict[str, int] = {}
        categories: dict[str, dict[str, int]] = {}
        for name, category, count in rows:
            counts[name] = counts.get(name, 0) + int(count)
            categories.setdefault(name, {})[category] = int(count)
        names = list(dict.fromkeys([*counts.keys(), *registry.keys()]))
        items = [
            self._book_dict(registry.get(name), name, counts.get(name, 0), categories.get(name, {}))
            for name in names
        ]
        items.sort(key=lambda item: (item["created_at"] or "", item["name"]), reverse=True)
        return items

    def create_address_book(
        self,
        *,
        name: str,
        seed: str = "",
        addresses: list[str] | None = None,
        include_seed: bool = True,
        category: str = DEFAULT_CATEGORY,
        note: str = "",
    ) -> dict[str, Any]:
        """新建（或追加到同名）地址库，把查询得到的关联地址导入进去。

        与“同步到已知地址库”的区别：这里会给这批地址打上库名，地址库里能按库
        查看和整库导出；已经存在的地址不会改分类、标签和备注，只补库名。
        已人工确认的交易所地址和验证者地址一律不导入，和导出规则保持一致。
        """

        book_name = self._clean_book_name(name)
        # 新建地址库只允许用户分类，避免把关联地址误标成交易所/验证者后被
        # 当成公共地址过滤掉。
        if str(category or "").strip().upper() in SYSTEM_CATEGORIES:
            raise InputError("新建地址库不能使用交易所/验证者这类系统分类")
        category = normalize_category(category, allow_system=False)
        seed = (seed or "").strip()
        if seed and not is_valid_ss58(seed):
            raise InputError("发起地址不是合法的 SS58 地址")
        raw_addresses = list(addresses or [])
        if len(raw_addresses) > MAX_BOOK_ADDRESSES:
            raise InputError(f"单个地址库最多导入 {MAX_BOOK_ADDRESSES} 个地址")

        candidates: list[str] = []
        seen: set[str] = set()
        invalid = 0
        if include_seed and seed:
            candidates.append(seed)
            seen.add(seed)
        for value in raw_addresses:
            value = str(value or "").strip()
            if not value or value in seen:
                continue
            if not is_valid_ss58(value):
                invalid += 1
                continue
            seen.add(value)
            candidates.append(value)
        if len(candidates) > MAX_BOOK_ADDRESSES:
            raise InputError(f"单个地址库最多导入 {MAX_BOOK_ADDRESSES} 个地址")

        note_text = note.strip()[:2000]
        default_note = (
            f"由关联查询 {seed} 新建并导入" if seed else f"新建地址库「{book_name}」导入"
        )
        source = f"book:{book_name}"[:255]
        with self.database.session() as db:
            self._ensure_category(db, category)
            existing: dict[str, AddressLibrary] = {}
            for index in range(0, len(candidates), 400):
                chunk = candidates[index : index + 400]
                for row in db.scalars(
                    select(AddressLibrary).where(AddressLibrary.address.in_(chunk))
                ).all():
                    existing[row.address] = row

            inserted = attached = already = other_book = 0
            skipped_exchange = skipped_validator = skipped_system = 0
            added: list[str] = []
            inserted_addresses: list[str] = []
            updated_snapshots: list[dict[str, Any]] = []
            for address in candidates:
                row = existing.get(address)
                if row is not None:
                    if (
                        row.category == "EXCHANGE"
                        and row.exchange_confirmed
                        and not row.exchange_rejected
                    ):
                        skipped_exchange += 1
                        continue
                    if row.category == "VALIDATOR":
                        skipped_validator += 1
                        continue
                    if SYSTEM_CLASSIFICATION in row_categories(row):
                        skipped_system += 1
                        continue
                    if not row.book:
                        updated_snapshots.append(
                            {"address": address, "snapshot": _library_row_snapshot(row)}
                        )
                        row.book = book_name
                        # 原本还没归类的地址（分类是默认的“其他”）按这次选的
                        # 分类走；已经人工分好类的保持不动。
                        if row.category == DEFAULT_CATEGORY and category != DEFAULT_CATEGORY:
                            row.category = category
                        attached += 1
                    elif row.book == book_name:
                        already += 1
                    else:
                        # 地址已经属于别的库，不抢归属，避免把人工整理好的库拆散。
                        other_book += 1
                    continue
                db.add(
                    AddressLibrary(
                        address=address,
                        label=book_name,
                        category=category,
                        categories=encode_categories([category]),
                        note=note_text or default_note,
                        source=source,
                        book=book_name,
                        exchange_confirmed=False,
                        exchange_rejected=False,
                    )
                )
                inserted += 1
                inserted_addresses.append(address)
                if len(added) < 200:
                    added.append(address)

            kept = inserted + attached + already + other_book
            if kept == 0:
                reasons = []
                if skipped_exchange:
                    reasons.append(f"已确认交易所 {skipped_exchange} 个")
                if skipped_validator:
                    reasons.append(f"验证者 {skipped_validator} 个")
                if skipped_system:
                    reasons.append(f"系统分类 {skipped_system} 个")
                detail = f"（{'、'.join(reasons)}已排除）" if reasons else ""
                raise InputError(f"没有可导入的地址{detail}，地址库未创建")

            registry = db.scalar(select(AddressBook).where(AddressBook.name == book_name))
            created_registry = registry is None
            if registry is None:
                registry = AddressBook(name=book_name, note=note_text, seed=seed)
                db.add(registry)
            else:
                if seed and not registry.seed:
                    registry.seed = seed
                if note_text:
                    registry.note = note_text
            db.flush()
            if inserted or attached:
                _record_import_batch(
                    db,
                    kind="book",
                    summary=f"新建地址库「{book_name}」· 新增 {inserted} 个、归入 {attached} 个",
                    inserted=inserted_addresses,
                    updated=updated_snapshots,
                    created_book=book_name if created_registry else None,
                )
                db.flush()
            result = {
                "book": self._book_dict(
                    registry,
                    book_name,
                    inserted + attached + already,
                    {category: inserted + attached} if inserted + attached else {},
                ),
                "name": book_name,
                "total": len(candidates),
                "inserted": inserted,
                "attached": attached,
                "already_in_book": already,
                "other_book": other_book,
                "existing": attached + already + other_book,
                "skipped_exchange": skipped_exchange,
                "skipped_validator": skipped_validator,
                "skipped_system": skipped_system,
                "invalid": invalid,
                "category": category,
                "added": added,
            }
        return result

    def _book_rows(self, db, name: str) -> list[AddressLibrary]:
        return list(
            db.scalars(
                select(AddressLibrary)
                .where(AddressLibrary.book == name)
                .order_by(AddressLibrary.id)
            ).all()
        )

    def export_address_book(self, name: str, file_format: str = "txt") -> tuple[str, str, str]:
        """整库导出：TXT 每行一个地址，CSV 带标签与分类。"""

        if file_format not in {"txt", "csv"}:
            raise InputError("地址库导出格式只能是 txt 或 csv")
        book_name = self._clean_book_name(name)
        with self.database.session() as db:
            rows = self._book_rows(db, book_name)
        if not rows:
            raise InputError("地址库不存在或还没有地址")
        safe_name = "".join(
            char for char in book_name if char not in '\\/:*?"<>|'
        ).strip() or "address-book"
        if file_format == "txt":
            header = ""
            licensed_to = license_mod.watermark()
            if licensed_to:
                # 这行以 # 开头，重新导入时会被当成注释忽略，不影响复用。
                header = f"# Wallet Link · 授权给 {licensed_to}\n"
            return (
                header + "\n".join(row.address for row in rows) + "\n",
                "text/plain; charset=utf-8",
                f"{safe_name}.txt",
            )
        import csv
        import io

        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["address", "label", "category", "note", "book"])
        for row in rows:
            writer.writerow([row.address, row.label, row.category, row.note, row.book])
        return output.getvalue(), "text/csv; charset=utf-8", f"{safe_name}.csv"

    def dissolve_address_book(self, name: str) -> dict[str, Any]:
        """解散地址库：库名标记和库里的地址一起删除。

        之前只去掉 ``book`` 标记，地址会继续挂在已知地址库里，清单里仍然
        看得到，等于“解散了但没清干净”。现在按库整体删除，只删属于这个库
        的地址（``book == 库名``）；同时写一条撤回快照，误删可用「撤回上次
        导入」连库带地址恢复。
        """

        book_name = self._clean_book_name(name)
        with self.database.session() as db:
            rows = self._book_rows(db, book_name)
            registry = db.scalar(select(AddressBook).where(AddressBook.name == book_name))
            if not rows and registry is None:
                raise InputError("地址库不存在")
            snapshots = [
                {"address": row.address, **_library_row_snapshot(row)} for row in rows
            ]
            for row in rows:
                db.delete(row)
            if registry is not None:
                db.delete(registry)
            if snapshots:
                _record_import_batch(
                    db,
                    kind="dissolve",
                    summary=f"解散地址库「{book_name}」· 删除 {len(snapshots)} 个地址",
                    inserted=[],
                    updated=[],
                    deleted=snapshots,
                    removed_book=book_name,
                )
        return {
            "dissolved": True,
            "name": book_name,
            "deleted": len(snapshots),
            # 兼容旧前端字段：解散不再保留地址。
            "addresses_kept": 0,
        }

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
        self,
        archive_id: int,
        file_format: str = "txt",
        *,
        depth: int | None = None,
    ) -> tuple[str, str, str]:
        """导出历史记录的关联地址（TXT）或完整结果（JSON）。

        ``depth`` 为 1/2/3 时只导出该层级的关联地址；None 导出全部。
        """

        if file_format not in {"txt", "json"}:
            raise InputError("导出格式只能是 txt 或 json")
        if depth is not None and depth not in (1, 2, 3):
            raise InputError("导出层级只能是 1、2、3 或全部")
        row = self._load_archive(archive_id)
        depth_tag = f"-d{depth}" if depth else ""
        if file_format == "json":
            if depth is None:
                return (
                    (row.payload or "{}") + "\n",
                    "application/json; charset=utf-8",
                    f"bittensor-link-archive-{archive_id}.json",
                )
            try:
                payload = json.loads(row.payload) if row.payload else {}
            except ValueError as exc:
                raise InputError("历史记录内容已损坏，无法导出地址") from exc
            payload["related"] = [
                item
                for item in payload.get("related") or []
                if str(item.get("depth") or "") == str(depth)
            ]
            return (
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                "application/json; charset=utf-8",
                f"bittensor-link-archive-{archive_id}{depth_tag}.json",
            )
        try:
            payload = json.loads(row.payload) if row.payload else {}
        except ValueError as exc:
            raise InputError("历史记录内容已损坏，无法导出地址") from exc
        selection = select_exportable_addresses(
            payload.get("related") or [], depth=depth
        )
        date = (row.finished_at or row.created_at).strftime("%Y%m%d")
        licensed_to = license_mod.watermark()
        header = f"# Wallet Link · 授权给 {licensed_to}\n" if licensed_to else ""
        return (
            header
            + "\n".join(selection["addresses"])
            + ("\n" if selection["addresses"] else ""),
            "text/plain; charset=utf-8",
            f"bittensor-addresses-{row.seed[:8]}{depth_tag}-{date}.txt",
        )

    # -- 关联地址同步进地址库 ---------------------------------------------
    def sync_relation_addresses(
        self,
        *,
        seed: str,
        addresses: list[str],
        include_seed: bool = True,
        category: str = DEFAULT_CATEGORY,
        label: str = "",
        note: str = "",
        book_name: str = "",
        only_library_matched: bool = False,
    ) -> dict[str, Any]:
        """把关联地址和发起地址去重写入地址库。

        已存在的地址一律跳过，不覆盖原有分类、标签和备注；这样反复同步同一批
        地址也不会把人工维护过的记录改掉。``book_name`` 指定时新地址直接归入
        该自建地址库（合并导入），不再另起一个标签组。
        """

        seed = (seed or "").strip()
        if seed and not is_valid_ss58(seed):
            raise InputError("发起地址不是合法的 SS58 地址")
        if str(category or "").strip().upper() in SYSTEM_CATEGORIES:
            raise InputError("同步地址库不能使用交易所/验证者这类系统分类")
        category = normalize_category(category, allow_system=False)
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
            self._ensure_category(db, category)
            existing_rows: dict[str, AddressLibrary] = {}
            for index in range(0, len(candidates), 400):
                chunk = candidates[index : index + 400]
                for row in db.scalars(
                    select(AddressLibrary).where(AddressLibrary.address.in_(chunk))
                ).all():
                    existing_rows[row.address] = row
            existing = set(existing_rows)
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
                    "book": (book_name or "").strip(),
                    "recategorized": 0,
                    "added": [],
                }

            # 合并导入已有地址库时，标签与库名保持一致，新地址才会落进同一个组。
            book_value = (book_name or "").strip()[:BOOK_NAME_MAX_LENGTH]
            if book_value and db.scalar(
                select(AddressBook).where(AddressBook.name == book_value)
            ) is None:
                raise InputError("目标地址库不存在；要新建地址库请用「新建地址库并导入」")
            if book_value and not label.strip():
                label = book_value
            # 没填标签时用发起地址后四位，同一批扫描同步进来的地址天然归成一组。
            default_label = label.strip() or default_address_label(seed)
            note_text = note.strip() or (
                f"由关联扫描同步：发起地址 {seed}" if seed else "由关联扫描同步"
            )
            source = f"relation-sync:{seed[:8]}" if seed else "relation-sync"
            added: list[dict[str, Any]] = []
            inserted = 0
            recategorized = 0
            inserted_addresses: list[str] = []
            updated_snapshots: list[dict[str, Any]] = []
            for address in candidates:
                if address in existing:
                    row = existing_rows[address]
                    # 已经人工分好类的地址不动；还挂在默认“其他”上的地址，
                    # 允许这次选择的分类把它归位，否则选了分类也没效果。
                    if (
                        row.category == DEFAULT_CATEGORY
                        and category != DEFAULT_CATEGORY
                    ):
                        updated_snapshots.append(
                            {"address": address, "snapshot": _library_row_snapshot(row)}
                        )
                        row.category = category
                        recategorized += 1
                    continue
                db.add(
                    AddressLibrary(
                        address=address,
                        label=default_label,
                        category=category,
                        note=note_text,
                        source=source,
                        book=book_value,
                        exchange_confirmed=category == "EXCHANGE",
                        exchange_rejected=False,
                    )
                )
                inserted += 1
                inserted_addresses.append(address)
                if len(added) < 20:
                    added.append({"address": address, "category": category})
            if inserted or recategorized:
                target = book_value or default_label
                _record_import_batch(
                    db,
                    kind="sync",
                    summary=f"同步到「{target}」· 新增 {inserted} 个地址",
                    inserted=inserted_addresses,
                    updated=updated_snapshots,
                )
            return {
                "total": len(candidates),
                "inserted": inserted,
                "existing": len(candidates) - inserted,
                "matched": matched,
                "seed_in_library": seed_in_library,
                "invalid": invalid,
                "category": category,
                "label": default_label,
                "book": book_value,
                "recategorized": recategorized,
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
        """查询页默认填入的地址：取最近整理过的一条非公共地址。"""

        with self.database.session() as db:
            return db.scalar(
                select(AddressLibrary.address)
                .where(AddressLibrary.category.notin_(SYSTEM_CATEGORIES))
                .order_by(AddressLibrary.updated_at.desc(), AddressLibrary.id.desc())
                .limit(1)
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
        filter_systems: bool = False,
        removed_addresses: Iterable[str] = (),
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
                    # 只传人工命名的标签；默认标签是地址后四位，拿它当“角色名”
                    # 会把“交易所地址”这类角色覆盖成 jEbt 之类的短码。
                    label=row.label.strip(),
                    category=row.category,
                    note=row.note,
                    source=row.source,
                    exchange_confirmed=bool(row.exchange_confirmed),
                    exchange_rejected=bool(row.exchange_rejected),
                    categories=tuple(row_categories(row)),
                )
                for row in db.scalars(select(AddressLibrary)).all()
            ]
            first_block = db.scalar(select(func.min(Transfer.block_number)))
            last_block = db.scalar(select(func.max(Transfer.block_number)))
            local_transfer_rows = to_relation_records(
                db.scalars(select(Transfer).order_by(Transfer.block_number)).all()
            )

        # 关联终点交易所：人工确认的 EXCHANGE 分类，或用户分类下标签/备注里
        # 写明「交易所」的地址。它们仍出现在结果里，但不再向下扩展（省额度）。
        exchange_addresses = {
            entry.address
            for entry in library
            if (
                entry.category == "EXCHANGE" and entry.exchange_confirmed
            ) or (
                entry.category not in {"EXCHANGE", "VALIDATOR"}
                and not entry.exchange_rejected
                and ("交易所" in entry.note or "交易所" in entry.label)
            )
        }
        confirmed_exchange_addresses = {
            entry.address
            for entry in library
            if entry.category == "EXCHANGE" and entry.exchange_confirmed
        }
        noted_exchange_addresses = exchange_addresses - confirmed_exchange_addresses
        # 「系统」分类：无论是否勾选过滤，查到即作为关联终点（不再向下扩展）；
        # 勾选过滤时连它本身也从结果里排除。
        system_addresses = {
            entry.address
            for entry in library
            if SYSTEM_CLASSIFICATION in entry.categories
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
        # 手动「移除关联」：地址既不进结果，也不再作为中转——它那条线索的
        # 下游地址在重查时一并消失。
        removed_addresses = {
            value.strip()
            for value in removed_addresses
            if value.strip() and is_valid_ss58(value.strip()) and value.strip() != seed
        }
        if filter_exchanges and exchange_addresses:
            warnings.append(f"已启用交易所过滤，跳过 {len(exchange_addresses)} 个已人工确认地址。")
        if noted_exchange_addresses and not filter_exchanges:
            warnings.append(
                f"有 {len(noted_exchange_addresses)} 个地址因人工备注/标签写明「交易所」，"
                "作为关联终点不再向下扩展其转账。"
            )
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
        if system_addresses:
            if filter_systems:
                excluded_addresses.update(system_addresses)
                warnings.append(
                    f"已启用系统地址过滤，跳过 {len(system_addresses)} 个「系统」分类地址；"
                    "其余地址查到系统地址即截止，不再向下扩展。"
                )
            else:
                warnings.append(
                    f"有 {len(system_addresses)} 个「系统」分类地址作为关联终点；"
                    "查到这些地址即截止，不再向下扩展其转账（如需连同它们一起排除，请勾选「过滤系统地址」）。"
                )
        if removed_addresses:
            excluded_addresses.update(removed_addresses)
            warnings.append(
                f"已手动移除 {len(removed_addresses)} 个关联地址；刷新后这些地址、与其有直接转账交集的"
                "地址及其线索的下游地址都不会出现。"
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
            removed_addresses=set(removed_addresses),
            system_addresses=set(system_addresses),
            validator_api_requests=validator_api_requests,
            validator_cache_hits=validator_cache_hits,
        )

    def new_crawl(self, context: RelationContext) -> TaostatsCrawl:
        """按上下文创建可续跑的爬取器（首次调用时才会排入起始地址）。"""

        state = CrawlState(
            seed=context.seed,
            depth=context.depth,
            policy=context.policy,
            terminal_exchange_addresses=(
                set(context.exchange_addresses) | set(context.system_addresses)
            ),
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
        filter_systems: bool = False,
        removed_addresses: Iterable[str] = (),
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
            filter_systems=filter_systems,
            removed_addresses=removed_addresses,
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
        include_balances: bool = True,
        balance_deadline: float | None = None,
    ) -> dict[str, Any]:
        """把上下文与已爬到的记录合成最终结果。

        ``include_balances=False`` 用于停止续查：不再花时间做余额 RPC，
        让部分结果立刻返回（余额栏显示“未获取”）。
        ``balance_deadline`` 是 ``time.monotonic()`` 表示的绝对截止时刻，
        所有余额 RPC（含交易所候选和关联地址两批）共享这一个总预算。
        """

        def balance_ttl() -> float | None:
            if balance_deadline is None:
                return None
            return balance_deadline - time.monotonic()

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
        if balance_candidates and include_balances:
            ttl = balance_ttl()
            if ttl is None or ttl > 0:
                try:
                    balances = query_account_balances(
                        self.rpc_url,
                        balance_candidates,
                        **({"total_timeout_seconds": ttl} if ttl is not None else {}),
                    )
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
            removed_addresses=context.removed_addresses,
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
            all_balance_addresses = list(dict.fromkeys(
                address
                for address in [seed, *(row.address for row in report.related)]
                if is_valid_ss58(address) and address not in balances
            ))
            balance_addresses = all_balance_addresses[:MAX_BALANCE_ADDRESSES_PER_RESULT]
            if len(all_balance_addresses) > len(balance_addresses):
                warnings.append(
                    f"为尽快返回结果，本次只读取前 {len(balance_addresses)} 个地址的当前余额"
                    f"（共 {len(all_balance_addresses)} 个）；其余地址余额栏显示“未获取”。"
                )
        if balance_addresses and include_balances:
            ttl = balance_ttl()
            if ttl is None or ttl > 0:
                try:
                    balances.update(query_account_balances(
                        self.rpc_url,
                        balance_addresses,
                        **({"total_timeout_seconds": ttl} if ttl is not None else {}),
                    ))
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
                removed_addresses=context.removed_addresses,
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
