from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import time
import threading
import urllib.request
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import xxhash

from .address_monitor import (
    address_event_record,
    append_project_owner_suffix,
    derived_stake_transfer_buy_raw,
    mask_address,
    project_owner_tags_for_raw_event,
)
from .github import parse_github_repository
from .rules import event_fields, get_rule
from .storage import Store, now_iso
from .substrate_adapter import AsyncSubstrateAdapter

LOG = logging.getLogger(__name__)
EVENT_PARSER_STATE = "chain:dict-event-parser:v2"
EVENT_PARSER_BACKFILL_BLOCKS = max(0, int(os.getenv("WALLET_LINK_EVENT_BACKFILL_BLOCKS", "200")))


def _json_value(value: Any) -> Any:
    if hasattr(value, "value"):
        raw = value.value
        return _json_value(raw() if callable(raw) else raw)
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "serialize"):
        try:
            return _json_value(value.serialize())
        except Exception:
            pass
    return str(value)


def _result(response: Any) -> Any:
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return response


def _height(header: dict[str, Any]) -> int:
    return int(str(header["number"]), 16)


def _attributes(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        candidate = event.get("attributes")
        if not isinstance(candidate, (dict, list, tuple)):
            nested = event.get("event")
            candidate = nested.get("attributes") if isinstance(nested, dict) else None
        params = candidate
    else:
        params = getattr(event, "params", None)
        if not isinstance(params, (dict, list, tuple)):
            value = getattr(event, "value", {})
            if callable(value):
                value = value()
            if isinstance(value, dict):
                candidate = value.get("attributes", value.get("params"))
                if isinstance(candidate, (dict, list, tuple)):
                    params = candidate
                elif candidate is not None:
                    params = [candidate]
                else:
                    params = []
            elif params is None:
                params = []
            else:
                params = [params]
    if not isinstance(params, (dict, list, tuple)):
        params = []
    if isinstance(params, dict):
        return {str(k): _json_value(v) for k, v in params.items()}
    result: dict[str, Any] = {}
    for index, item in enumerate(params or []):
        if isinstance(item, dict):
            name = item.get("name") or item.get("type")
            val = item.get("value")
            if name:
                result[str(name)] = _json_value(val)
            else:
                result[str(index)] = _json_value(item)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            result[str(item[0])] = _json_value(item[1])
        else:
            result[str(index)] = _json_value(item)
    return result


def _number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def _with_subnet_prefix(summary: Any, netuid: Any = None) -> str:
    text = str(summary or "").strip()
    if re.match(r"^SN(?:\d|待定|未定位|不适用)", text):
        return text
    number = _number(netuid)
    prefix = f"SN{number}" if number is not None else "SN待定"
    return f"{prefix} {text}".strip()


def _timestamp(value: Any) -> str:
    try:
        millis = int(value)
        return datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return now_iso()


class ChainCollector:
    def __init__(self, *, url: str, network: str, store: Store,
                 rules: dict[tuple[str, str], dict[str, Any]], publish: Any,
                 status: Any, poll_seconds: float = 2.0, max_blocks_per_poll: int = 100,
                 fetch_workers: int = 3, profile_sync_seconds: int = 604800,
                 subnet_lifecycle_callback: Any = None):
        self.url = url
        self.network = network
        self.store = store
        self.rules = rules
        self.publish = publish
        self.status = status
        self.poll_seconds = poll_seconds
        self.profile_sync_seconds = max(300, profile_sync_seconds)
        self.max_blocks_per_poll = max_blocks_per_poll
        self.subnet_lifecycle_callback = subnet_lifecycle_callback
        self.substrate: Any = None
        self._connection_generation = 0
        self.last_spec_version: int | None = None
        self.last_repository_sync: float = 0.0
        self.last_profile_sync_at: str | None = None
        self.last_trading_sync: float = 0.0
        self._repository_sync_lock = threading.Lock()
        self._repository_sync_thread: threading.Thread | None = None
        self._trading_sync_lock = threading.Lock()
        self._trading_sync_thread: threading.Thread | None = None
        self._worker_local = threading.local()
        self._metadata_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, fetch_workers),
            thread_name_prefix=f"chain-fetch-{network}",
        )
        self._enrichment_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"chain-enrich-{network}",
        )

    def connect(self) -> None:
        self.substrate = self._new_substrate()
        self._connection_generation += 1
        self.status("chain", "connected", {"network": self.network, "endpoint": self.url})

    def force_reconnect(self) -> dict[str, Any]:
        """Drop this collector's adapter handle so the run loop reconnects."""

        previous = self.substrate
        self.substrate = None
        self._connection_generation += 1
        try:
            if previous is not None:
                previous.close()
        except Exception:
            pass
        stamp = now_iso()
        self.status("chain", "connecting", {
            "network": self.network,
            "endpoint": self.url,
            "manual_reset": True,
            "updated_at": stamp,
        })
        return {
            "state": "connecting",
            "connection_generation": self._connection_generation,
            "updated_at": stamp,
        }

    def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        if self.substrate is None:
            raise RuntimeError("chain connection is not initialized")
        return _result(self.substrate.rpc_request(method, params or []))

    def _new_substrate(self) -> Any:
        return AsyncSubstrateAdapter(self.url)

    def hash_at(self, height: int) -> str:
        result = self.rpc("chain_getBlockHash", [height])
        if not result:
            raise RuntimeError(f"RPC returned no block hash for height {height}")
        return str(result)

    def version_at(self, block_hash: str) -> int | None:
        try:
            value = self.rpc("state_getRuntimeVersion", [block_hash])
            return int(value["specVersion"]) if value else None
        except Exception:
            LOG.exception("Could not read runtime version at %s", block_hash)
            return None

    def _capture_runtime_metadata(self, substrate: Any, block_hash: str,
                                  spec_version: int | None) -> None:
        if spec_version is None or self.store.has_runtime_metadata(self.network, spec_version):
            return
        with self._metadata_lock:
            if self.store.has_runtime_metadata(self.network, spec_version):
                return
            metadata = _result(substrate.rpc_request("state_getMetadata", [block_hash]))
            metadata_hex = metadata if isinstance(metadata, str) else json.dumps(
                _json_value(metadata), ensure_ascii=False, separators=(",", ":")
            )
            digest = hashlib.sha256(metadata_hex.encode("utf-8")).hexdigest()
            self.store.save_runtime_metadata(
                self.network, spec_version, block_hash, digest, metadata_hex
            )

    def _event_name(self, event: Any, name: str) -> str:
        aliases = {
            "event_module": ("event_module", "module_id", "moduleId"),
            "event": ("event_id", "eventId", "event"),
        }.get(name, (name, name.replace("_", "")))
        payload = event if isinstance(event, dict) else getattr(event, "value", {})
        if callable(payload):
            payload = payload()
        candidates: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            candidates.append(payload)
            nested = payload.get("event")
            if isinstance(nested, dict):
                candidates.append(nested)
        for candidate in candidates:
            for alias in aliases:
                part = candidate.get(alias)
                if part is None or part is candidate or isinstance(part, dict):
                    continue
                return str(getattr(part, "name", part))
        if not isinstance(event, dict):
            part = getattr(event, name, None)
            if part is not None and not isinstance(part, dict):
                return str(getattr(part, "name", part))
        return "unknown"

    def _event_index(self, event: Any, fallback: int) -> int:
        value = event.get("event_index") if isinstance(event, dict) else getattr(event, "event_index", None)
        if value is None:
            raw_value = event if isinstance(event, dict) else getattr(event, "value", {})
            if callable(raw_value):
                raw_value = raw_value()
            if isinstance(raw_value, dict):
                value = raw_value.get("event_index")
        value = fallback if value is None else value
        try:
            return int(str(value), 0) if isinstance(value, str) else int(value)
        except (ValueError, TypeError):
            return fallback

    def _raw_record(self, event: Any, block_hash: str, height: int, event_index: int) -> dict[str, Any]:
        module = self._event_name(event, "event_module")
        name = self._event_name(event, "event")
        value = event if isinstance(event, dict) else getattr(event, "value", {})
        if callable(value):
            value = value()
        return {
            "raw_id": f"{self.network}:{block_hash}:{event_index}",
            "event_index": event_index,
            "module": module,
            "event_name": name,
            "raw": {"height": height, "block_hash": block_hash, "module": module,
                    "event_name": name, "attributes": _attributes(event), "value": _json_value(value),
                    "phase": _json_value(
                        getattr(event, "phase", None)
                        or (value.get("phase") if isinstance(value, dict) else None)
                    ),
                    "extrinsic_idx": _json_value(
                        getattr(event, "extrinsic_idx", None)
                        if getattr(event, "extrinsic_idx", None) is not None
                        else (value.get("extrinsic_idx") if isinstance(value, dict) else None)
                    )},
        }

    def _event_record(self, raw: dict[str, Any], block_hash: str, height: int, spec_version: int | None,
                      event_at: str) -> dict[str, Any] | None:
        rule = get_rule(self.rules, raw["module"], raw["event_name"])
        if not rule:
            return None
        attrs = raw["raw"]["attributes"]
        fields = event_fields(attrs, rule)
        index = rule.get("netuid_index")
        netuid = fields.get("netuid") if index is None else _number(attrs.get("netuid", fields.get("netuid")))
        if netuid is None and isinstance(attrs, dict):
            for key in ("netuid", "net_uid", "netUid", "netuid_index"):
                if key in attrs:
                    netuid = _number(attrs[key])
                    break
        if netuid is None and index is not None:
            vals = list(attrs.values())
            if 0 <= index < len(vals):
                netuid = _number(vals[index])
        raw_id = raw["raw_id"]
        event_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
        summary = rule["summary_template"]
        if rule.get("id") == "network_registered" and netuid is not None:
            summary = f"SN{netuid} 已注册"
        elif rule.get("id") == "network_removed" and netuid is not None:
            summary = f"SN{netuid} 已从网络中移除"
        elif rule.get("id") == "dissolve_scheduled" and netuid is not None:
            summary = f"SN{netuid} 解散已排期"
        elif rule.get("id") in ("subnet_identity_set", "subnet_identity_removed") and netuid is not None:
            summary = f"SN{netuid} 身份信息已{('设置' if rule['id'].endswith('_set') else '移除')}，正在核验变更内容"
        elif rule.get("id") == "subnet_owner_changed" and netuid is not None:
            summary = f"SN{netuid} 子网所有者已变更"
        elif rule.get("id") == "subnet_owner_hotkey_set" and netuid is not None:
            summary = f"SN{netuid} 所有者热键已设置"
        elif rule.get("id") == "network_registration_queued":
            summary = "收到子网注册排队信息；该事件未提供已分配的子网编号"
        summary = _with_subnet_prefix(summary, netuid)
        return {
            "event_id": event_id,
            "network": self.network,
            "netuid": netuid,
            "event_type": rule["category"],
            "event_name": f"{raw['module']}.{raw['event_name']}",
            "source": "chain",
            "source_url": (
                "https://polkadot.js.org/apps/"
                "?rpc=wss%3A%2F%2Fentrypoint-finney.opentensor.ai%3A443"
                f"#/explorer/query/{block_hash}"
            ),
            "event_at": event_at,
            "summary": summary,
            "details": {
                "fields": fields,
                "rule_id": rule["id"],
                "rule_source": rule.get("evidence_source"),
            },
            "raw_data": raw["raw"],
            "block_number": height,
            "block_hash": block_hash,
            "event_index": raw["event_index"],
            "runtime_spec_version": spec_version,
            "chain_status": "observed",
            "enrichment_status": "pending" if rule.get("enrichment") != "none" else "complete",
            "confidence": rule.get("confidence", "high"),
        }

    def _worker_substrate(self) -> Any:
        cached = getattr(self._worker_local, "client", None)
        if cached and cached[0] == self._connection_generation:
            return cached[1]
        if cached:
            try:
                cached[1].close()
            except Exception:
                pass
        substrate = self._new_substrate()
        self._worker_local.client = (self._connection_generation, substrate)
        return substrate

    def _block_events(self, block_hash: str, substrate: Any | None = None) -> list[Any]:
        substrate = substrate or self.substrate
        substrate.init_runtime(block_hash=block_hash)
        return substrate.get_events(block_hash=block_hash)

    def _block_time(self, block_hash: str, substrate: Any | None = None) -> str:
        substrate = substrate or self.substrate
        try:
            value = substrate.query(module="Timestamp", storage_function="Now", block_hash=block_hash)
            return _timestamp(_json_value(value))
        except Exception:
            LOG.warning("Timestamp lookup failed at block %s; using observation time", block_hash)
            return now_iso()

    def _identity_snapshot(self, netuid: int, block_hash: str, substrate: Any | None = None) -> Any:
        substrate = substrate or self.substrate
        if not hasattr(substrate, "query"):
            raise RuntimeError("storage queries are unavailable")
        for storage in ("SubnetIdentitiesV3", "SubnetIdentitiesV2", "SubnetIdentities"):
            try:
                result = substrate.query(module="SubtensorModule", storage_function=storage,
                                         params=[netuid], block_hash=block_hash)
                value = _json_value(result)
                if value is not None:
                    return value
            except Exception:
                continue
        return None

    def sync_identity_repositories(self, block_hash: str | None = None,
                                   substrate: Any | None = None) -> int:
        """Import repositories explicitly registered in current subnet identities."""
        substrate = substrate or self.substrate
        if substrate is None or not hasattr(substrate, "query_map"):
            return 0
        imported = 0
        seen_repository_ids: set[int] = set()
        completed = False
        try:
            records = _scan_subnet_identities(substrate, block_hash)
            imported, seen_repository_ids = self._apply_identity_records(records, block_hash)
            completed = True
        except Exception:
            LOG.warning("Fast subnet identity scan failed; falling back to metadata decoding", exc_info=True)
        for storage_name in ("SubnetIdentitiesV3", "SubnetIdentitiesV2", "SubnetIdentities"):
            if completed:
                break
            try:
                kwargs: dict[str, Any] = {
                    "module": "SubtensorModule",
                    "storage_function": storage_name,
                }
                if block_hash:
                    kwargs["block_hash"] = block_hash
                entries = substrate.query_map(**kwargs)
                for netuid, identity in entries:
                    parsed = _identity_repository(identity)
                    number = _number(_json_value(netuid))
                    if number is None or parsed is None:
                        continue
                    owner, name = parsed
                    repository = self.store.upsert_chain_repository({
                        "network": self.network,
                        "netuid": number,
                        "owner": owner,
                        "name": name,
                        "html_url": f"https://github.com/{owner}/{name}",
                    })
                    seen_repository_ids.add(int(repository["repository_id"]))
                    imported += 1
                completed = True
                break
            except Exception:
                LOG.debug("Subnet identity storage %s was not iterable", storage_name, exc_info=True)
        if completed:
            self.store.reconcile_chain_repositories(self.network, seen_repository_ids)
        self.last_repository_sync = time.monotonic()
        return imported

    def _apply_identity_records(self, records: list[tuple[int, dict[str, str]]],
                                block_hash: str | None = None) -> tuple[int, set[int]]:
        imported = 0
        seen_repository_ids: set[int] = set()
        for netuid, identity in records:
            profile = _identity_to_profile(identity)
            self.store.upsert_subnet_profile_auto(
                self.network, int(netuid), profile, block_hash,
            )
            parsed = parse_github_repository(identity.get("github_repo"))
            if parsed is None:
                continue
            owner, name = parsed
            repository = self.store.upsert_chain_repository({
                "network": self.network,
                "netuid": int(netuid),
                "owner": owner,
                "name": name,
                "html_url": f"https://github.com/{owner}/{name}",
            })
            seen_repository_ids.add(int(repository["repository_id"]))
            imported += 1
        return imported, seen_repository_ids

    def start_repository_sync(self, block_hash: str | None) -> bool:
        with self._repository_sync_lock:
            if self._repository_sync_thread and self._repository_sync_thread.is_alive():
                return False
            self.last_repository_sync = time.monotonic()
            self.status("profile_sync", "running", {
                "message": "正在校对子网编号、名称和关联链接",
                "interval_seconds": self.profile_sync_seconds,
                "last_synced_at": self.last_profile_sync_at,
                "started_at": now_iso(),
            })
            self._repository_sync_thread = threading.Thread(
                target=self._sync_repositories_background,
                args=(block_hash,),
                name=f"repository-sync-{self.network}",
                daemon=True,
            )
            self._repository_sync_thread.start()
            return True

    def _sync_repositories_background(self, block_hash: str | None) -> None:
        try:
            records = _scan_subnet_identities_http(self.url, block_hash)
            _, seen_repository_ids = self._apply_identity_records(records, block_hash)
            self.store.reconcile_chain_repositories(self.network, seen_repository_ids)
            self.last_repository_sync = time.monotonic()
            self.last_profile_sync_at = now_iso()
            self.store.resolve_alerts("chain_repositories")
            self.status("profile_sync", "connected", {
                "message": "子网档案校对完成",
                "profiles_found": len(records),
                "repositories_found": len(seen_repository_ids),
                "last_synced_at": self.last_profile_sync_at,
                "interval_seconds": self.profile_sync_seconds,
            })
        except Exception as exc:
            self.last_repository_sync = 0.0
            LOG.exception("Could not import current subnet identity repositories")
            self.status("profile_sync", "degraded", {
                "message": "子网档案校对失败",
                "error": str(exc)[:400],
                "last_synced_at": self.last_profile_sync_at,
                "interval_seconds": self.profile_sync_seconds,
            })
            self.store.add_alert(
                "chain_repositories", "warning",
                f"子网仓库身份同步失败：{exc}"[:400],
            )

    def start_trading_sync(self, block_hash: str | None) -> None:
        with self._trading_sync_lock:
            if self._trading_sync_thread and self._trading_sync_thread.is_alive():
                return
            self.last_trading_sync = time.monotonic()
            self._trading_sync_thread = threading.Thread(
                target=self._sync_trading_background,
                args=(block_hash,),
                name=f"trading-sync-{self.network}",
                daemon=True,
            )
            self._trading_sync_thread.start()

    def _sync_trading_background(self, block_hash: str | None) -> None:
        try:
            records, height = _scan_trading_states_http(self.url, block_hash)
            current_netuids: set[int] = set()
            disabled_netuids = self.store.disabled_netuids(self.network)
            for record in records:
                current_netuids.add(int(record["netuid"]))
                self.store.ensure_subnet_profile(self.network, int(record["netuid"]))
                previous = self.store.get_trading_state(self.network, int(record["netuid"]))
                previous_source = previous.get("state_source") if previous else None
                self.store.set_trading_state(
                    network=self.network,
                    netuid=record["netuid"],
                    enabled=record["enabled"],
                    registration_block=record["registration_block"],
                    block_hash=record["block_hash"],
                    block_number=height,
                    emission_enabled=record.get("emission_enabled"),
                )
                if (
                    int(record["netuid"]) not in disabled_netuids
                    and previous is not None
                    and previous_source == "subtoken_enabled"
                ):
                    if previous["enabled"] != record["enabled"]:
                        self._emit_trading_change(record, previous, height)
                    previous_emission = previous.get("emission_enabled")
                    current_emission = record.get("emission_enabled")
                    if (
                        previous_emission is not None
                        and current_emission is not None
                        and bool(previous_emission) != bool(current_emission)
                    ):
                        self._emit_emission_change(record, previous, height)
            self.store.reconcile_subnet_profiles(self.network, current_netuids)
            self.last_trading_sync = time.monotonic()
            self.store.resolve_alerts("chain_trading")
        except Exception as exc:
            self.last_trading_sync = 0.0
            LOG.exception("Could not sync subnet trading states")
            self.store.add_alert(
                "chain_trading", "warning",
                f"交易开放状态同步失败：{exc}"[:400],
            )

    def _emit_emission_change(self, record: dict[str, Any], previous: dict[str, Any],
                              height: int) -> None:
        enabled = bool(record.get("emission_enabled"))
        event_id = hashlib.sha256(
            f"{self.network}:emission:{record['netuid']}:{record['block_hash']}:{enabled}".encode()
        ).hexdigest()
        event = {
            "event_id": event_id,
            "network": self.network,
            "netuid": int(record["netuid"]),
            "event_type": "排放开关",
            "event_name": "SubtensorModule.SubnetEmissionEnabled",
            "source": "chain",
            "source_url": (
                "https://polkadot.js.org/apps/"
                "?rpc=wss%3A%2F%2Fentrypoint-finney.opentensor.ai%3A443"
                f"#/explorer/query/{record['block_hash']}"
            ),
            "event_at": now_iso(),
            "summary": (
                f"SN{record['netuid']} 已开启子网排放"
                if enabled else f"SN{record['netuid']} 已关闭子网排放"
            ),
            "details": {
                "field": "SubnetEmissionEnabled",
                "previous": bool(previous.get("emission_enabled")),
                "current": enabled,
                "detection_method": "state_diff",
                "detected_at_block": height,
                "previous_observed_at": previous.get("emission_observed_at"),
            },
            "raw_data": {"record": record, "previous": previous},
            "block_number": height,
            "block_hash": record["block_hash"],
            "event_index": None,
            "chain_status": "observed",
            "enrichment_status": "complete",
            "confidence": "high",
        }
        stored, created = self.store.create_event(event)
        if created:
            self.publish(stored, "discovered")

    def _emit_trading_change(self, record: dict[str, Any], previous: dict[str, Any],
                             height: int) -> None:
        enabled = bool(record["enabled"])
        event_id = hashlib.sha256(
            f"{self.network}:subtoken:{record['netuid']}:{record['block_hash']}:{enabled}".encode()
        ).hexdigest()
        event = {
            "event_id": event_id,
            "network": self.network,
            "netuid": int(record["netuid"]),
            "event_type": "交易开放",
            "event_name": "SubtensorModule.SubtokenEnabled",
            "source": "chain",
            "source_url": (
                "https://polkadot.js.org/apps/"
                "?rpc=wss%3A%2F%2Fentrypoint-finney.opentensor.ai%3A443"
                f"#/explorer/query/{record['block_hash']}"
            ),
            "event_at": now_iso(),
            "summary": (
                f"SN{record['netuid']} 已开放交易"
                if enabled else f"SN{record['netuid']} 已关闭交易"
            ),
            "details": {
                "field": "SubtokenEnabled",
                "previous": previous["enabled"],
                "current": enabled,
                "detection_method": "state_diff",
                "detected_at_block": height,
                "previous_observed_at": previous.get("observed_at"),
            },
            "raw_data": {"record": record, "previous": previous},
            "block_number": height,
            "block_hash": record["block_hash"],
            "event_index": None,
            "chain_status": "observed",
            "enrichment_status": "complete",
            "confidence": "high",
        }
        stored, created = self.store.create_event(event)
        if created:
            self.publish(stored, "discovered")

    def enrich_identity(self, event: dict[str, Any], parent_hash: str,
                        substrate: Any | None = None) -> dict[str, Any]:
        netuid = event.get("netuid")
        if netuid is None:
            return event
        before = self._identity_snapshot(int(netuid), parent_hash, substrate)
        after = self._identity_snapshot(int(netuid), event["block_hash"], substrate)
        details = dict(event.get("details", {}))
        details["identity_before"] = before
        details["identity_after"] = after
        if isinstance(after, dict):
            self.store.upsert_subnet_profile_auto(
                self.network,
                int(netuid),
                _identity_to_profile({
                    str(key): value for key, value in after.items()
                }),
                event.get("block_hash"),
            )
        changes = {}
        before_dict = before if isinstance(before, dict) else {}
        after_dict = after if isinstance(after, dict) else {}
        for key in sorted(set(before_dict) | set(after_dict)):
            if before_dict.get(key) != after_dict.get(key):
                changes[key] = {"before": before_dict.get(key), "after": after_dict.get(key)}
        details["identity_changes"] = changes
        if changes:
            name_change = next((value for key, value in changes.items() if key.casefold() in ("subnet_name", "name")), None)
            if name_change:
                event["summary"] = f"SN{netuid} 名称变更：{name_change.get('before') or '（无）'} → {name_change.get('after') or '（无）'}"
            else:
                keys = "、".join(changes)
                event["summary"] = f"SN{netuid} 身份信息已变更：{keys}"
        else:
            event["summary"] = f"SN{netuid} 身份信息已{'移除' if event['event_name'].endswith('SubnetIdentityRemoved') else '设置'}；状态差异未发现字段变化"
        event["details"] = details
        event["enrichment_status"] = "complete"
        event["last_enriched_at"] = now_iso()
        repository = parse_github_repository(
            _field_by_key(after_dict, ("github_repo", "github", "repo"))
        )
        if repository:
            owner, name = repository
            stored_repository = self.store.upsert_chain_repository({
                "network": self.network,
                "netuid": int(netuid),
                "owner": owner,
                "name": name,
                "html_url": f"https://github.com/{owner}/{name}",
            })
            self.store.deactivate_chain_repositories_for_netuid(
                self.network, int(netuid), int(stored_repository["repository_id"])
            )
        else:
            self.store.deactivate_chain_repositories_for_netuid(self.network, int(netuid))
        return event

    def _fetch_block(self, height: int, substrate: Any | None = None) -> dict[str, Any]:
        substrate = substrate or self._worker_substrate()
        block_hash_data = _result(substrate.rpc_request("chain_getBlockHash", [height]))
        if not block_hash_data:
            raise RuntimeError(f"RPC returned no block hash for height {height}")
        block_hash = str(block_hash_data)
        header = _result(substrate.rpc_request("chain_getHeader", [block_hash]))
        if not isinstance(header, dict):
            raise RuntimeError(f"Could not load header for block {height}")
        parent_hash = str(header.get("parentHash", ""))
        runtime = _result(substrate.rpc_request("state_getRuntimeVersion", [block_hash]))
        spec_version = int(runtime["specVersion"]) if runtime else None
        try:
            self._capture_runtime_metadata(substrate, block_hash, spec_version)
        except Exception as exc:
            self.store.add_alert(
                "chain_metadata", "warning",
                f"运行时元数据保存失败：{exc}"[:400],
            )
        events = self._block_events(block_hash, substrate)
        block_at = self._block_time(block_hash, substrate)
        disabled_netuids = self.store.disabled_netuids(self.network)
        watched_addresses = self.store.watched_addresses_with_global_threshold(
            self.network, enabled_only=True
        )
        raws, mapped = self._map_block_events(
            events,
            watched_addresses=watched_addresses,
            disabled_netuids=disabled_netuids,
            block_hash=block_hash,
            height=height,
            spec_version=spec_version,
            event_at=block_at,
        )
        return {
            "height": height, "block_hash": block_hash, "parent_hash": parent_hash,
            "spec_version": spec_version, "raws": raws, "events": mapped,
        }

    def _map_block_events(self, events: list[Any], *, watched_addresses: list[dict[str, Any]],
                          disabled_netuids: set[int], block_hash: str, height: int,
                          spec_version: int | None, event_at: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        prepared: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        stake_extrinsics: set[int] = set()
        for fallback, item in enumerate(events):
            event_index = self._event_index(item, fallback)
            raw = self._raw_record(item, block_hash, height, event_index)
            address_event = address_event_record(
                raw,
                network=self.network,
                watched_addresses=watched_addresses,
                block_hash=block_hash,
                height=height,
                spec_version=spec_version,
                event_at=event_at,
            )
            if address_event is not None and raw["event_name"] in {
                "StakeAdded", "StakeRemoved", "StakeTransferred", "StakeAndHotkeyTransferred",
            }:
                extrinsic_index = self._raw_extrinsic_index(raw)
                if extrinsic_index is not None:
                    stake_extrinsics.add(extrinsic_index)
            prepared.append((raw, address_event))

        # Some runtime stake transfers emit only StakeRemoved. Once the local
        # index confirms the destination coldkey, add a deterministic StakeAdded
        # event so the UI shows both source sell and destination buy.
        actual_added = {
            self._raw_stake_added_signature(raw)
            for raw, _ in prepared
            if raw["event_name"] == "StakeAdded"
        }
        synthetic_raw_ids: set[str] = set()
        for raw, address_event in list(prepared):
            if address_event is None or raw["event_name"] != "StakeRemoved":
                continue
            derived_raw = derived_stake_transfer_buy_raw(raw)
            if derived_raw is None:
                continue
            if self._raw_stake_added_signature(derived_raw) in actual_added:
                continue
            derived_event = address_event_record(
                derived_raw,
                network=self.network,
                watched_addresses=watched_addresses,
                block_hash=block_hash,
                height=height,
                spec_version=spec_version,
                event_at=event_at,
            )
            if derived_event is None:
                continue
            prepared.append((derived_raw, derived_event))
            synthetic_raw_ids.add(str(derived_raw["raw_id"]))

        raws: list[dict[str, Any]] = []
        mapped: list[dict[str, Any]] = []
        for raw, address_event in prepared:
            synthetic = str(raw["raw_id"]) in synthetic_raw_ids
            if (
                address_event is not None
                and raw["module"] == "Balances"
                and raw["event_name"] == "Transfer"
                and self._raw_extrinsic_index(raw) in stake_extrinsics
            ):
                address_event = None
            raw_added = False
            if address_event is not None:
                if not synthetic:
                    raws.append(raw)
                mapped.append(address_event)
                raw_added = True
            if synthetic:
                continue
            normalized = self._event_record(raw, block_hash, height, spec_version, event_at)
            normalized_rule = str((normalized or {}).get("details", {}).get("rule_id") or "")
            lifecycle_event = normalized_rule in {
                "network_registered", "network_removed", "subnet_identity_set",
            }
            if normalized and (
                lifecycle_event
                or normalized.get("netuid") is None
                or int(normalized["netuid"]) not in disabled_netuids
            ):
                owner_tags = project_owner_tags_for_raw_event(raw, watched_addresses)
                if owner_tags:
                    normalized["summary"] = append_project_owner_suffix(
                        normalized.get("summary") or "", owner_tags
                    )
                    normalized_details = dict(normalized.get("details") or {})
                    normalized_details["project_owner_tags"] = owner_tags
                    normalized["details"] = normalized_details
                if not raw_added:
                    raws.append(raw)
                mapped.append(normalized)
        return raws, mapped

    @staticmethod
    def _raw_stake_added_signature(raw: dict[str, Any]) -> tuple[str, str, int, int] | None:
        data = raw.get("raw") or {}
        values = data.get("attributes") or {}
        def field(name: str, index: int) -> Any:
            if isinstance(values, dict):
                return values.get(name, values.get(str(index), values.get(index)))
            if isinstance(values, list) and 0 <= index < len(values):
                return values[index]
            return None
        try:
            return (
                str(field("coldkey", 0) or ""),
                str(field("hotkey", 1) or ""),
                int(field("netuid", 4)),
                int(field("tao_amount", 2)),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _raw_extrinsic_index(raw: dict[str, Any]) -> int | None:
        data = raw.get("raw") or {}
        value = data.get("extrinsic_idx")
        if value is None and isinstance(data.get("value"), dict):
            value = data["value"].get("extrinsic_idx")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _persist_block(self, fetched: dict[str, Any]) -> list[dict[str, Any]]:
        height = int(fetched["height"])
        block_hash = str(fetched["block_hash"])
        parent_hash = str(fetched["parent_hash"])
        spec_version = fetched.get("spec_version")
        raws = fetched["raws"]
        mapped = fetched["events"]
        created = self.store.process_chain_block(self.network, height, block_hash, parent_hash, spec_version, raws, mapped)
        for event in created:
            self.publish(event, "discovered")
            rule_id = (event.get("details") or {}).get("rule_id")
            netuid = event.get("netuid")
            if rule_id == "network_registered" and netuid is not None:
                self.store.reset_subnet_profile_for_registration(
                    self.network, int(netuid), archive_key=f"event:{event['event_id']}",
                )
                self.start_repository_sync(block_hash)
            elif rule_id == "network_removed" and netuid is not None:
                self.store.archive_subnet_profile(
                    self.network, int(netuid), "network_removed",
                    archive_key=f"event:{event['event_id']}",
                )
                self.store.deactivate_chain_repositories_for_netuid(
                    self.network, int(netuid)
                )
            if (
                rule_id in {"network_registered", "network_removed", "subnet_identity_set"}
                and netuid is not None
                and callable(self.subnet_lifecycle_callback)
            ):
                try:
                    self.subnet_lifecycle_callback(rule_id, int(netuid), dict(event))
                except Exception:
                    LOG.exception("Subnet lifecycle callback failed for SN%s", netuid)
            if (
                event.get("enrichment_status") == "pending"
                and (netuid is not None or rule_id == "coldkey_swapped")
            ):
                self._enrichment_executor.submit(self._enrich_and_update, event)
        self.last_spec_version = spec_version
        return created

    def _enrich_and_update(self, event: dict[str, Any]) -> None:
        try:
            substrate = self._worker_substrate()
            block_hash = event["block_hash"]
            if (event.get("details") or {}).get("rule_id") == "coldkey_swapped":
                enriched = self.enrich_coldkey_swap(dict(event), block_hash, substrate)
            else:
                header = _result(substrate.rpc_request("chain_getHeader", [block_hash]))
                parent_hash = str(header.get("parentHash", ""))
                enriched = self.enrich_identity(dict(event), parent_hash, substrate)
            patch = {
                "summary": enriched["summary"],
                "details": enriched.get("details", {}),
                "netuid": enriched.get("netuid", event.get("netuid")),
                "last_enriched_at": enriched.get("last_enriched_at", now_iso()),
                "enrichment_status": enriched.get("enrichment_status", "complete"),
            }
            updated = self.store.update_event(event["event_id"], "enriched", patch)
            if updated:
                self.publish(updated, "enriched")
        except Exception as exc:
            LOG.exception("Identity enrichment failed for %s", event.get("event_id"))
            failed = self.store.update_event(
                event["event_id"], "enrichment_failed",
                {"enrichment_status": "failed", "last_enriched_at": now_iso(),
                 "details": {**event.get("details", {}), "enrichment_error": str(exc)[:400]}},
            )
            if failed:
                self.publish(failed, "enrichment_failed")
            self.store.add_alert("chain_enrichment", "warning", f"身份补全失败：{exc}"[:400])

    def enrich_coldkey_swap(self, event: dict[str, Any], block_hash: str,
                            substrate: Any) -> dict[str, Any]:
        fields = (event.get("details") or {}).get("fields") or {}
        old_coldkey = str(fields.get("old_coldkey") or "")
        new_coldkey = str(fields.get("new_coldkey") or "")
        netuids: list[int] = []
        for key, owner in substrate.query_map(
            module="SubtensorModule",
            storage_function="SubnetOwner",
            block_hash=block_hash,
        ):
            if _account_equal(new_coldkey, owner):
                netuid = _number(getattr(key, "value", key))
                if netuid is not None:
                    netuids.append(int(netuid))
        netuids = sorted(set(netuids))
        if netuids:
            subnet_text = "、".join(f"SN{netuid}" for netuid in netuids)
            summary = (
                f"{subnet_text} 子网冷键交换："
                f"{mask_address(old_coldkey)} → {mask_address(new_coldkey)}"
            )
        else:
            summary = (
                f"SN未定位 检测到冷键交换：{mask_address(old_coldkey)} → "
                f"{mask_address(new_coldkey)}；未定位到已登记子网所有权"
            )
        details = dict(event.get("details") or {})
        details.update({
            "affected_subnets": netuids,
            "coldkey_swap": {
                "old_coldkey": old_coldkey,
                "new_coldkey": new_coldkey,
            },
            "lookup": "SubtensorModule.SubnetOwner at event block",
        })
        return {
            "netuid": netuids[0] if len(netuids) == 1 else event.get("netuid"),
            "summary": summary,
            "details": details,
            "enrichment_status": "complete",
            "last_enriched_at": now_iso(),
        }

    def process_block(self, height: int) -> list[dict[str, Any]]:
        return self._persist_block(self._fetch_block(height))

    def _ensure_event_parser_backfill(self, *, backfill_blocks: int | None = None) -> None:
        """One-time rewind after fixing the dict-event parser.

        Older releases received plain dict events from the Substrate client but only
        normalized ScaleValue objects, so live chain events were silently skipped.
        Replaying a recent window restores the most recent missed address events.
        """

        state = self.store.get_cursor(EVENT_PARSER_STATE) or {}
        try:
            version = int(state.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= 2:
            return
        cursor = self.store.get_cursor(f"chain:{self.network}")
        if not cursor:
            self.store.set_collector_state(EVENT_PARSER_STATE, {
                "version": 2,
                "reason": "fresh event database",
                "updated_at": now_iso(),
            })
            return
        limit = EVENT_PARSER_BACKFILL_BLOCKS if backfill_blocks is None else max(0, int(backfill_blocks))
        if limit <= 0:
            self.store.set_collector_state(EVENT_PARSER_STATE, {
                "version": 2,
                "reason": "backfill disabled",
                "updated_at": now_iso(),
            })
            return
        best = self.rpc("chain_getHeader", [])
        if not isinstance(best, dict):
            raise RuntimeError("chain_getHeader returned no best header during parser backfill")
        target = _height(best)
        rewind_to = max(1, target - limit)
        anchor_height = rewind_to - 1
        if anchor_height < 1:
            self.store.set_collector_state(EVENT_PARSER_STATE, {
                "version": 2,
                "reason": "chain too short",
                "updated_at": now_iso(),
            })
            return
        anchor_hash = self.hash_at(anchor_height)
        anchor_header = self.rpc("chain_getHeader", [anchor_hash])
        anchor_version = self.version_at(anchor_hash)
        self.store.process_chain_block(
            self.network,
            anchor_height,
            anchor_hash,
            str((anchor_header or {}).get("parentHash", "")),
            anchor_version,
            [],
            [],
        )
        self.store.set_collector_state(EVENT_PARSER_STATE, {
            "version": 2,
            "reason": "dict event parser migration",
            "anchor_height": anchor_height,
            "rewind_to": rewind_to,
            "target_at_reset": target,
            "backfill_blocks": limit,
            "updated_at": now_iso(),
        })
        self.status("chain", "connected", {
            "network": self.network,
            "best_height": target,
            "last_processed": anchor_height,
            "finalized_height": None,
            "lag_blocks": max(0, target - anchor_height),
            "processed": 0,
            "syncing": True,
            "parser_backfill": True,
            "updated_at": now_iso(),
            "state": "connected",
        })
        LOG.info(
            "Rewound event cursor from %s to %s to replay %s blocks after dict-event parser fix",
            cursor.get("height"), anchor_height, limit,
        )

    def _report_progress(self, target: int, processed: int) -> None:
        fresh = self.store.get_cursor(f"chain:{self.network}") or {}
        current_height = int(fresh.get("height") or 0)
        self.status("chain", "connected", {
            "network": self.network,
            "best_height": target,
            "last_processed": current_height,
            "finalized_height": None,
            "spec_version": self.last_spec_version or fresh.get("spec_version"),
            "lag_blocks": max(0, target - current_height),
            "processed": processed,
            "syncing": current_height < target,
            "updated_at": now_iso(),
            "state": "connected",
        })

    def poll_once(self) -> dict[str, Any]:
        best = self.rpc("chain_getHeader", [])
        if not isinstance(best, dict):
            raise RuntimeError("chain_getHeader returned no best header")
        target = _height(best)
        cursor = self.store.get_cursor(f"chain:{self.network}")
        if not cursor:
            anchor_hash = self.hash_at(target)
            anchor_header = self.rpc("chain_getHeader", [anchor_hash])
            anchor_version = self.version_at(anchor_hash)
            self.store.process_chain_block(self.network, target, anchor_hash, str(anchor_header.get("parentHash", "")), anchor_version, [], [])
            LOG.info("Initialized live-only collection at Finney block %s", target)
            cursor = self.store.get_cursor(f"chain:{self.network}")
            self.start_repository_sync(anchor_hash)
        last = int(cursor["height"])
        if target >= last:
            current_hash = self.hash_at(last)
            if current_hash.lower() != str(cursor["hash"]).lower():
                common = self.store.find_common_block(self.network, self.hash_at, last)
                if common is None:
                    raise RuntimeError("Reorg passed the local chain anchor; manual collector recovery is required")
                for event in self.store.retract_after(self.network, int(common["height"])):
                    self.publish(event, "retracted")
                cursor = self.store.get_cursor(f"chain:{self.network}")
                last = int(cursor["height"])
        elif target < last:
            common = self.store.find_common_block(self.network, self.hash_at, min(target, last))
            if common is None:
                raise RuntimeError("Best chain is shorter than the local anchor; manual collector recovery is required")
            for event in self.store.retract_after(self.network, int(common["height"])):
                self.publish(event, "retracted")
            cursor = self.store.get_cursor(f"chain:{self.network}")
            last = int(cursor["height"])
        upper = min(target, last + self.max_blocks_per_poll)
        heights = list(range(last + 1, upper + 1))
        processed = 0
        if heights:
            try:
                # Use the already-initialized primary connection for the oldest block so
                # the first matching event reaches the panel before workers warm up.
                self._persist_block(self._fetch_block(heights[0], self.substrate))
                processed += 1
                self._report_progress(target, processed)
                futures = [self._executor.submit(self._fetch_block, height) for height in heights[1:]]
                for future in futures:
                    self._persist_block(future.result())
                    processed += 1
                    self._report_progress(target, processed)
            except Exception as exc:
                if self._state_was_discarded(exc) and last < target:
                    return self._recover_to_live_head(target, last, exc)
                raise
        finalized_hash = self.rpc("chain_getFinalizedHead", [])
        finalized_height = None
        if finalized_hash:
            finalized_header = self.rpc("chain_getHeader", [finalized_hash])
            finalized_height = _height(finalized_header)
            fresh_height = int((self.store.get_cursor(f"chain:{self.network}") or {}).get("height", 0))
            safe_finalized_height = min(finalized_height, fresh_height, target)
            for event in self.store.finalize_through(self.network, safe_finalized_height):
                self.publish(event, "finalized")
        fresh = self.store.get_cursor(f"chain:{self.network}") or {}
        self.store.resolve_alerts("chain")
        snapshot = {"network": self.network, "best_height": target, "last_processed": fresh.get("height"),
                    "finalized_height": safe_finalized_height if finalized_hash else None,
                    "spec_version": self.last_spec_version or fresh.get("spec_version"),
                    "lag_blocks": max(0, target - int(fresh.get("height", 0))), "processed": processed,
                    "updated_at": now_iso(), "state": "connected"}
        self.status("chain", "connected", snapshot)
        return snapshot

    @staticmethod
    def _state_was_discarded(exc: Exception) -> bool:
        message = str(exc).casefold()
        return (
            "state already discarded" in message
            or "unknown block" in message
            or "unknownblock" in message
        )

    def _recover_to_live_head(self, target: int, previous_height: int,
                              error: Exception) -> dict[str, Any]:
        anchor_hash = self.hash_at(target)
        anchor_header = self.rpc("chain_getHeader", [anchor_hash])
        anchor_version = self.version_at(anchor_hash)
        self.store.process_chain_block(
            self.network, target, anchor_hash,
            str(anchor_header.get("parentHash", "")),
            anchor_version, [], [],
        )
        self.last_spec_version = anchor_version
        self.last_trading_sync = 0.0
        self.last_repository_sync = 0.0
        self.store.resolve_alerts("chain")
        self.store.add_log(
            "WARNING", "app.chain",
            (
                f"Historical state was discarded at block {previous_height + 1}; "
                f"fast-forwarded live cursor to {target}"
            ),
        )
        snapshot = {
            "network": self.network,
            "best_height": target,
            "last_processed": target,
            "finalized_height": target,
            "spec_version": anchor_version,
            "lag_blocks": 0,
            "processed": 0,
            "recovered": True,
            "previous_height": previous_height,
            "skipped_blocks": max(0, target - previous_height),
            "reason": str(error)[:300],
            "updated_at": now_iso(),
            "state": "connected",
        }
        self.status("chain", "connected", snapshot)
        LOG.warning(
            "Fast-forwarded Finney cursor from %s to %s because historical state was discarded",
            previous_height, target,
        )
        return snapshot

    def run(self, stop: Any) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                if self.substrate is None:
                    self.connect()
                self._ensure_event_parser_backfill()
                if time.monotonic() - self.last_trading_sync >= 5:
                    self.start_trading_sync(None)
                self.poll_once()
                if time.monotonic() - self.last_repository_sync >= self.profile_sync_seconds:
                    self.start_repository_sync(None)
                backoff = 1.0
                stop.wait(self.poll_seconds)
            except Exception as exc:
                LOG.exception("Chain collector error")
                self.status("chain", "degraded", {"error": str(exc), "updated_at": now_iso()})
                self.store.add_alert("chain", "critical", "RPC unavailable" if "connect" in str(exc).lower() else str(exc)[:400])
                try:
                    if self.substrate is not None:
                        self.substrate.close()
                except Exception:
                    pass
                self.substrate = None
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._enrichment_executor.shutdown(wait=False, cancel_futures=True)
        try:
            if self.substrate is not None:
                self.substrate.close()
        except Exception:
            pass
        self.substrate = None


def _field_by_key(value: Any, keys: tuple[str, ...]) -> Any:
    normalized = {key.casefold() for key in keys}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in normalized:
                return item
        for item in value.values():
            found = _field_by_key(item, keys)
            if found is not None:
                return found
    return None


def _account_equal(expected: Any, actual: Any) -> bool:
    expected_candidates = _account_candidates(expected)
    actual_candidates = _account_candidates(actual)
    return bool(expected_candidates.intersection(actual_candidates))


def _account_candidates(value: Any) -> set[str]:
    if value is None:
        return set()
    candidates: set[str] = set()
    raw_value = getattr(value, "value", value)
    for item in (
        value,
        raw_value,
        getattr(value, "ss58_address", None),
        getattr(value, "public_key", None),
    ):
        if item is None:
            continue
        if isinstance(item, bytes):
            candidates.add("0x" + item.hex())
            continue
        text = str(item).strip()
        if not text:
            continue
        candidates.add(text)
        if text.startswith("0x"):
            try:
                candidates.add("0x" + bytes.fromhex(text[2:]).hex())
            except ValueError:
                pass
    return candidates


def _identity_repository(value: Any) -> tuple[str, str] | None:
    plain = _json_value(value)
    return parse_github_repository(
        _field_by_key(plain, ("github_repo", "github", "repo"))
    )


X_URL = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,30})",
    re.IGNORECASE,
)


