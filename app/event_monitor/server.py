from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
from decimal import Decimal, InvalidOperation
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..validation import is_valid_ss58 as is_valid_ss58_address

from .address_monitor import (
    address_event_record,
    bracket_tag,
    manual_note_tags,
    mask_address as _mask_address,
    project_owner_tags,
)
from .github import parse_github_repository
from .storage import Store, now_iso
from .subnet_exports import build_subnet_archive, build_subnet_document, normalize_format

LOG = logging.getLogger(__name__)
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
PROFILE_FIELDS = (
    "name", "github_url", "website_url", "x_url", "discord_url",
    "description", "logo_url", "additional",
)


def _unique_tags(values: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = bracket_tag(value)
        key = re.sub(r"\s+", "", tag).casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def _annotate_address_event(
    event: dict[str, Any],
    watched_by_address: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """用当前地址资料刷新事件中的标签，但保留原始摘要作为证据快照。"""

    if event.get("source") != "address":
        return event
    details = dict(event.get("details") or {})
    matched = []
    all_manual_tags: list[str] = []
    all_display_tags: list[str] = []
    for original in details.get("matched_addresses") or []:
        if not isinstance(original, dict):
            continue
        item = dict(original)
        address = str(item.get("address") or "").strip()
        current = watched_by_address.get(address)
        if current:
            item["address_id"] = current.get("address_id")
            item["label"] = current.get("label") or item.get("label") or "未命名地址"
            item["library_book"] = current.get("library_book") or item.get("library_book") or ""
            item["manual_note"] = str(current.get("manual_note") or "")
            item["address_role"] = str(current.get("address_role") or "")
        item["official_owner"] = str(item.get("address_role") or "") == "project_owner"
        manual_tags = manual_note_tags(item.get("manual_note"))
        item["manual_note_tags"] = manual_tags
        library_book = str(item.get("library_book") or "").strip()
        label = str(item.get("label") or "").strip()
        library_tag = bracket_tag(library_book) if library_book else ""
        if not library_tag and label not in {"", "未命名地址"}:
            address = str(item.get("address") or "").strip()
            masked = _mask_address(address)
            if label not in {address, masked} and not (len(label) <= 4 and address.endswith(label)):
                library_tag = bracket_tag(label)
        item["display_tags"] = _unique_tags([library_tag, *manual_tags])
        matched.append(item)
        all_manual_tags.extend(manual_tags)
        all_display_tags.extend(item["display_tags"])
    details["matched_addresses"] = matched
    details["manual_note_tags"] = _unique_tags(all_manual_tags)
    details["display_tags"] = _unique_tags(all_display_tags)
    event["details"] = details
    return event


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any] | None]] = set()
        self._sequence = 0

    def subscribe(self) -> queue.Queue[dict[str, Any] | None]:
        subscriber: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any], change_type: str) -> None:
        with self._lock:
            self._sequence += 1
            payload = {
                "id": self._sequence,
                "change_type": change_type,
                "published_at": now_iso(),
                "event": event,
            }
            dead = []
            for subscriber in self._subscribers:
                try:
                    subscriber.put_nowait(payload)
                except queue.Full:
                    dead.append(subscriber)
            for subscriber in dead:
                self._subscribers.discard(subscriber)

    def close(self) -> None:
        with self._lock:
            for subscriber in self._subscribers:
                try:
                    subscriber.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers.clear()


class WorkerStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, dict[str, Any]] = {}

    def set(self, component: str, state: str, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._values[component] = {
                "state": state,
                "details": details or {},
                "updated_at": now_iso(),
            }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._values, ensure_ascii=False))


class AppContext:
    def __init__(self, *, store: Store, hub: EventHub, status: WorkerStatus,
                 static_dir: Path, product_info: dict[str, Any] | None = None,
                 simulation_enabled: bool = True, demo_enabled: bool = False,
                 simulation_signals: list[dict[str, Any]] | None = None,
                 runtime_config: dict[str, Any] | None = None,
                 env_path: Path | None = None):
        self.store = store
        self.hub = hub
        self.status = status
        self.static_dir = static_dir
        self.product_info = product_info or {}
        self.simulation_enabled = simulation_enabled
        self.simulation_signals = simulation_signals or []
        self.simulation_signal_map = {
            str(signal["id"]): signal for signal in self.simulation_signals
        }
        self.runtime_config = runtime_config or {}
        self.demo_enabled = demo_enabled
        self.env_path = env_path
        self.request_profile_sync: Any = None
        self.github_token_update: Any = None


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: AppContext):
        self.context = context
        super().__init__(address, MonitorHandler)


