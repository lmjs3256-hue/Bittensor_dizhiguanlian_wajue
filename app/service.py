"""Address library import, audit and chain scan orchestration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .collector import RPCCollector, query_account_balances
from .db import (
    AddressLibrary,
    ChainCursor,
    Database,
    ScanJob,
    ScanLock,
    Transfer,
    to_relation_records,
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
    get_transfer_page,
)
from .validation import is_valid_ss58


MAX_MANUAL_SCAN_BLOCKS = 16
MAX_TAOSTATS_REQUESTS_PER_QUERY = 5
MAX_EXCHANGE_BALANCE_CHECKS_PER_QUERY = 5
PUBLIC_MONTHLY_CREDIT_REFERENCE = 10_000


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


def _parse_upload(filename: str, text: str) -> list[dict[str, str]]:
    import csv
    import io
    import json

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

    def import_rows(self, rows: list[dict[str, str]], source: str) -> dict[str, int]:
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
                    db.add(
                        AddressLibrary(
                            address=address,
                            label=row.get("label", ""),
                            category=category,
                            note=row.get("note", ""),
                            source=source,
                            exchange_confirmed=(
                                category == "EXCHANGE"
                                and not bool(row.get("exchange_rejected", False))
                            ),
                            exchange_rejected=bool(row.get("exchange_rejected", False)),
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
        warnings = []
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
        transfer_rows = list(local_transfer_rows)

        api_requests = 0
        cache_hits = 0
        validator_api_requests = 0
        validator_cache_hits = 0
        pagination_complete = True
        provider_window_truncated = False
        api_error = ""
        balances: dict[str, Any] = {}
        records_by_key: dict[str, Any] = {}
        exchange_addresses = {
            entry.address
            for entry in library
            if entry.category == "EXCHANGE" and entry.exchange_confirmed
        }
        terminal_exchange_addresses = set(exchange_addresses)
        unconfirmed_exchange_addresses = {
            entry.address
            for entry in library
            if entry.category == "EXCHANGE" and not entry.exchange_confirmed
        }
        manual_validator_addresses = {
            entry.address for entry in library if entry.category == "VALIDATOR"
        }
        auto_validator_addresses: set[str] = set()
        if filter_validators:
            try:
                auto_validator_addresses, from_cache = get_active_validator_addresses()
                if from_cache:
                    validator_cache_hits += 1
                else:
                    validator_api_requests += 1
                    api_requests += 1
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
        if data_source == "taostats" and seed not in excluded_addresses:
            from collections import deque
            from decimal import Decimal, InvalidOperation

            from .relation_config import TransferRecord

            address_queue = deque([(seed, 0)])
            page_queue = deque()
            queued_pages = {(seed, 1)}
            address_depth = {seed: 0}
            while (address_queue or page_queue) and api_requests < MAX_TAOSTATS_REQUESTS_PER_QUERY:
                if address_queue:
                    address, current_depth = address_queue.popleft()
                    page_number = 1
                else:
                    address, current_depth, page_number = page_queue.popleft()
                if current_depth >= depth:
                    continue
                if address in terminal_exchange_addresses:
                    continue
                try:
                    page = get_transfer_page(address, page_number)
                    if page.from_cache:
                        cache_hits += 1
                    else:
                        api_requests += 1
                        self.database.record_api_request("/v1/transfers", success=True)
                except TaostatsAuthError as exc:
                    if getattr(exc, "request_made", False):
                        self.database.record_api_request(
                            "/v1/transfers",
                            status_code=getattr(exc, "status_code", None),
                            success=False,
                        )
                    api_error = str(exc)
                    pagination_complete = False
                    break
                except TaostatsRateLimitError as exc:
                    if getattr(exc, "request_made", False):
                        self.database.record_api_request(
                            "/v1/transfers",
                            status_code=getattr(exc, "status_code", None),
                            success=False,
                        )
                    api_error = str(exc)
                    pagination_complete = False
                    break
                except TaostatsUnavailableError as exc:
                    if getattr(exc, "request_made", False):
                        self.database.record_api_request(
                            "/v1/transfers",
                            status_code=getattr(exc, "status_code", None),
                            success=False,
                        )
                    api_error = str(exc)
                    pagination_complete = False
                    break

                if page.truncated_by_window:
                    provider_window_truncated = True
                    pagination_complete = False
                    warnings.append(
                        "Taostats 的服务端分页窗口未覆盖该地址全部匹配记录；"
                        "本次结果不是完整历史。"
                    )
                if page.next_page is not None:
                    next_key = (address, page.next_page)
                    if next_key not in queued_pages:
                        queued_pages.add(next_key)
                        page_queue.append((address, current_depth, page.next_page))

                next_addresses: set[str] = set()
                for item in page.rows:
                    try:
                        source = str(item["from"]).strip()
                        target = str(item["to"]).strip()
                        block_number = int(item["block_number"])
                        raw_amount = Decimal(str(item["amount"]))
                        tx_hash = str(item.get("transaction_hash") or "")
                        transfer_id = str(item.get("id") or "")
                        timestamp = str(item.get("timestamp") or "")
                    except (KeyError, TypeError, ValueError, InvalidOperation):
                        pagination_complete = False
                        warnings.append("Taostats 返回记录中有字段无法解析，已跳过该记录。")
                        continue
                    if not source or not target or not raw_amount.is_finite() or raw_amount < 0:
                        pagination_complete = False
                        warnings.append("Taostats 返回记录中有无效地址或金额，已跳过该记录。")
                        continue
                    amount_rao = int(raw_amount)
                    if raw_amount != amount_rao:
                        pagination_complete = False
                        warnings.append("Taostats 返回了非整数 RAO 金额，已跳过该记录。")
                        continue
                    if address not in {source, target}:
                        pagination_complete = False
                        warnings.append("Taostats 返回了与查询地址无关的转账记录，已跳过该记录。")
                        continue
                    key = transfer_id or f"{block_number}:{tx_hash}:{source}:{target}:{amount_rao}"
                    amount_tao = Decimal(amount_rao) / Decimal(1_000_000_000)
                    records_by_key[key] = TransferRecord(
                        tx_hash=tx_hash,
                        block_number=block_number,
                        from_address=source,
                        to_address=target,
                        amount_tao=amount_tao,
                        timestamp=timestamp,
                    )
                    if current_depth > 0 and amount_tao < policy.min_edge_tao:
                        continue
                    if source == address and policy.include_outgoing and target not in excluded_addresses:
                        next_addresses.add(target)
                    if target == address and policy.include_incoming and source not in excluded_addresses:
                        next_addresses.add(source)

                for neighbor in sorted(next_addresses):
                    next_depth = current_depth + 1
                    if next_depth < depth and next_depth < address_depth.get(neighbor, depth + 1):
                        if neighbor in terminal_exchange_addresses:
                            # Keep the edge in records_by_key so the public
                            # exchange appears in results, but never query or
                            # traverse through its high-volume outgoing graph.
                            continue
                        address_depth[neighbor] = next_depth
                        page_key = (neighbor, 1)
                        if page_key not in queued_pages:
                            queued_pages.add(page_key)
                            # Query the first page of each newly found address
                            # before spending the small free-tier budget on old pages.
                            address_queue.append((neighbor, next_depth))

            pending_queries = bool(address_queue or page_queue)
            if pending_queries and api_requests >= MAX_TAOSTATS_REQUESTS_PER_QUERY:
                pagination_complete = False
                warnings.append(
                    f"本轮单次查询最多调用 {MAX_TAOSTATS_REQUESTS_PER_QUERY} 次 Taostats API；"
                    "尚有待查询地址，结果只包含已获取的部分历史。"
                )
            if api_error:
                warnings.append(api_error)
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
        detected_balance_candidates = [
            address
            for address in exchange_balance_candidates(transfer_rows, library)
            if is_valid_ss58(address)
        ]
        balance_candidates = detected_balance_candidates[:MAX_EXCHANGE_BALANCE_CHECKS_PER_QUERY]
        if len(detected_balance_candidates) > len(balance_candidates):
            warnings.append(
                f"本次仅检查前 {MAX_EXCHANGE_BALANCE_CHECKS_PER_QUERY} 个高频候选余额；其余地址不会自动标记疑似交易所。"
            )
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
            "api_cache_hits": cache_hits,
            "validator_api_requests": validator_api_requests,
            "validator_cache_hits": validator_cache_hits,
            "filter_exchanges": filter_exchanges,
            "filter_validators": filter_validators,
            "exchange_addresses_filtered": len(exchange_addresses) if filter_exchanges else 0,
            "validator_addresses_filtered": (
                len(manual_validator_addresses | auto_validator_addresses)
                if filter_validators else 0
            ),
            "unconfirmed_exchange_candidates": len(unconfirmed_exchange_addresses),
            "exchange_balance_candidates": len(balance_candidates),
            "exchange_balance_checks": sum(value is not None for value in balances.values()),
            "local_transfer_count": len(local_transfer_rows),
            "taostats_transfer_count": len(records_by_key) if data_source == "taostats" else 0,
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
                }
                for row in report.related
            ],
            "library_matches": [
                row.__dict__ | {
                    "incoming_tao": str(row.incoming_tao),
                    "outgoing_tao": str(row.outgoing_tao),
                    "total_tao": str(row.total_tao),
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