def _identity_to_profile(identity: dict[str, str]) -> dict[str, Any]:
    github_raw = str(identity.get("github_repo") or "").strip()
    github = parse_github_repository(github_raw)
    values = {
        "name": str(identity.get("subnet_name") or "").strip(),
        "github_url": f"https://github.com/{github[0]}/{github[1]}" if github else "",
        "github_raw": github_raw,
        "website_url": str(identity.get("subnet_url") or "").strip(),
        "x_url": _extract_x_url(identity),
        "discord_url": str(identity.get("discord") or "").strip(),
        "description": str(identity.get("description") or "").strip(),
        "logo_url": str(identity.get("logo_url") or "").strip(),
        "additional": str(identity.get("additional") or "").strip(),
    }
    values["_sources"] = {
        field: ("chain_identity.additional" if field == "x_url" else "chain_identity")
        for field, value in values.items()
        if value
    }
    return values


def _extract_x_url(identity: dict[str, str]) -> str:
    ordered = [
        identity.get("additional"),
        identity.get("description"),
        identity.get("subnet_url"),
    ]
    ordered.extend(
        value for key, value in identity.items()
        if key not in {"additional", "description", "subnet_url"}
    )
    for text in ordered:
        match = X_URL.search(str(text or ""))
        if match:
            return f"https://x.com/{match.group(1)}"
    for text in ordered:
        match = re.search(
            r"(?:^|\b)X\s*:\s*@([A-Za-z0-9_]{1,30})",
            str(text or ""),
            re.IGNORECASE,
        )
        if match:
            return f"https://x.com/{match.group(1)}"
    return ""


