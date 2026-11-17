from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rules import event_fields, get_rule, load_rules


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode_watched_address(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["min_transfer_tao"] = float(item.get("min_transfer_tao") or 0)
    item["library_book"] = str(item.get("library_book") or "")
    return item


def _event_summary_with_subnet_prefix(
    summary: Any, netuid: Any, event_name: Any,
) -> str:
    text = str(summary or "").strip()
    name = str(event_name or "")
    if name == "Balances.Transfer":
        if text.startswith("SN") and " · " in text:
            _, _, remainder = text.partition(" · ")
            if "TAO 转账" in remainder:
                return remainder.strip()
        return text
    if not text or text.startswith("SN"):
        return text
    try:
        number = int(netuid)
    except (TypeError, ValueError):
        number = None
    if name.endswith(("StakeAdded", "StakeRemoved")) and number is not None:
        marker = f"SN{number}"
        text = text.replace(f" · {marker} ", " · ", 1)
        return f"{marker} · {text}".strip()
    marker = f"SN{number}" if number is not None else "SN待定"
    return f"{marker} · {text}".strip()


def _event_filter_clause(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    for name in (
        "network", "netuid", "event_type", "source",
        "chain_status", "enrichment_status",
    ):
        if filters.get(name) not in (None, ""):
            where.append(f"e.{name}=?")
            params.append(filters[name])
    if filters.get("from"):
        where.append("e.event_at>=?")
        params.append(filters["from"])
    if filters.get("to"):
        where.append("e.event_at<=?")
        params.append(filters["to"])
    if filters.get("q"):
        needle = f"%{filters['q']}%"
        where.append(
            "(e.summary LIKE ? OR e.event_name LIKE ? OR "
            "e.event_type LIKE ? OR CAST(e.netuid AS TEXT) LIKE ?)"
        )
        params.extend([needle, needle, needle, needle])
    return (" WHERE " + " AND ".join(where) if where else ""), params


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    network TEXT NOT NULL,
                    netuid INTEGER,
                    event_type TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT,
                    event_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_enriched_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    raw_data_json TEXT NOT NULL,
                    block_number INTEGER,
                    block_hash TEXT,
                    event_index INTEGER,
                    runtime_spec_version INTEGER,
                    chain_status TEXT,
                    enrichment_status TEXT NOT NULL DEFAULT 'complete',
                    confidence TEXT NOT NULL DEFAULT 'high',
                    updated_at TEXT NOT NULL,
                    update_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS events_time ON events(event_at DESC);
                CREATE INDEX IF NOT EXISTS events_netuid_time ON events(network, netuid, event_at);
                CREATE INDEX IF NOT EXISTS events_filter ON events(network, source, event_type, chain_status);
                CREATE TABLE IF NOT EXISTS event_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL REFERENCES events(event_id),
                    change_type TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_history_event ON event_history(event_id, history_id);
                CREATE TABLE IF NOT EXISTS raw_chain_events (
                    raw_id TEXT PRIMARY KEY,
                    network TEXT NOT NULL,
                    block_number INTEGER NOT NULL,
                    block_hash TEXT NOT NULL,
                    event_index INTEGER NOT NULL,
                    module TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    runtime_spec_version INTEGER,
                    raw_json TEXT NOT NULL,
                    collected_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chain_blocks (
                    network TEXT NOT NULL,
                    height INTEGER NOT NULL,
                    block_hash TEXT NOT NULL,
                    parent_hash TEXT NOT NULL,
                    runtime_spec_version INTEGER,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY(network, height)
                );
                CREATE TABLE IF NOT EXISTS runtime_metadata (
                    network TEXT NOT NULL,
                    spec_version INTEGER NOT NULL,
                    block_hash TEXT NOT NULL,
                    metadata_hash TEXT NOT NULL,
                    metadata_hex TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(network, spec_version)
                );
                CREATE TABLE IF NOT EXISTS collector_state (
                    collector TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repositories (
                    repository_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    network TEXT NOT NULL,
                    netuid INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    html_url TEXT NOT NULL,
                    default_branch TEXT,
                    relation_source TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_error TEXT,
                    commit_cursor TEXT,
                    release_cursor TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(network, owner, name)
                );
                CREATE TABLE IF NOT EXISTS system_alerts (
                    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS system_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    component TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS system_logs_time ON system_logs(created_at DESC);
                CREATE TABLE IF NOT EXISTS subnet_trading_state (
                    network TEXT NOT NULL,
                    netuid INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    state_source TEXT NOT NULL DEFAULT 'subtoken_enabled',
                    registration_block INTEGER,
                    block_hash TEXT NOT NULL,
                    block_number INTEGER,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(network, netuid)
                );
                CREATE TABLE IF NOT EXISTS subnet_profiles (
                    network TEXT NOT NULL,
                    netuid INTEGER NOT NULL,
                    auto_json TEXT NOT NULL DEFAULT '{}',
                    override_json TEXT NOT NULL DEFAULT '{}',
                    auto_block_hash TEXT,
                    auto_updated_at TEXT,
                    edited_at TEXT,
                    edited_by TEXT,
                    monitored INTEGER NOT NULL DEFAULT 1,
                    priority_level INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    profile_started_at TEXT,
                    archived_at TEXT,
                    archive_reason TEXT,
                    PRIMARY KEY(network, netuid)
                );
                CREATE TABLE IF NOT EXISTS subnet_profile_archive (
                    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    network TEXT NOT NULL,
                    netuid INTEGER NOT NULL,
                    archived_at TEXT NOT NULL,
                    archive_reason TEXT NOT NULL,
                    archive_key TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    UNIQUE(network, netuid, archive_key)
                );
                CREATE INDEX IF NOT EXISTS subnet_profile_archive_netuid
                    ON subnet_profile_archive(network, netuid, archived_at);
                CREATE TABLE IF NOT EXISTS watched_addresses (
                    address_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    network TEXT NOT NULL,
                    address TEXT NOT NULL,
                    label TEXT NOT NULL,
                    min_transfer_tao REAL NOT NULL DEFAULT 0,
                    library_book TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(network, address)
                );
                CREATE INDEX IF NOT EXISTS watched_addresses_network
                    ON watched_addresses(network, enabled);
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(subnet_trading_state)").fetchall()
            }
            if "state_source" not in columns:
                db.execute(
                    "ALTER TABLE subnet_trading_state ADD COLUMN state_source TEXT NOT NULL DEFAULT 'legacy'"
                )
            profile_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(subnet_profiles)").fetchall()
            }
            if "monitored" not in profile_columns:
                db.execute(
                    "ALTER TABLE subnet_profiles ADD COLUMN monitored INTEGER NOT NULL DEFAULT 1"
                )
            if "priority_level" not in profile_columns:
                db.execute(
                    "ALTER TABLE subnet_profiles ADD COLUMN priority_level INTEGER NOT NULL DEFAULT 0"
                )
            if "profile_started_at" not in profile_columns:
                db.execute("ALTER TABLE subnet_profiles ADD COLUMN profile_started_at TEXT")
            if "archived_at" not in profile_columns:
                db.execute("ALTER TABLE subnet_profiles ADD COLUMN archived_at TEXT")
            if "archive_reason" not in profile_columns:
                db.execute("ALTER TABLE subnet_profiles ADD COLUMN archive_reason TEXT")
            address_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(watched_addresses)").fetchall()
            }
            if "min_transfer_tao" not in address_columns:
                db.execute(
                    "ALTER TABLE watched_addresses ADD COLUMN min_transfer_tao REAL NOT NULL DEFAULT 0"
                )
            address_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(watched_addresses)").fetchall()
            }
            if "library_book" not in address_columns:
                db.execute(
                    "ALTER TABLE watched_addresses ADD COLUMN library_book TEXT NOT NULL DEFAULT ''"
                )
            self._backfill_event_summary_prefixes(db)

    def _backfill_event_summary_prefixes(self, db: sqlite3.Connection) -> None:
        rules: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            rules, _ = load_rules(
                Path(__file__).resolve().parent / "config" / "event_rules.json"
            )
        except (OSError, ValueError):
            rules = {}
        rows = db.execute(
            "SELECT event_id,netuid,event_name,summary,raw_data_json FROM events "
            "WHERE event_name LIKE 'Balances.%' OR event_name LIKE 'SubtensorModule.%'"
        ).fetchall()
        for row in rows:
            netuid = row["netuid"]
            raw_data: dict[str, Any] = {}
            try:
                raw_data = json.loads(row["raw_data_json"] or "{}")
            except (TypeError, ValueError):
                raw_data = {}
            if netuid is None and rules:
                rule = get_rule(
                    rules, raw_data.get("module"), raw_data.get("event_name")
                )
                if rule is not None:
                    attributes = raw_data.get("attributes")
                    if not attributes:
                        value = raw_data.get("value") or {}
                        if isinstance(value, dict):
                            attributes = value.get("attributes", [])
                    fields = event_fields(attributes or [], rule)
                    candidate = fields.get("netuid")
                    try:
                        netuid = int(candidate) if candidate is not None else None
                    except (TypeError, ValueError):
                        netuid = None
            summary = _event_summary_with_subnet_prefix(
                row["summary"], netuid, row["event_name"],
            )
            if (
                (netuid is not None and netuid != row["netuid"])
                or (summary and summary != row["summary"])
            ):
                db.execute(
                    "UPDATE events SET netuid=?,summary=? WHERE event_id=?",
                    (netuid, summary, row["event_id"]),
                )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _decode_event(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        item["raw_data"] = json.loads(item.pop("raw_data_json"))
        return item

    def create_event(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        stamp = now_iso()
        values = dict(event)
        values.setdefault("first_seen_at", stamp)
        values.setdefault("last_enriched_at", stamp)
        values.setdefault("updated_at", stamp)
        values["details_json"] = json_text(values.pop("details", {}))
        values["raw_data_json"] = json_text(values.pop("raw_data", {}))
        columns = list(values)
        with self.connect() as db:
            cur = db.execute(
                f"INSERT OR IGNORE INTO events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )
            created = cur.rowcount == 1
            if created:
                db.execute(
                    "INSERT INTO event_history(event_id,change_type,changed_at,payload_json) VALUES(?,?,?,?)",
                    (event["event_id"], "discovered", stamp, json_text(self._decode_event(db.execute("SELECT * FROM events WHERE event_id=?", (event["event_id"],)).fetchone()))),
                )
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            return self._decode_event(row), created

    def update_event(self, event_id: str, change_type: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        stamp = now_iso()
        safe = {key: value for key, value in patch.items() if key in {
            "netuid", "event_type", "source_url", "event_at", "last_enriched_at", "summary",
            "chain_status", "enrichment_status", "confidence", "runtime_spec_version", "block_number", "block_hash"
        }}
        for key in ("details", "raw_data"):
            if key in patch:
                safe[key + "_json"] = json_text(patch[key])
        safe.update(updated_at=stamp)
        with self.connect() as db:
            old = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if old is None:
                return None
            safe["update_count"] = old["update_count"] + 1
            sets = ",".join(f"{key}=?" for key in safe)
            db.execute(f"UPDATE events SET {sets} WHERE event_id=?", [*safe.values(), event_id])
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            item = self._decode_event(row)
            db.execute(
                "INSERT INTO event_history(event_id,change_type,changed_at,payload_json) VALUES(?,?,?,?)",
                (event_id, change_type, stamp, json_text({"patch": patch, "event": item})),
            )
            return item

    def process_chain_block(self, network: str, height: int, block_hash: str, parent_hash: str,
                            spec_version: int | None, raw_rows: list[dict[str, Any]],
                            event_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stamp = now_iso()
        created_events: list[dict[str, Any]] = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for raw in raw_rows:
                db.execute(
                    "INSERT OR IGNORE INTO raw_chain_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (raw["raw_id"], network, height, block_hash, raw["event_index"], raw["module"], raw["event_name"], spec_version, json_text(raw["raw"]), stamp),
                )
            for event in event_rows:
                data = dict(event)
                data.setdefault("first_seen_at", stamp)
                data.setdefault("last_enriched_at", stamp)
                data.setdefault("updated_at", stamp)
                data["details_json"] = json_text(data.pop("details", {}))
                data["raw_data_json"] = json_text(data.pop("raw_data", {}))
                cols = list(data)
                cur = db.execute(
                    f"INSERT OR IGNORE INTO events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                    [data[col] for col in cols],
                )
                if cur.rowcount == 1:
                    row = db.execute("SELECT * FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
                    item = self._decode_event(row)
                    created_events.append(item)
                    db.execute("INSERT INTO event_history(event_id,change_type,changed_at,payload_json) VALUES(?,?,?,?)",
                               (event["event_id"], "discovered", stamp, json_text(item)))
            db.execute("INSERT OR REPLACE INTO chain_blocks VALUES(?,?,?,?,?,?)",
                       (network, height, block_hash, parent_hash, spec_version, stamp))
            db.execute("INSERT OR REPLACE INTO collector_state VALUES(?,?,?)",
                       (f"chain:{network}", json_text({"height": height, "hash": block_hash, "spec_version": spec_version}), stamp))
        return created_events

    def get_cursor(self, collector: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT state_json FROM collector_state WHERE collector=?", (collector,)).fetchone()
            return json.loads(row["state_json"]) if row else None

    def get_block(self, network: str, height: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM chain_blocks WHERE network=? AND height=?", (network, height)).fetchone()
            return dict(row) if row else None

    def last_block(self, network: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM chain_blocks WHERE network=? ORDER BY height DESC LIMIT 1", (network,)).fetchone()
            return dict(row) if row else None

    def has_runtime_metadata(self, network: str, spec_version: int) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM runtime_metadata WHERE network=? AND spec_version=?",
                (network, spec_version),
            ).fetchone()
            return row is not None

    def save_runtime_metadata(self, network: str, spec_version: int, block_hash: str,
                              metadata_hash: str, metadata_hex: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO runtime_metadata
                   (network,spec_version,block_hash,metadata_hash,metadata_hex,captured_at)
                   VALUES(?,?,?,?,?,?)""",
                (network, spec_version, block_hash, metadata_hash, metadata_hex, now_iso()),
            )

    def list_runtime_metadata(self, network: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT spec_version,block_hash,metadata_hash,length(metadata_hex) AS metadata_hex_length,
                          captured_at
                   FROM runtime_metadata WHERE network=? ORDER BY spec_version DESC""",
                (network,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_runtime_metadata(self, network: str, spec_version: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT spec_version,block_hash,metadata_hash,metadata_hex,captured_at
                   FROM runtime_metadata WHERE network=? AND spec_version=?""",
                (network, spec_version),
            ).fetchone()
            return dict(row) if row else None

    def find_common_block(self, network: str, canonical_hash_at: Any, from_height: int) -> dict[str, Any] | None:
        with self.connect() as db:
            height = from_height
            while height >= 0:
                row = db.execute("SELECT * FROM chain_blocks WHERE network=? AND height=?", (network, height)).fetchone()
                if row and row["block_hash"].lower() == canonical_hash_at(height).lower():
                    return dict(row)
                height -= 1
        return None

    def retract_after(self, network: str, ancestor_height: int) -> list[dict[str, Any]]:
        stamp = now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM events WHERE network=? AND source IN ('chain','address') AND block_number>? AND chain_status!='retracted' ORDER BY block_number,event_index",
                              (network, ancestor_height)).fetchall()
            updated = []
            for row in rows:
                item = self._decode_event(row)
                item["chain_status"] = "retracted"
                item["summary"] = "链重组更正：该事件所在区块已不在当前主链"
                item["update_count"] += 1
                item["updated_at"] = stamp
                db.execute("UPDATE events SET chain_status='retracted',summary=?,updated_at=?,update_count=update_count+1 WHERE event_id=?",
                           (item["summary"], stamp, item["event_id"]))
                db.execute("INSERT INTO event_history(event_id,change_type,changed_at,payload_json) VALUES(?,?,?,?)",
                           (item["event_id"], "retracted", stamp, json_text({"reason": "reorg", "event": item})))
                updated.append(item)
            db.execute("DELETE FROM raw_chain_events WHERE network=? AND block_number>?", (network, ancestor_height))
            db.execute("DELETE FROM chain_blocks WHERE network=? AND height>?", (network, ancestor_height))
            row = db.execute("SELECT height,block_hash,runtime_spec_version FROM chain_blocks WHERE network=? AND height=?", (network, ancestor_height)).fetchone()
            state = {"height": row["height"], "hash": row["block_hash"], "spec_version": row["runtime_spec_version"]} if row else None
            if state is None:
                db.execute("DELETE FROM collector_state WHERE collector=?", (f"chain:{network}",))
            else:
                db.execute("UPDATE collector_state SET state_json=?,updated_at=? WHERE collector=?",
                           (json_text(state), stamp, f"chain:{network}"))
        return updated

    def finalize_through(self, network: str, height: int) -> list[dict[str, Any]]:
        changed = []
        with self.connect() as db:
            rows = db.execute("SELECT event_id FROM events WHERE network=? AND source IN ('chain','address') AND chain_status='observed' AND block_number<=?",
                              (network, height)).fetchall()
        for row in rows:
            item = self.update_event(row["event_id"], "finalized", {"chain_status": "finalized"})
            if item:
                changed.append(item)
        return changed

    def list_events(self, filters: dict[str, Any], *, ascending: bool = False, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        clause, params = _event_filter_clause(filters)
        direction = "ASC" if ascending else "DESC"
        with self.connect() as db:
            rows = db.execute(f"""SELECT e.*,COALESCE(p.priority_level,0) AS priority_level,
                                     COALESCE(p.monitored,1) AS subnet_monitored
                              FROM events e
                              LEFT JOIN subnet_profiles p
                                ON p.network=e.network AND p.netuid=e.netuid
                              {clause}
                              ORDER BY e.event_at {direction},e.first_seen_at {direction}
                              LIMIT ? OFFSET ?""",
                              [*params, max(1, min(500, limit)), max(0, offset)]).fetchall()
            return [self._decode_event(row) for row in rows]

    def list_subnet_events(self, network: str, netuid: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT e.*,COALESCE(p.priority_level,0) AS priority_level,
                          COALESCE(p.monitored,1) AS subnet_monitored
                   FROM events e
                   LEFT JOIN subnet_profiles p
                     ON p.network=e.network AND p.netuid=e.netuid
                   WHERE e.network=? AND e.netuid=?
                   ORDER BY e.event_at ASC,e.first_seen_at ASC""",
                (network, int(netuid)),
            ).fetchall()
            return [self._decode_event(row) for row in rows]

    def subnet_event_counts(self, network: str) -> dict[int, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT netuid,COUNT(*) AS event_count,
                          MIN(event_at) AS first_event_at,MAX(event_at) AS last_event_at
                   FROM events
                   WHERE network=? AND netuid IS NOT NULL
                   GROUP BY netuid ORDER BY netuid""",
                (network,),
            ).fetchall()
            return {int(row["netuid"]): dict(row) for row in rows}

    def count_events(self, filters: dict[str, Any]) -> int:
        clause, params = _event_filter_clause(filters)
        with self.connect() as db:
            row = db.execute(
                f"SELECT COUNT(*) AS total FROM events e{clause}",
                params,
            ).fetchone()
            return int(row["total"])

    def list_event_types(self, network: str | None = None) -> list[str]:
        if network:
            with self.connect() as db:
                records = db.execute(
                    "SELECT DISTINCT event_type FROM events WHERE network=? ORDER BY event_type",
                    (network,),
                ).fetchall()
        else:
            with self.connect() as db:
                records = db.execute(
                    "SELECT DISTINCT event_type FROM events ORDER BY event_type"
                ).fetchall()
        return [str(row["event_type"]) for row in records]

    def delete_event_ids(self, event_ids: list[str]) -> int:
        ids = list(dict.fromkeys(event_id for event_id in event_ids if event_id))
        if not ids:
            return 0
        with self.connect() as db:
            marks = ",".join("?" for _ in ids)
            db.execute(f"DELETE FROM event_history WHERE event_id IN ({marks})", ids)
            cur = db.execute(f"DELETE FROM events WHERE event_id IN ({marks})", ids)
            return cur.rowcount

    def delete_simulated_events(self, *, event_id: str | None = None) -> int:
        """Delete regular simulation rows and realistically shaped address simulations."""
        with self.connect() as db:
            where = "(source='simulation' OR confidence='simulated')"
            params: list[Any] = []
            if event_id is not None:
                where += " AND event_id=?"
                params.append(event_id)
            ids = [
                row["event_id"]
                for row in db.execute(
                    f"SELECT event_id FROM events WHERE {where}", params
                ).fetchall()
            ]
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            db.execute(
                f"DELETE FROM event_history WHERE event_id IN ({marks})", ids
            )
            db.execute(f"DELETE FROM events WHERE event_id IN ({marks})", ids)
            return len(ids)

    def delete_events_by_source(self, source: str, *, event_id: str | None = None) -> int:
        """Permanently delete records belonging to the requested source."""
        with self.connect() as db:
            where = "source=?"
            params: list[Any] = [source]
            if event_id is not None:
                where += " AND event_id=?"
                params.append(event_id)
            ids = [
                row["event_id"]
                for row in db.execute(
                    f"SELECT event_id FROM events WHERE {where}", params
                ).fetchall()
            ]
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            db.execute(
                f"DELETE FROM event_history WHERE event_id IN ({marks})", ids
            )
            db.execute(f"DELETE FROM events WHERE event_id IN ({marks})", ids)
            return len(ids)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT e.*,COALESCE(p.priority_level,0) AS priority_level,
                          COALESCE(p.monitored,1) AS subnet_monitored
                   FROM events e
                   LEFT JOIN subnet_profiles p
                     ON p.network=e.network AND p.netuid=e.netuid
                   WHERE e.event_id=?""",
                (event_id,),
            ).fetchone()
            return self._decode_event(row) if row else None

    def event_history(self, event_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in db.execute(
                "SELECT * FROM event_history WHERE event_id=? ORDER BY history_id", (event_id,)).fetchall()]

    def raw_events(self, event_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            row = db.execute("SELECT block_hash,event_index FROM events WHERE event_id=?", (event_id,)).fetchone()
            if not row:
                return []
            rows = db.execute("SELECT * FROM raw_chain_events WHERE block_hash=? AND event_index=? ORDER BY network",
                              (row["block_hash"], row["event_index"])).fetchall()
            return [{**dict(item), "raw": json.loads(item["raw_json"])} for item in rows]

    def add_alert(self, component: str, severity: str, message: str) -> dict[str, Any]:
        stamp = now_iso()
        with self.connect() as db:
            open_alert = db.execute("SELECT * FROM system_alerts WHERE component=? AND message=? AND resolved_at IS NULL ORDER BY alert_id DESC LIMIT 1",
                                    (component, message)).fetchone()
            if open_alert:
                return dict(open_alert)
            cur = db.execute(
                "INSERT INTO system_alerts(component,severity,message,created_at) VALUES(?,?,?,?)",
                (component, severity, message, stamp),
            )
            row = db.execute("SELECT * FROM system_alerts WHERE alert_id=?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def resolve_alerts(self, component: str, message: str | None = None) -> None:
        with self.connect() as db:
            if message:
                db.execute("UPDATE system_alerts SET resolved_at=? WHERE component=? AND message=? AND resolved_at IS NULL", (now_iso(), component, message))
            else:
                db.execute("UPDATE system_alerts SET resolved_at=? WHERE component=? AND resolved_at IS NULL", (now_iso(), component))

    def resolve_alert_family(self, prefix: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE system_alerts SET resolved_at=? WHERE component LIKE ? AND resolved_at IS NULL",
                (now_iso(), prefix + "%"),
            )

    def list_alerts(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM system_alerts WHERE resolved_at IS NULL ORDER BY alert_id DESC LIMIT 100").fetchall()]

    def add_log(self, level: str, component: str, message: str, max_rows: int = 5000) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO system_logs(created_at,level,component,message) VALUES(?,?,?,?)",
                (now_iso(), level.upper(), component[:160], message[:4000]),
            )
            count = db.execute("SELECT count(*) FROM system_logs").fetchone()[0]
            if count > max_rows:
                db.execute(
                    """DELETE FROM system_logs WHERE log_id IN (
                       SELECT log_id FROM system_logs ORDER BY log_id ASC LIMIT ?
                    )""",
                    (count - max_rows,),
                )

    def list_logs(self, *, limit: int = 200, level: str | None = None,
                  component: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if level:
            where.append("level=?")
            params.append(level.upper())
        if component:
            where.append("component LIKE ?")
            params.append(component + "%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM system_logs{clause} ORDER BY log_id DESC LIMIT ?",
                [*params, max(1, min(1000, limit))],
            ).fetchall()
            return [dict(row) for row in rows]

    def set_trading_state(self, *, network: str, netuid: int, enabled: bool,
                          registration_block: int | None, block_hash: str,
                          block_number: int | None,
                          state_source: str = "subtoken_enabled") -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                """INSERT INTO subnet_trading_state
                   (network,netuid,enabled,state_source,registration_block,block_hash,block_number,observed_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(network,netuid) DO UPDATE SET
                     enabled=excluded.enabled,
                     state_source=excluded.state_source,
                     registration_block=COALESCE(excluded.registration_block,subnet_trading_state.registration_block),
                     block_hash=excluded.block_hash,
                     block_number=excluded.block_number,
                     observed_at=excluded.observed_at""",
                (network, netuid, int(enabled), state_source, registration_block, block_hash,
                 block_number, now_iso()),
            )
            row = db.execute(
                "SELECT * FROM subnet_trading_state WHERE network=? AND netuid=?",
                (network, netuid),
            ).fetchone()
            return dict(row)

    def get_trading_state(self, network: str, netuid: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM subnet_trading_state WHERE network=? AND netuid=?",
                (network, netuid),
            ).fetchone()
            if row is None:
                return None
            return {**dict(row), "enabled": bool(row["enabled"])}

    def list_trading_states(self, network: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT network,netuid,enabled,state_source,registration_block,block_hash,
                          block_number,observed_at
                   FROM subnet_trading_state WHERE network=?
                   ORDER BY registration_block DESC NULLS LAST,netuid DESC""",
                (network,),
            ).fetchall()
            return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def ensure_subnet_profile(self, network: str, netuid: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO subnet_profiles(network,netuid,active,profile_started_at) VALUES(?,?,1,?)",
                (network, netuid, now_iso()),
            )

    @staticmethod
    def _profile_snapshot(row: sqlite3.Row, archived_at: str, reason: str) -> dict[str, Any]:
        auto = json.loads(row["auto_json"] or "{}")
        overrides = json.loads(row["override_json"] or "{}")
        effective = {**auto, **overrides}
        return {
            "network": row["network"],
            "netuid": int(row["netuid"]),
            **effective,
            "auto": auto,
            "overrides": overrides,
            "auto_block_hash": row["auto_block_hash"],
            "auto_updated_at": row["auto_updated_at"],
            "edited_at": row["edited_at"],
            "edited_by": row["edited_by"],
            "profile_started_at": row["profile_started_at"],
            "archived_at": archived_at,
            "archive_reason": reason,
        }

    @staticmethod
    def _has_profile_data(row: sqlite3.Row) -> bool:
        return bool(
            json.loads(row["auto_json"] or "{}")
            or json.loads(row["override_json"] or "{}")
            or row["auto_block_hash"]
        )

    def _archive_profile_row(self, db: sqlite3.Connection, row: sqlite3.Row,
                             reason: str, archive_key: str, stamp: str) -> None:
        snapshot = self._profile_snapshot(row, stamp, reason)
        db.execute(
            """INSERT OR IGNORE INTO subnet_profile_archive
               (network,netuid,archived_at,archive_reason,archive_key,profile_json)
               VALUES(?,?,?,?,?,?)""",
            (row["network"], int(row["netuid"]), stamp, reason, archive_key,
             json_text(snapshot)),
        )
        db.execute(
            """UPDATE subnet_profiles
               SET auto_json='{}',override_json='{}',auto_block_hash=NULL,
                   auto_updated_at=NULL,edited_at=NULL,edited_by=NULL,
                   monitored=1,priority_level=0,active=0,
                   profile_started_at=NULL,archived_at=?,archive_reason=?
               WHERE network=? AND netuid=?""",
            (stamp, reason, row["network"], int(row["netuid"])),
        )

    def archive_subnet_profile(self, network: str, netuid: int, reason: str,
                               archive_key: str | None = None) -> bool:
        stamp = now_iso()
        key = archive_key or f"archive:{stamp}"
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM subnet_profiles WHERE network=? AND netuid=?",
                (network, int(netuid)),
            ).fetchone()
            if row is None or not row["active"] or not self._has_profile_data(row):
                return False
            self._archive_profile_row(db, row, reason, key, stamp)
            return True

    def reset_subnet_profile_for_registration(self, network: str, netuid: int,
                                               archive_key: str | None = None) -> None:
        stamp = now_iso()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM subnet_profiles WHERE network=? AND netuid=?",
                (network, int(netuid)),
            ).fetchone()
            if row is not None and row["active"] and self._has_profile_data(row):
                self._archive_profile_row(
                    db, row, "new_registration_replaced_active_profile",
                    archive_key or f"registration:{stamp}", stamp,
                )
            db.execute(
                "INSERT OR IGNORE INTO subnet_profiles(network,netuid,active,profile_started_at) VALUES(?,?,1,?)",
                (network, int(netuid), stamp),
            )
            db.execute(
                """UPDATE subnet_profiles
                   SET auto_json='{}',override_json='{}',auto_block_hash=NULL,
                       auto_updated_at=NULL,edited_at=NULL,edited_by=NULL,
                       monitored=1,priority_level=0,active=1,
                       profile_started_at=?,archived_at=NULL,archive_reason=NULL
                   WHERE network=? AND netuid=?""",
                (stamp, network, int(netuid)),
            )

    def list_archived_subnet_profiles(self, network: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT archive_id,archived_at,archive_reason,profile_json
                   FROM subnet_profile_archive
                   WHERE network=?
                   ORDER BY archived_at DESC,archive_id DESC""",
                (network,),
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["profile_json"])
            item.update({
                "archive_id": row["archive_id"],
                "archived_at": row["archived_at"],
                "archive_reason": row["archive_reason"],
                "active": False,
            })
            result.append(item)
        return result

    def upsert_subnet_profile_auto(self, network: str, netuid: int,
                                   values: dict[str, Any], block_hash: str | None) -> None:
        self.ensure_subnet_profile(network, netuid)
        stamp = now_iso()
        with self.connect() as db:
            row = db.execute(
                "SELECT active,archived_at FROM subnet_profiles WHERE network=? AND netuid=?",
                (network, netuid),
            ).fetchone()
            if row and row["archived_at"]:
                db.execute(
                    """UPDATE subnet_profiles
                       SET auto_json='{}',override_json='{}',auto_block_hash=NULL,
                           auto_updated_at=NULL,edited_at=NULL,edited_by=NULL,
                           monitored=1,priority_level=0,active=1,
                           profile_started_at=?,archived_at=NULL,archive_reason=NULL
                       WHERE network=? AND netuid=?""",
                    (stamp, network, netuid),
                )
            db.execute(
                """UPDATE subnet_profiles
                   SET auto_json=?,auto_block_hash=?,auto_updated_at=?,active=1,
                       archived_at=NULL,archive_reason=NULL
                   WHERE network=? AND netuid=?""",
                (json_text(values), block_hash, stamp, network, netuid),
            )

    def save_subnet_profile_override(self, network: str, netuid: int,
                                     values: dict[str, Any], edited_by: str) -> dict[str, Any]:
        self.ensure_subnet_profile(network, netuid)
        with self.connect() as db:
            row = db.execute(
                "SELECT override_json FROM subnet_profiles WHERE network=? AND netuid=?",
                (network, netuid),
            ).fetchone()
            overrides = json.loads(row["override_json"] or "{}") if row else {}
            overrides.update(values)
            db.execute(
                """UPDATE subnet_profiles
                   SET override_json=?,edited_at=?,edited_by=?,active=1,
                       profile_started_at=COALESCE(profile_started_at,?),
                       archived_at=NULL,archive_reason=NULL
                   WHERE network=? AND netuid=?""",
                (json_text(overrides), now_iso(), edited_by, now_iso(), network, netuid),
            )
        return self.get_subnet_profile(network, netuid)

    def get_subnet_profile(self, network: str, netuid: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT p.*,t.enabled AS trading_enabled,
                          t.registration_block,t.block_number,t.observed_at
                   FROM subnet_profiles p
                   LEFT JOIN subnet_trading_state t
                     ON t.network=p.network AND t.netuid=p.netuid
                   WHERE p.network=? AND p.netuid=?""",
                (network, netuid),
            ).fetchone()
            return self._decode_profile(row) if row else None

    def list_subnet_profiles(self, network: str, *,
                             include_inactive: bool = False) -> list[dict[str, Any]]:
        where = "WHERE p.network=?"
        if not include_inactive:
            where += " AND p.active=1"
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT p.*,t.enabled AS trading_enabled,
                           t.registration_block,t.block_number,t.observed_at
                    FROM subnet_profiles p
                    LEFT JOIN subnet_trading_state t
                      ON t.network=p.network AND t.netuid=p.netuid
                    {where}
                    ORDER BY p.netuid""",
                (network,),
            ).fetchall()
            return [self._decode_profile(row) for row in rows]

    def reconcile_subnet_profiles(self, network: str, seen_netuids: set[int]) -> None:
        stamp = now_iso()
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM subnet_profiles WHERE network=? AND active=1",
                (network,),
            ).fetchall()
            for row in rows:
                netuid = int(row["netuid"])
                if netuid not in seen_netuids:
                    if self._has_profile_data(row):
                        self._archive_profile_row(
                            db, row, "reconcile_registration_missing",
                            f"reconcile:{stamp}:{netuid}", stamp,
                        )
                    else:
                        db.execute(
                            """UPDATE subnet_profiles
                               SET active=0,profile_started_at=NULL,archived_at=?,
                                   archive_reason='reconcile_registration_missing'
                               WHERE network=? AND netuid=?""",
                            (stamp, network, netuid),
                        )
            for netuid in sorted(seen_netuids):
                row = db.execute(
                    "SELECT active,archived_at FROM subnet_profiles WHERE network=? AND netuid=?",
                    (network, netuid),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO subnet_profiles(network,netuid,active,profile_started_at) VALUES(?,?,1,?)",
                        (network, netuid, stamp),
                    )
                elif not row["active"] or row["archived_at"]:
                    db.execute(
                        """UPDATE subnet_profiles
                           SET auto_json='{}',override_json='{}',auto_block_hash=NULL,
                               auto_updated_at=NULL,edited_at=NULL,edited_by=NULL,
                               monitored=1,priority_level=0,active=1,
                               profile_started_at=?,archived_at=NULL,archive_reason=NULL
                           WHERE network=? AND netuid=?""",
                        (stamp, network, netuid),
                    )

    def set_subnet_monitoring(self, network: str, netuids: list[int], monitored: bool) -> int:
        clean = sorted({int(netuid) for netuid in netuids if 0 <= int(netuid) <= 65535})
        with self.connect() as db:
            for netuid in clean:
                db.execute(
                    "INSERT OR IGNORE INTO subnet_profiles(network,netuid,active) VALUES(?,?,1)",
                    (network, netuid),
                )
                db.execute(
                    """UPDATE subnet_profiles
                       SET monitored=?,
                           priority_level=CASE WHEN ?=0 THEN 0 ELSE priority_level END
                       WHERE network=? AND netuid=?""",
                    (int(monitored), int(monitored), network, netuid),
                )
        return len(clean)

    def monitored_netuids(self, network: str) -> set[int]:
        with self.connect() as db:
            return {
                int(row["netuid"])
                for row in db.execute(
                    "SELECT netuid FROM subnet_profiles WHERE network=? AND monitored=1 AND active=1",
                    (network,),
                ).fetchall()
            }

    def disabled_netuids(self, network: str) -> set[int]:
        with self.connect() as db:
            return {
                int(row["netuid"])
                for row in db.execute(
                    "SELECT netuid FROM subnet_profiles WHERE network=? AND monitored=0",
                    (network,),
                ).fetchall()
            }

    def set_subnet_priority(self, network: str, netuids: list[int], level: int) -> int:
        level = max(0, min(2, int(level)))
        clean = sorted({int(netuid) for netuid in netuids if 0 <= int(netuid) <= 65535})
        with self.connect() as db:
            for netuid in clean:
                db.execute(
                    """INSERT INTO subnet_profiles(network,netuid,monitored,priority_level,active)
                       VALUES(?,?,?,?,1)
                       ON CONFLICT(network,netuid) DO UPDATE SET
                         priority_level=excluded.priority_level,
                         monitored=1,
                         active=1""",
                    (network, netuid, int(level > 0), level),
                )
        return len(clean)

    def list_prioritized_profiles(self, network: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT p.*,t.enabled AS trading_enabled,
                          t.registration_block,t.block_number,t.observed_at
                   FROM subnet_profiles p
                   LEFT JOIN subnet_trading_state t
                     ON t.network=p.network AND t.netuid=p.netuid
                   WHERE p.network=? AND p.active=1 AND p.priority_level>0
                   ORDER BY p.priority_level DESC,p.netuid ASC""",
                (network,),
            ).fetchall()
            return [self._decode_profile(row) for row in rows]

    def list_polling_repositories(self, network: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT r.*,COALESCE(p.priority_level,0) AS priority_level
                   FROM repositories r
                   LEFT JOIN subnet_profiles p
                     ON p.network=r.network AND p.netuid=r.netuid
                   WHERE r.network=? AND r.active=1 AND COALESCE(p.monitored,1)=1
                   ORDER BY COALESCE(p.priority_level,0) DESC,
                            COALESCE(r.last_checked_at,'') ASC,r.repository_id ASC""",
                (network,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _decode_profile(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        auto = json.loads(item.pop("auto_json") or "{}")
        overrides = json.loads(item.pop("override_json") or "{}")
        auto_sources = auto.pop("_sources", {})
        effective = {**auto, **overrides}
        return {
            "network": item["network"],
            "netuid": item["netuid"],
            **effective,
            "auto": auto,
            "overrides": overrides,
            "auto_block_hash": item["auto_block_hash"],
            "auto_updated_at": item["auto_updated_at"],
            "edited_at": item["edited_at"],
            "edited_by": item["edited_by"],
            "monitored": bool(item["monitored"]),
            "priority_level": int(item["priority_level"]),
            "active": bool(item["active"]),
            "profile_started_at": item.get("profile_started_at"),
            "archived_at": item.get("archived_at"),
            "archive_reason": item.get("archive_reason"),
            "trading_enabled": (
                bool(item["trading_enabled"]) if item["trading_enabled"] is not None else None
            ),
            "registration_block": item["registration_block"],
            "trading_block_number": item["block_number"],
            "trading_observed_at": item["observed_at"],
            "field_sources": {
                field: (
                    "manual" if field in overrides
                    else auto_sources.get(field, "chain_identity")
                )
                for field in effective
                if effective.get(field)
            },
        }

    def list_watched_addresses(self, network: str,
                               *, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE network=?"
        if enabled_only:
            where += " AND enabled=1"
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT * FROM watched_addresses {where}
                    ORDER BY created_at DESC,address_id DESC""",
                (network,),
            ).fetchall()
            return [_decode_watched_address(row) for row in rows]

    def add_watched_address(
        self, network: str, address: str, label: str,
        min_transfer_tao: float = 0, library_book: str = "",
    ) -> dict[str, Any]:
        stamp = now_iso()
        try:
            with self.connect() as db:
                cur = db.execute(
                    """INSERT INTO watched_addresses
                       (network,address,label,min_transfer_tao,library_book,enabled,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        network, address, label, max(0.0, float(min_transfer_tao)),
                        str(library_book or "").strip()[:80], 1, stamp, stamp,
                    ),
                )
                row = db.execute(
                    "SELECT * FROM watched_addresses WHERE address_id=?",
                    (cur.lastrowid,),
                ).fetchone()
                return _decode_watched_address(row)
        except sqlite3.IntegrityError as exc:
            raise ValueError("该地址已在地址库中") from exc

    def update_watched_address(self, address_id: int, *,
                               label: str | None = None,
                               enabled: bool | None = None,
                               min_transfer_tao: float | None = None,
                               library_book: str | None = None) -> dict[str, Any] | None:
        updates = {"updated_at": now_iso()}
        if label is not None:
            updates["label"] = label
        if enabled is not None:
            updates["enabled"] = int(enabled)
        if min_transfer_tao is not None:
            updates["min_transfer_tao"] = max(0.0, float(min_transfer_tao))
        if library_book is not None:
            updates["library_book"] = str(library_book or "").strip()[:80]
        with self.connect() as db:
            current = db.execute(
                "SELECT address_id FROM watched_addresses WHERE address_id=?",
                (address_id,),
            ).fetchone()
            if current is None:
                return None
            db.execute(
                "UPDATE watched_addresses SET "
                + ",".join(f"{key}=?" for key in updates)
                + " WHERE address_id=?",
                [*updates.values(), address_id],
            )
            row = db.execute(
                "SELECT * FROM watched_addresses WHERE address_id=?",
                (address_id,),
            ).fetchone()
            return _decode_watched_address(row)

    def delete_watched_address(self, address_id: int) -> bool:
        with self.connect() as db:
            cur = db.execute(
                "DELETE FROM watched_addresses WHERE address_id=?",
                (address_id,),
            )
            return cur.rowcount == 1

    def list_repositories(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM repositories ORDER BY netuid,owner,name").fetchall()]

    def add_repository(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            cur = db.execute(
                """INSERT INTO repositories
                   (network,netuid,owner,name,html_url,relation_source,reviewed_by,added_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (values["network"], values["netuid"], values["owner"], values["name"],
                 values["html_url"], values["relation_source"], values["reviewed_by"], now_iso()),
            )
            return dict(db.execute("SELECT * FROM repositories WHERE repository_id=?", (cur.lastrowid,)).fetchone())

    def upsert_chain_repository(self, values: dict[str, Any]) -> dict[str, Any]:
        """Keep a chain-derived mapping fresh without overwriting a reviewed mapping."""
        stamp = now_iso()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM repositories WHERE network=? AND owner=? AND name=?",
                (values["network"], values["owner"], values["name"]),
            ).fetchone()
            if row:
                if row["relation_source"] == "chain_identity":
                    db.execute(
                        """UPDATE repositories
                           SET netuid=?,html_url=?,relation_source='chain_identity',active=1
                           WHERE repository_id=?""",
                        (values["netuid"], values["html_url"], row["repository_id"]),
                    )
                return dict(db.execute("SELECT * FROM repositories WHERE repository_id=?", (row["repository_id"],)).fetchone())
            cur = db.execute(
                """INSERT INTO repositories
                   (network,netuid,owner,name,html_url,relation_source,reviewed_by,added_at)
                   VALUES(?,?,?,?,?, 'chain_identity', 'chain:SubtensorModule.SubnetIdentitiesV3', ?)""",
                (values["network"], values["netuid"], values["owner"], values["name"],
                 values["html_url"], stamp),
            )
            return dict(db.execute("SELECT * FROM repositories WHERE repository_id=?", (cur.lastrowid,)).fetchone())

    def upsert_reviewed_repository(self, values: dict[str, Any]) -> dict[str, Any]:
        """Create or refresh a profile-edit repository mapping."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM repositories WHERE network=? AND owner=? AND name=?",
                (values["network"], values["owner"], values["name"]),
            ).fetchone()
            if row:
                db.execute(
                    """UPDATE repositories
                       SET netuid=?,html_url=?,relation_source='manual_review',
                           reviewed_by=?,active=1
                       WHERE repository_id=?""",
                    (values["netuid"], values["html_url"], values["reviewed_by"],
                     row["repository_id"]),
                )
                repository_id = row["repository_id"]
            else:
                cur = db.execute(
                    """INSERT INTO repositories
                       (network,netuid,owner,name,html_url,relation_source,reviewed_by,added_at)
                       VALUES(?,?,?,?,?,'manual_review',?,?)""",
                    (values["network"], values["netuid"], values["owner"], values["name"],
                     values["html_url"], values["reviewed_by"], now_iso()),
                )
                repository_id = cur.lastrowid
            return dict(db.execute(
                "SELECT * FROM repositories WHERE repository_id=?", (repository_id,)
            ).fetchone())

    def reconcile_chain_repositories(self, network: str, seen_repository_ids: set[int]) -> int:
        """Deactivate chain mappings no longer present in the latest identity state."""
        with self.connect() as db:
            rows = db.execute(
                """SELECT repository_id,owner,name FROM repositories
                   WHERE network=? AND relation_source='chain_identity' AND active=1""",
                (network,),
            ).fetchall()
            stale = [row["repository_id"] for row in rows if row["repository_id"] not in seen_repository_ids]
            if stale:
                marks = ",".join("?" for _ in stale)
                db.execute(
                    f"UPDATE repositories SET active=0 WHERE repository_id IN ({marks})",
                    stale,
                )
                for row in rows:
                    if row["repository_id"] in stale:
                        db.execute(
                            """UPDATE system_alerts SET resolved_at=?
                               WHERE component=? AND resolved_at IS NULL""",
                            (now_iso(), f"github:{row['owner']}/{row['name']}"),
                        )
            return len(stale)

    def deactivate_chain_repositories_for_netuid(self, network: str, netuid: int,
                                                 keep_repository_id: int | None = None) -> int:
        with self.connect() as db:
            if keep_repository_id is None:
                cur = db.execute(
                    """UPDATE repositories SET active=0
                       WHERE network=? AND netuid=? AND relation_source='chain_identity' AND active=1""",
                    (network, netuid),
                )
            else:
                cur = db.execute(
                    """UPDATE repositories SET active=0
                       WHERE network=? AND netuid=? AND relation_source='chain_identity'
                         AND active=1 AND repository_id!=?""",
                    (network, netuid, keep_repository_id),
                )
            return cur.rowcount

    def set_repo_baseline(self, repo_id: int, branch: str | None, commit: str | None, release: str | None) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE repositories
                   SET default_branch=?,commit_cursor=COALESCE(commit_cursor,?),
                       release_cursor=COALESCE(release_cursor,?),last_checked_at=?,last_error=NULL
                   WHERE repository_id=?""",
                (branch, commit, release, now_iso(), repo_id),
            )

    def reset_repo_baseline(self, repo_id: int, branch: str | None,
                            commit: str | None, release: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE repositories
                   SET default_branch=?,commit_cursor=?,release_cursor=COALESCE(?,release_cursor),
                       last_checked_at=?,last_error=NULL
                   WHERE repository_id=?""",
                (branch, commit, release, now_iso(), repo_id),
            )

    def advance_repo_cursor(self, repo_id: int, *, commit: str | None = None, release: str | None = None,
                            branch: str | None = None, error: str | None = None) -> None:
        updates = {"last_checked_at": now_iso(), "last_error": error}
        if commit is not None:
            updates["commit_cursor"] = commit
        if release is not None:
            updates["release_cursor"] = release
        if branch is not None:
            updates["default_branch"] = branch
        with self.connect() as db:
            db.execute("UPDATE repositories SET " + ",".join(f"{k}=?" for k in updates) + " WHERE repository_id=?", [*updates.values(), repo_id])