class MonitorHandler(SimpleHTTPRequestHandler):
    server: MonitorHTTPServer

    def __init__(self, *args: Any, **kwargs: Any):
        server = args[2]
        super().__init__(*args, directory=str(server.context.static_dir), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _text(self, data: str, content_type: str, status: int = 200,
              filename: str | None = None) -> None:
        payload = data.encode("utf-8-sig") if filename and filename.endswith(".csv") else data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def _bytes(self, payload: bytes, content_type: str, *, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求体为空或过大")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON 请求体必须是对象")
        return data

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/settings/github":
                self._json(self._github_settings())
                return
            if path == "/api/health":
                self._json({"status": "ok", "time": now_iso()})
                return
            if path == "/api/status":
                self._json({
                    "workers": self.server.context.status.snapshot(),
                    "alerts": self.server.context.store.list_alerts(),
                    "runtime": self.server.context.runtime_config,
                    "time": now_iso(),
                })
                return
            if path == "/api/product":
                self._json(self.server.context.product_info)
                return
            if path in {"/api/simulation/signals", "/api/simulation/catalog"}:
                self._json({
                    "enabled": self.server.context.simulation_enabled,
                    "signals": self.server.context.simulation_signals,
                })
                return
            if path == "/api/logs":
                self._json({"logs": self.server.context.store.list_logs(
                    limit=_integer(_first(query, "limit"), 200),
                    level=_first(query, "level"),
                    component=_first(query, "component"),
                )})
                return
            if path == "/api/trading":
                network = _first(query, "network") or "finney"
                self._json({
                    "network": network,
                    "subnets": self.server.context.store.list_trading_states(network),
                })
                return
            if path == "/api/settings/address-filter":
                self._json({
                    "min_transfer_tao": self.server.context.store.get_global_transfer_threshold(),
                })
                return
            if path == "/api/addresses":
                network = _first(query, "network") or "finney"
                addresses = self.server.context.store.list_watched_addresses(network)
                self._json({
                    "network": network,
                    "addresses": [
                        {**item, "masked": _mask_address(item["address"])}
                        for item in addresses
                    ],
                })
                return
            if path == "/api/subnet-profiles":
                network = _first(query, "network") or "finney"
                include_archived = _first(query, "include_archived") == "1"
                self._json({
                    "network": network,
                    "profiles": self.server.context.store.list_subnet_profiles(
                        network,
                        include_inactive=_first(query, "include_inactive") == "1",
                    ),
                    "archived_profiles": (
                        self.server.context.store.list_archived_subnet_profiles(network)
                        if include_archived else []
                    ),
                })
                return
            if path == "/api/subnet-profiles/export":
                network = _first(query, "network") or "finney"
                fmt = (_first(query, "format") or "json").casefold()
                self._export_profiles(network, fmt)
                return
            if path == "/api/subnet-priorities":
                network = _first(query, "network") or "finney"
                self._json({
                    "network": network,
                    "priorities": self.server.context.store.list_prioritized_profiles(network),
                })
                return
            match = re.fullmatch(r"/api/subnet-profiles/(\d+)", path)
            if match:
                network = _first(query, "network") or "finney"
                profile = self.server.context.store.get_subnet_profile(network, int(match.group(1)))
                if profile is None:
                    self._error(404, "子网档案不存在")
                    return
                self._json({"profile": profile})
                return
            if path == "/api/events":
                filters = {name: _first(query, name) for name in (
                    "network", "netuid", "event_type", "source", "chain_status",
                    "enrichment_status", "from", "to", "q",
                )}
                limit = _integer(_first(query, "limit"), 100)
                offset = _integer(_first(query, "offset"), 0)
                events = self.server.context.store.list_events(
                    filters,
                    ascending=_first(query, "order") == "asc",
                    limit=limit,
                    offset=offset,
                )
                network = filters.get("network") or "finney"
                watched = self.server.context.store.list_watched_addresses(network)
                watched_by_address = {
                    str(item.get("address") or ""): item for item in watched
                }
                events = [
                    _annotate_address_event(event, watched_by_address)
                    for event in events
                ]
                self._json({
                    "events": events,
                    "total": self.server.context.store.count_events(filters),
                    "event_types": self.server.context.store.list_event_types(network),
                })
                return
            if path == "/api/subnet-event-records":
                network = _first(query, "network") or "finney"
                self._json(self._subnet_record_index(network))
                return
            if path == "/api/subnet-event-records/export":
                network = _first(query, "network") or "finney"
                fmt = normalize_format(_first(query, "format"))
                profiles = self.server.context.store.list_subnet_profiles(network)
                counts = self.server.context.store.subnet_event_counts(network)
                events_by_netuid = {
                    netuid: self.server.context.store.list_subnet_events(network, netuid)
                    for netuid in counts
                }
                payload = build_subnet_archive(
                    network=network,
                    profiles=profiles,
                    events_by_netuid=events_by_netuid,
                    fmt=fmt,
                )
                content_type = {
                    "md": "application/zip",
                    "csv": "application/zip",
                    "json": "application/zip",
                    "html": "application/zip",
                }[fmt]
                date = now_iso()[:10].replace("-", "")
                self._bytes(
                    payload, content_type,
                    filename=f"subnet-event-records-{fmt}-{date}.zip",
                )
                return
            subnet_export = re.fullmatch(r"/api/subnets/(\d+)/export", path)
            if subnet_export:
                network = _first(query, "network") or "finney"
                netuid = int(subnet_export.group(1))
                fmt = normalize_format(_first(query, "format"))
                profile = self.server.context.store.get_subnet_profile(network, netuid) or {}
                events = self.server.context.store.list_subnet_events(network, netuid)
                payload = build_subnet_document(
                    network=network, netuid=netuid,
                    name=str(profile.get("name") or ""),
                    events=events, fmt=fmt,
                )
                content_type = {
                    "md": "text/markdown; charset=utf-8",
                    "csv": "text/csv; charset=utf-8",
                    "json": "application/json; charset=utf-8",
                    "html": "text/html; charset=utf-8",
                }[fmt]
                self._bytes(
                    payload, content_type,
                    filename=f"SN{netuid}.{fmt}",
                )
                return
            match = re.fullmatch(r"/api/events/([0-9a-f]{64})", path)
            if match:
                event = self.server.context.store.get_event(match.group(1))
                if not event:
                    self._error(404, "事件不存在")
                    return
                watched = self.server.context.store.list_watched_addresses(
                    str(event.get("network") or "finney")
                )
                event = _annotate_address_event(
                    event,
                    {str(item.get("address") or ""): item for item in watched},
                )
                self._json({
                    "event": event,
                    "history": self.server.context.store.event_history(event["event_id"]),
                    "raw_events": self.server.context.store.raw_events(event["event_id"]),
                })
                return
            match = re.fullmatch(r"/api/subnets/(\d+)", path)
            if match:
                self._json({
                    "netuid": int(match.group(1)),
                    "events": self.server.context.store.list_events({
                        "network": _first(query, "network") or "finney",
                        "netuid": int(match.group(1)),
                    }, ascending=True, limit=500),
                })
                return
            if path == "/api/repositories":
                self._json({"repositories": self.server.context.store.list_repositories()})
                return
            if path == "/api/runtime-metadata":
                network = _first(query, "network") or "finney"
                self._json({"network": network, "versions": self.server.context.store.list_runtime_metadata(network)})
                return
            match = re.fullmatch(r"/api/runtime-metadata/(\d+)", path)
            if match:
                network = _first(query, "network") or "finney"
                metadata = self.server.context.store.get_runtime_metadata(network, int(match.group(1)))
                if metadata is None:
                    self._error(404, "运行时元数据不存在")
                    return
                self._json({"network": network, "metadata": metadata})
                return
            if path == "/api/alerts":
                self._json({"alerts": self.server.context.store.list_alerts()})
                return
            if path == "/api/stream":
                self._stream()
                return
            if path.startswith("/api/"):
                self._error(404, "接口不存在")
                return
            if (
                path == "/"
                or path in {
                    "/logs", "/changelog", "/guide", "/features",
                    "/profiles", "/priorities", "/addresses", "/records", "/settings", "/simulation",
                }
                or path == "/floating"
                or path.startswith(("/events/", "/subnets/"))
            ):
                self.path = "/floating.html" if path == "/floating" else "/index.html"
            super().do_GET()
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            LOG.exception("GET request failed")
            self._error(500, str(exc)[:400])

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/repositories":
                self._add_repository(body)
                return
            if path == "/api/addresses":
                raw_addresses = body.get("addresses")
                address_values = (
                    raw_addresses if isinstance(raw_addresses, list)
                    else re.split(r"[\s,，;；]+", str(body.get("address") or ""))
                )
                addresses = list(dict.fromkeys(
                    str(value).strip() for value in address_values if str(value).strip()
                ))
                label = str(body.get("label") or "").strip()[:120]
                library_book = str(body.get("library_book") or body.get("book") or "").strip()[:80]
                manual_note = str(body.get("manual_note") or "").strip()[:240]
                address_role = str(body.get("address_role") or "").strip()[:40]
                if address_role not in {"", "project_owner"}:
                    address_role = ""
                network = str(body.get("network") or "finney")
                if not addresses:
                    raise ValueError("请至少输入一个 SS58 地址")
                invalid = [value for value in addresses if not is_valid_ss58_address(value)]
                if invalid:
                    raise ValueError(f"无效的 SS58 地址：{_mask_address(invalid[0])}")
                min_transfer_tao = _tao_threshold(body.get("min_transfer_tao"))
                created = []
                skipped = []
                for address in addresses:
                    address_label = label or _mask_address(address)
                    try:
                        watched = self.server.context.store.add_watched_address(
                            network, address, address_label, min_transfer_tao,
                            library_book=library_book,
                            manual_note=manual_note,
                            address_role=address_role,
                            clear_exclusion=True,
                        )
                    except ValueError:
                        skipped.append(address)
                        continue
                    created.append({**watched, "masked": _mask_address(address)})
                self._json({
                    "address": created[0] if len(created) == 1 else None,
                    "addresses": created,
                    "created": len(created),
                    "skipped": len(skipped),
                    "skipped_addresses": [_mask_address(value) for value in skipped],
                }, 201 if created else 200)
                return
            if path == "/api/subnet-profiles/import":
                self._import_profiles(body)
                return
            if path == "/api/subnet-profiles/sync":
                if self.server.context.request_profile_sync is None:
                    self._error(503, "链上档案同步未启用")
                    return
                started = bool(self.server.context.request_profile_sync(None))
                self._json({
                    "status": "accepted",
                    "started": started,
                    "message": "已开始校对" if started else "已有校对任务在运行",
                }, 202)
                return
            if path == "/api/subnet-profiles/selection":
                network = str(body.get("network") or "finney")
                netuids = _parse_netuids(body.get("netuids"))
                if not netuids:
                    raise ValueError("请至少选择一个子网")
                count = self.server.context.store.set_subnet_monitoring(
                    network, netuids, bool(body.get("monitored", True)),
                )
                self._json({"status": "ok", "updated": count})
                return
            if path == "/api/subnet-priorities":
                network = str(body.get("network") or "finney")
                netuids = _parse_netuids(body.get("netuids"))
                if not netuids:
                    raise ValueError("请至少输入一个子网编号")
                level = int(body.get("level", 1))
                if level not in {0, 1, 2}:
                    raise ValueError("priority level 必须为 0、1 或 2")
                count = self.server.context.store.set_subnet_priority(network, netuids, level)
                self._json({"status": "ok", "updated": count, "level": level})
                return
            if path == "/api/simulation/events":
                self._simulate_event(body)
                return
            if path == "/api/demo/events" and self.server.context.demo_enabled:
                self._demo_event(body)
                return
            self._error(404, "接口不存在")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            LOG.exception("POST request failed")
            self._error(500, str(exc)[:400])

    def do_PUT(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/settings/github":
                body = self._body()
                token = str(body.get("token") or "").strip()
                if "\n" in token or "\r" in token or len(token) > 500:
                    raise ValueError("GitHub Token 格式无效")
                self._save_github_token(token)
                if self.server.context.github_token_update is not None:
                    self.server.context.github_token_update(token or None)
                configured = bool(token)
                self.server.context.runtime_config["github_token_configured"] = configured
                self.server.context.runtime_config["github_batch_size"] = 30 if configured else 1
                os.environ["GITHUB_TOKEN"] = token
                self._json(self._github_settings())
                return
            if path == "/api/settings/address-filter":
                body = self._body()
                threshold = _tao_threshold(body.get("min_transfer_tao"))
                saved = self.server.context.store.set_global_transfer_threshold(threshold)
                self._json({"min_transfer_tao": saved})
                return
            address_match = re.fullmatch(r"/api/addresses/(\d+)", path)
            if address_match:
                body = self._body()
                label = (
                    str(body.get("label") or "").strip()[:120]
                    if "label" in body else None
                )
                enabled = bool(body["enabled"]) if "enabled" in body else None
                min_transfer_tao = (
                    _tao_threshold(body.get("min_transfer_tao"))
                    if "min_transfer_tao" in body else None
                )
                library_book = (
                    str(body.get("library_book") or body.get("book") or "").strip()[:80]
                    if "library_book" in body or "book" in body else None
                )
                manual_note = (
                    str(body.get("manual_note") or "").strip()[:240]
                    if "manual_note" in body else None
                )
                address_role = None
                if "address_role" in body:
                    address_role = str(body.get("address_role") or "").strip()[:40]
                    if address_role not in {"", "project_owner"}:
                        address_role = ""
                watched = self.server.context.store.update_watched_address(
                    int(address_match.group(1)),
                    label=label,
                    enabled=enabled,
                    min_transfer_tao=min_transfer_tao,
                    library_book=library_book,
                    manual_note=manual_note,
                    address_role=address_role,
                )
                if watched is None:
                    self._error(404, "监控地址不存在")
                    return
                self._json({
                    "address": {
                        **watched,
                        "masked": _mask_address(watched["address"]),
                    }
                })
                return
            match = re.fullmatch(r"/api/subnet-profiles/(\d+)", path)
            if not match:
                self._error(404, "接口不存在")
                return
            body = self._body()
            network = str(body.get("network") or "finney")
            edited_by = str(body.get("edited_by") or "").strip()
            if not edited_by:
                raise ValueError("保存档案时必须填写编辑人")
            values = self._profile_values(body)
            profile = self.server.context.store.save_subnet_profile_override(
                network, int(match.group(1)), values, edited_by,
            )
            self._sync_profile_repository(
                network, int(match.group(1)), values.get("github_url"), edited_by,
            )
            self._json({"profile": profile})
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            LOG.exception("PUT request failed")
            self._error(500, str(exc)[:400])

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/addresses/book":
                network = _first(query, "network") or "finney"
                library_book = _first(query, "library_book") or _first(query, "book")
                result = self.server.context.store.delete_watched_address_book(
                    network, library_book or "",
                )
                if not result["deleted"]:
                    self._error(404, "该地址库不在事件监控名单中")
                    return
                self._json({"status": "deleted", **result})
                return
            if path == "/api/simulation/events":
                deleted = self.server.context.store.delete_simulated_events()
                if deleted:
                    self.server.context.hub.publish({
                        "event_id": "__simulation_events_deleted__",
                        "network": "finney",
                        "netuid": None,
                        "event_type": "模拟事件删除",
                        "event_name": "SimulationDeleted",
                        "source": "simulation",
                        "event_at": now_iso(),
                        "summary": f"已永久删除 {deleted} 条模拟事件",
                    }, "deleted")
                self._json({"status": "deleted", "deleted": deleted})
                return
            event_match = re.fullmatch(r"/api/events/([0-9a-f]{64})", path)
            if event_match:
                event_id = event_match.group(1)
                event = self.server.context.store.get_event(event_id)
                if event is None:
                    self._error(404, "事件不存在")
                    return
                if event["source"] != "simulation" and event.get("confidence") != "simulated":
                    self._error(400, "只允许删除模拟测试事件")
                    return
                deleted = self.server.context.store.delete_simulated_events(
                    event_id=event_id,
                )
                if deleted:
                    self.server.context.hub.publish({
                        "event_id": "__simulation_event_deleted__",
                        "network": event["network"],
                        "netuid": event["netuid"],
                        "event_type": "模拟事件删除",
                        "event_name": "SimulationDeleted",
                        "source": "simulation",
                        "event_at": now_iso(),
                        "summary": "已永久删除一条模拟事件",
                    }, "deleted")
                self._json({"status": "deleted", "deleted": deleted})
                return
            match = re.fullmatch(r"/api/addresses/(\d+)", path)
            if not match:
                self._error(404, "接口不存在")
                return
            if not self.server.context.store.delete_watched_address(int(match.group(1))):
                self._error(404, "监控地址不存在")
                return
            self._json({"status": "deleted"})
        except Exception as exc:
            LOG.exception("DELETE request failed")
            self._error(500, str(exc)[:400])

    def _subnet_record_index(self, network: str) -> dict[str, Any]:
        profiles = self.server.context.store.list_subnet_profiles(network)
        counts = self.server.context.store.subnet_event_counts(network)
        profile_map = {int(profile["netuid"]): profile for profile in profiles}
        netuids = sorted(set(profile_map) | set(counts))
        records = []
        for netuid in netuids:
            profile = profile_map.get(netuid, {})
            count = counts.get(netuid, {})
            records.append({
                "netuid": netuid,
                "name": profile.get("name") or "",
                "event_count": int(count.get("event_count") or 0),
                "first_event_at": count.get("first_event_at"),
                "last_event_at": count.get("last_event_at"),
                "view_url": f"/subnets/{netuid}",
                "exports": {
                    fmt: f"/api/subnets/{netuid}/export?format={fmt}"
                    for fmt in ("md", "csv", "json", "html")
                },
            })
        return {
            "network": network,
            "subnet_count": len(records),
            "archive_exports": {
                fmt: f"/api/subnet-event-records/export?format={fmt}"
                for fmt in ("md", "csv", "json", "html")
            },
            "records": records,
        }

    def _github_settings(self) -> dict[str, Any]:
        configured = bool(os.getenv("GITHUB_TOKEN", "").strip())
        return {
            "configured": configured,
            "mode": "authenticated" if configured else "anonymous",
            "github_batch_size": 30 if configured else 1,
            "token_url": "https://github.com/settings/personal-access-tokens/new",
            "message": (
                "GitHub Token 已配置，每轮最多处理 30 个仓库。"
                if configured else "当前使用匿名 GitHub API，每轮最多处理 1 个仓库。"
            ),
        }

    def _save_github_token(self, token: str) -> None:
        path = self.server.context.env_path
        if path is None:
            raise ValueError("当前运行模式不支持保存 GitHub Token")
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        output = []
        replaced = False
        for line in lines:
            if line.strip().startswith("GITHUB_TOKEN="):
                output.append(f"GITHUB_TOKEN={token}")
                replaced = True
            else:
                output.append(line)
        if not replaced:
            output.append(f"GITHUB_TOKEN={token}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _stream(self) -> None:
        subscriber = self.server.context.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n")
            self.wfile.write(f"event: ready\ndata: {json.dumps({'time': now_iso()})}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    payload = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                if payload is None:
                    break
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
                self.wfile.write(f"id: {payload['id']}\nevent: event\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.context.hub.unsubscribe(subscriber)

    def _profile_values(self, body: dict[str, Any]) -> dict[str, str]:
        values: dict[str, str] = {}
        for field in PROFILE_FIELDS:
            if field not in body:
                continue
            value = str(body.get(field) or "").strip()
            limit = 4000 if field in {"description", "additional"} else 1000
            if len(value) > limit:
                raise ValueError(f"{field} 超出长度限制")
            if field in {"github_url", "website_url", "x_url", "discord_url", "logo_url"} and value:
                value = _normalise_profile_url(field, value)
            values[field] = value
        return values

    def _export_profiles(self, network: str, fmt: str) -> None:
        if fmt not in {"json", "csv"}:
            raise ValueError("format 只支持 json 或 csv")
        profiles = self.server.context.store.list_subnet_profiles(network)
        records = []
        for profile in profiles:
            record = {"network": network, "netuid": profile["netuid"]}
            record.update({field: profile.get(field, "") for field in PROFILE_FIELDS})
            records.append(record)
        if fmt == "json":
            self._text(
                json.dumps(
                    {"network": network, "exported_at": now_iso(), "profiles": records},
                    ensure_ascii=False, indent=2,
                ),
                "application/json; charset=utf-8",
                filename="bittensor-subnet-profiles.json",
            )
            return
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=["network", "netuid", *PROFILE_FIELDS])
        writer.writeheader()
        writer.writerows(records)
        self._text(
            output.getvalue(), "text/csv; charset=utf-8",
            filename="bittensor-subnet-profiles.csv",
        )

    def _import_profiles(self, body: dict[str, Any]) -> None:
        records = body.get("profiles") or body.get("records") or body.get("subnets")
        if not isinstance(records, list) or not records:
            raise ValueError("导入数据必须包含非空 profiles 数组")
        if len(records) > 500:
            raise ValueError("单次最多导入 500 条档案")
        edited_by = str(body.get("edited_by") or "import").strip()[:80]
        imported = 0
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("每条档案必须是对象")
            try:
                netuid = int(record.get("netuid"))
            except (TypeError, ValueError) as exc:
                raise ValueError("档案中的 netuid 必须是整数") from exc
            if not 0 <= netuid <= 65535:
                raise ValueError("档案中的 netuid 超出 u16 范围")
            values = {
                key: value for key, value in self._profile_values(record).items()
                if value != ""
            }
            if values:
                network = str(record.get("network") or "finney")
                self.server.context.store.save_subnet_profile_override(
                    network,
                    netuid, values, edited_by,
                )
                self._sync_profile_repository(
                    network, netuid, values.get("github_url"), edited_by,
                )
                imported += 1
        self._json({"status": "ok", "imported": imported})

    def _simulate_event(self, body: dict[str, Any]) -> None:
        if not self.server.context.simulation_enabled:
            self._error(403, "模拟信号功能已关闭")
            return
        signal_id = str(body.get("signal_id") or "").strip()
        signal = self.server.context.simulation_signal_map.get(signal_id)
        if signal is None:
            raise ValueError("请选择已登记且正在监控的信号类型")
        if signal.get("simulation_kind") == "address":
            self._simulate_address_event(body, signal)
            return
        try:
            netuid = int(body.get("netuid"))
        except (TypeError, ValueError) as exc:
            raise ValueError("simulation netuid 必须是整数") from exc
        if not 0 <= netuid <= 65535:
            raise ValueError("simulation netuid 超出 u16 范围")
        summary = _signal_summary(signal["summary"], netuid)
        delayed_summary = (
            _signal_summary(signal.get("completed_summary", ""), netuid)
            if signal.get("supports_completion") else ""
        )
        try:
            delay_seconds = min(60, max(0, int(body.get("delay_seconds", 0))))
        except (TypeError, ValueError):
            delay_seconds = 0
        if not delayed_summary:
            delay_seconds = 0
        event_id = hashlib.sha256(
            f"simulation:{time.time_ns()}:{netuid}".encode()
        ).hexdigest()
        event = {
            "event_id": event_id,
            "network": str(body.get("network") or "finney"),
            "netuid": netuid,
            "event_type": signal["event_type"],
            "event_name": signal["event_name"],
            "source": "simulation",
            "source_url": None,
            "event_at": now_iso(),
            "summary": summary,
            "details": {
                "simulated": True,
                "simulation_signal_id": signal_id,
                "signal_meaning": signal["description"],
                "payload": body.get("details") or {},
            },
            "raw_data": {"simulated": True, "input": body},
            "chain_status": "simulated",
            "enrichment_status": "pending" if delayed_summary else "complete",
            "confidence": "simulated",
        }
        stored, _ = self.server.context.store.create_event(event)
        self.server.context.hub.publish(stored, "simulated")
        if delayed_summary:
            store = self.server.context.store
            hub = self.server.context.hub

            def enrich() -> None:
                updated = store.update_event(
                    event_id, "simulated_enriched",
                    {
                        "summary": delayed_summary,
                        "enrichment_status": "complete",
                        "last_enriched_at": now_iso(),
                    },
                )
                if updated:
                    hub.publish(updated, "simulated_enriched")

            threading.Timer(delay_seconds, enrich).start()
        LOG.info("Simulation signal %s created for SN%s", signal_id, netuid)
        self._json({
            "event": stored,
            "signal": signal,
            "delayed_update_seconds": delay_seconds,
        }, 202)

    def _simulate_address_event(self, body: dict[str, Any], signal: dict[str, Any]) -> None:
        network = str(body.get("network") or "finney")
        watched = self.server.context.store.watched_addresses_with_global_threshold(
            network, enabled_only=True
        )
        try:
            address_id = int(body.get("address_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("请选择地址库中的已启用地址") from exc
        selected = next((item for item in watched if int(item["address_id"]) == address_id), None)
        if selected is None:
            raise ValueError("所选地址不存在或尚未启用")
        watched_address = str(selected["address"])
        module, _, event_name = str(signal["event_name"]).partition(".")
        if not module or not event_name:
            raise ValueError("地址模拟信号的事件名称无效")
        counterparty = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
        netuid: int | None = None
        if event_name == "Transfer":
            threshold_tao = Decimal(str(selected.get("min_transfer_tao") or 0))
            simulated_rao = max(
                1_000_000_000,
                int(threshold_tao * Decimal(1_000_000_000)),
            )
            attributes = [watched_address, counterparty, simulated_rao]
        elif event_name in {"StakeAdded", "StakeRemoved"}:
            try:
                netuid = int(body.get("netuid"))
            except (TypeError, ValueError) as exc:
                raise ValueError("地址质押模拟必须提供子网编号") from exc
            if not 0 <= netuid <= 65535:
                raise ValueError("子网编号超出 u16 范围")
            attributes = [
                watched_address, counterparty, 1_000_000_000, 2_000_000_000, netuid, 0,
            ]
        else:
            raise ValueError("未登记的地址模拟事件")
        stamp = now_iso()
        synthetic_id = hashlib.sha256(
            f"address-simulation:{time.time_ns()}:{address_id}:{event_name}".encode()
        ).hexdigest()
        block_hash = "0x" + hashlib.sha256(synthetic_id.encode()).hexdigest()
        raw = {
            "raw_id": f"{network}:{block_hash}:simulation",
            "event_index": 0,
            "module": module,
            "event_name": event_name,
            "raw": {
                "height": None,
                "block_hash": block_hash,
                "module": module,
                "event_name": event_name,
                "attributes": attributes,
                "value": {"attributes": attributes},
                "simulated": True,
            },
        }
        event = address_event_record(
            raw,
            network=network,
            watched_addresses=[selected],
            block_hash=block_hash,
            height=0,
            spec_version=None,
            event_at=stamp,
        )
        if event is None:
            raise ValueError("所选地址无法匹配该模拟事件")
        event.update({
            "event_id": synthetic_id,
            "source_url": None,
            "block_number": None,
            "block_hash": None,
            "event_index": None,
            "chain_status": "simulated",
            "enrichment_status": "complete",
            "confidence": "simulated",
            "details": {
                **(event.get("details") or {}),
                "simulated": True,
                "simulation_signal_id": signal["id"],
                "signal_meaning": signal["description"],
                "simulated_attributes": attributes,
            },
            "raw_data": {
                "simulated": True,
                "source": "address_monitor",
                "event": event_name,
                "attributes": attributes,
            },
        })
        stored, _ = self.server.context.store.create_event(event)
        self.server.context.hub.publish(stored, "simulated")
        LOG.info("Address simulation %s created for address_id=%s", signal["id"], address_id)
        self._json({
            "event": stored,
            "signal": signal,
            "delayed_update_seconds": 0,
        }, 202)

    def _sync_profile_repository(self, network: str, netuid: int,
                                 github_url: str | None, reviewed_by: str) -> None:
        parsed = parse_github_repository(github_url)
        if parsed is None:
            return
        owner, name = parsed
        self.server.context.store.upsert_reviewed_repository({
            "network": network,
            "netuid": netuid,
            "owner": owner,
            "name": name,
            "html_url": f"https://github.com/{owner}/{name}",
            "reviewed_by": reviewed_by,
        })

    def _add_repository(self, body: dict[str, Any]) -> None:
        owner = str(body.get("owner", "")).strip()
        name = str(body.get("name", "")).strip()
        if not REPO_PART.fullmatch(owner) or not REPO_PART.fullmatch(name):
            raise ValueError("owner 或 name 格式无效")
        try:
            netuid = int(body.get("netuid"))
        except (TypeError, ValueError) as exc:
            raise ValueError("netuid 必须是整数") from exc
        if not 0 <= netuid <= 65535:
            raise ValueError("netuid 超出 u16 范围")
        reviewed_by = str(body.get("reviewed_by", "")).strip()
        if not reviewed_by:
            raise ValueError("人工关联必须填写审核人")
        repo = self.server.context.store.add_repository({
            "network": str(body.get("network") or "finney"),
            "netuid": netuid,
            "owner": owner,
            "name": name,
            "html_url": f"https://github.com/{owner}/{name}",
            "relation_source": "manual_review",
            "reviewed_by": reviewed_by,
        })
        self._json({"repository": repo}, 201)

    def _demo_event(self, body: dict[str, Any]) -> None:
        event_id = str(body.get("event_id") or hashlib.sha256(
            f"demo:{now_iso()}".encode()
        ).hexdigest())
        event = {
            "event_id": event_id,
            "network": "finney",
            "netuid": int(body.get("netuid", 55)),
            "event_type": str(body.get("event_type", "身份信息变更")),
            "event_name": str(body.get("event_name", "Demo.Event")),
            "source": str(body.get("source", "chain")),
            "source_url": body.get("source_url"),
            "event_at": str(body.get("event_at") or now_iso()),
            "summary": str(body.get("summary", "演示事件：身份信息正在核验")),
            "details": body.get("details") if isinstance(body.get("details"), dict) else {"demo": True},
            "raw_data": body.get("raw_data") if isinstance(body.get("raw_data"), dict) else body,
            "chain_status": body.get("chain_status", "observed"),
            "enrichment_status": str(body.get("enrichment_status", "pending")),
            "confidence": str(body.get("confidence", "high")),
        }
        stored, created = self.server.context.store.create_event(event)
        if created:
            self.server.context.hub.publish(stored, "discovered")
        elif any(key in body for key in ("summary", "enrichment_status", "chain_status")):
            stored = self.server.context.store.update_event(
                event_id, "enriched",
                {
                    "summary": event["summary"],
                    "enrichment_status": event["enrichment_status"],
                    "chain_status": event["chain_status"],
                    "last_enriched_at": now_iso(),
                },
            )
            if stored:
                self.server.context.hub.publish(stored, "enriched")
        self._json({"event": stored, "created": created}, 201 if created else 200)


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _signal_summary(template: str, netuid: int) -> str:
    return str(template or "").replace("{netuid}", str(netuid)).strip()[:300]


def _parse_netuids(value: Any) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = re.split(r"[\s,，]+", str(value or ""))
    result = []
    for part in parts:
        text = str(part).strip().upper().removeprefix("SN")
        if not text:
            continue
        number = int(text)
        if not 0 <= number <= 65535:
            raise ValueError("子网编号超出 u16 范围")
        if number not in result:
            result.append(number)
    return result


def _normalise_profile_url(field: str, value: str) -> str:
    if field == "x_url" and value.startswith("@"):
        value = "https://x.com/" + value[1:]
    if field == "github_url" and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        value = "https://github.com/" + value
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} 必须是 HTTP(S) 链接")
    if field == "x_url" and parsed.netloc.casefold() in {"twitter.com", "www.twitter.com", "www.x.com"}:
        value = urllib.parse.urlunparse(parsed._replace(netloc="x.com"))
    return value


def _integer(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _tao_threshold(value: Any) -> float:
    if value is None or str(value).strip() == "":
        return 0.0
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("小额转账过滤阈值必须是数字") from exc
    if not number.is_finite():
        raise ValueError("小额转账过滤阈值必须是有限数字")
    if number < 0:
        raise ValueError("小额转账过滤阈值不能小于 0")
    if number > Decimal("1000000000"):
        raise ValueError("小额转账过滤阈值过大")
    return float(number)


def serve(*, host: str, port: int, context: AppContext) -> None:
    server = MonitorHTTPServer((host, port), context)
    LOG.info("Panel available at http://%s:%s", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        context.hub.close()
        server.server_close()