def _twox128(value: bytes) -> bytes:
    return b"".join(
        struct.pack("<Q", xxhash.xxh64(value, seed=seed).intdigest())
        for seed in (0, 1)
    )


def _scan_subnet_identities(substrate: Any, block_hash: str | None) -> list[tuple[int, dict[str, str]]]:
    """Read the current V3 identity map in two RPCs instead of one call per subnet."""
    if not block_hash:
        block_hash = _result(substrate.rpc_request("chain_getBlockHash", []))
    prefix = "0x" + (
        _twox128(b"SubtensorModule") + _twox128(b"SubnetIdentitiesV3")
    ).hex()
    keys = _result(substrate.rpc_request("state_getKeysPaged", [prefix, 1000, None, block_hash]))
    if not keys:
        return []
    batches: list[tuple[list[str], dict[str, str]]] = []
    for offset in range(0, len(keys), 100):
        batch = keys[offset:offset + 100]
        response = _result(substrate.rpc_request("state_queryStorageAt", [batch, block_hash]))
        changes = {}
        if isinstance(response, list) and response:
            changes = {
                str(key): str(value)
                for key, value in response[0].get("changes", [])
                if value
            }
        batches.append((batch, changes))
    records = []
    for batch, changes in batches:
        for key in batch:
            value = changes.get(key)
            if not value:
                continue
            netuid = int.from_bytes(bytes.fromhex(key[-4:]), "little")
            identity = _decode_identity_v3(value)
            if identity:
                records.append((netuid, identity))
    return records


def _scan_subnet_identities_http(url: str, block_hash: str | None) -> list[tuple[int, dict[str, str]]]:
    """Scan the identity map over the HTTP RPC endpoint without blocking event collection."""
    parsed = urlparse(url)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    endpoint = urlunparse(parsed._replace(scheme=scheme))

    def rpc(method: str, params: list[Any]) -> Any:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            endpoint, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "bittensor-event-monitor/0.1"},
        )
        last_error = None
        for _ in range(2):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if data.get("error"):
                    raise RuntimeError(data["error"].get("message", "HTTP RPC error"))
                return data.get("result")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"HTTP RPC failed: {last_error}")

    if not block_hash:
        block_hash = rpc("chain_getBlockHash", [])
    prefix = "0x" + (
        _twox128(b"SubtensorModule") + _twox128(b"SubnetIdentitiesV3")
    ).hex()
    keys = rpc("state_getKeysPaged", [prefix, 1000, None, block_hash]) or []
    records = []
    for offset in range(0, len(keys), 100):
        batch = keys[offset:offset + 100]
        response = rpc("state_queryStorageAt", [batch, block_hash]) or []
        changes = {
            str(key): str(value)
            for key, value in (response[0].get("changes", []) if response else [])
            if value
        }
        for key in batch:
            value = changes.get(key)
            if not value:
                continue
            netuid = int.from_bytes(bytes.fromhex(key[-4:]), "little")
            identity = _decode_identity_v3(value)
            if identity:
                records.append((netuid, identity))
    return records


def _scan_trading_states_http(url: str, block_hash: str | None) -> tuple[list[dict[str, Any]], int]:
    parsed = urlparse(url)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    endpoint = urlunparse(parsed._replace(scheme=scheme))

    def rpc(method: str, params: list[Any]) -> Any:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            endpoint, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "bittensor-event-monitor/0.1"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("error"):
            raise RuntimeError(data["error"].get("message", "HTTP RPC error"))
        return data.get("result")

    if not block_hash:
        block_hash = rpc("chain_getBlockHash", [])
    header = rpc("chain_getHeader", [block_hash])
    height = int(header["number"], 16)

    def read_map(storage_name: str) -> dict[int, str]:
        prefix = "0x" + (
            _twox128(b"SubtensorModule") + _twox128(storage_name.encode())
        ).hex()
        keys = rpc("state_getKeysPaged", [prefix, 1000, None, block_hash]) or []
        values: dict[int, str] = {}
        for offset in range(0, len(keys), 100):
            batch = keys[offset:offset + 100]
            response = rpc("state_queryStorageAt", [batch, block_hash]) or []
            for key, value in (response[0].get("changes", []) if response else []):
                if value:
                    values[int.from_bytes(bytes.fromhex(key[-4:]), "little")] = value
        return values

    trading_flags = read_map("SubtokenEnabled")
    emission_flags = read_map("SubnetEmissionEnabled")
    registered = read_map("NetworkRegisteredAt")
    records = []
    for netuid, raw_registration in registered.items():
        value = trading_flags.get(netuid)
        raw = bytes.fromhex(value.removeprefix("0x")) if value else b"\x00"
        emission_value = emission_flags.get(netuid)
        emission_raw = (
            bytes.fromhex(emission_value.removeprefix("0x"))
            if emission_value else b"\x00"
        )
        registration_block = (
            int.from_bytes(bytes.fromhex(raw_registration.removeprefix("0x")), "little")
            if raw_registration else None
        )
        records.append({
            "netuid": netuid,
            "enabled": bool(raw and raw[0]),
            "emission_enabled": bool(emission_raw and emission_raw[0]),
            "registration_block": registration_block,
            "block_hash": block_hash,
        })
    return records, height


def _decode_identity_v3(value: str) -> dict[str, str] | None:
    names = (
        "subnet_name", "github_repo", "subnet_contact", "subnet_url",
        "discord", "description", "logo_url", "additional",
    )
    data = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    offset = 0
    decoded = {}
    for name in names:
        length, offset = _decode_compact(data, offset)
        end = offset + length
        if end > len(data):
            return None
        decoded[name] = data[offset:end].decode("utf-8", errors="replace")
        offset = end
    if offset != len(data):
        return None
    return decoded


def _decode_compact(data: bytes, offset: int) -> tuple[int, int]:
    first = data[offset]
    mode = first & 0b11
    if mode == 0:
        return first >> 2, offset + 1
    if mode == 1:
        return int.from_bytes(data[offset:offset + 2], "little") >> 2, offset + 2
    if mode == 2:
        return int.from_bytes(data[offset:offset + 4], "little") >> 2, offset + 4
    width = (first >> 2) + 4
    return int.from_bytes(data[offset + 1:offset + 1 + width], "little"), offset + 1 + width
