from __future__ import annotations

import csv
import html
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

FORMAT_ALIASES = {
    "md": "md",
    "markdown": "md",
    "csv": "csv",
    "json": "json",
    "html": "html",
    "htm": "html",
}
CONTENT_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


def normalize_format(value: str | None) -> str:
    normalized = FORMAT_ALIASES.get(str(value or "md").strip().casefold())
    if normalized is None:
        raise ValueError("导出格式必须是 md、csv、json 或 html")
    return normalized


def build_subnet_document(
    *,
    network: str,
    netuid: int,
    name: str,
    events: list[dict[str, Any]],
    fmt: str,
    exported_at: str | None = None,
) -> bytes:
    fmt = normalize_format(fmt)
    ordered = sorted(events, key=lambda item: (item.get("event_at") or "", item.get("first_seen_at") or ""))
    generated_at = exported_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if fmt == "md":
        return _markdown(network, netuid, name, ordered, generated_at).encode("utf-8")
    if fmt == "csv":
        return _csv(network, netuid, name, ordered, generated_at)
    if fmt == "json":
        return _json_document(network, netuid, name, ordered, generated_at).encode("utf-8")
    return _html_document(network, netuid, name, ordered, generated_at).encode("utf-8")


def build_subnet_archive(
    *,
    network: str,
    profiles: list[dict[str, Any]],
    events_by_netuid: dict[int, list[dict[str, Any]]],
    fmt: str,
    exported_at: str | None = None,
) -> bytes:
    fmt = normalize_format(fmt)
    generated_at = exported_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    profile_map = {int(profile["netuid"]): profile for profile in profiles}
    all_netuids = sorted(set(profile_map) | set(events_by_netuid))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for netuid in all_netuids:
            profile = profile_map.get(netuid, {})
            document = build_subnet_document(
                network=network,
                netuid=netuid,
                name=str(profile.get("name") or ""),
                events=events_by_netuid.get(netuid, []),
                fmt=fmt,
                exported_at=generated_at,
            )
            archive.writestr(f"SN{netuid}.{fmt}", document)
        archive.writestr(
            "README.txt",
            (
                "Subnet Pulse 子网事件更新记录\n"
                f"网络：{network}\n"
                f"导出时间：{generated_at}\n"
                f"格式：{fmt}\n"
                "排序：event_at 正序，同时间按 first_seen_at 正序\n"
            ).encode("utf-8"),
        )
    return output.getvalue()


def _event_time(value: Any) -> str:
    return str(value or "时间未知")


def _status(event: dict[str, Any]) -> str:
    if event.get("confidence") == "simulated" or (event.get("details") or {}).get("simulated"):
        return "模拟测试"
    if event.get("chain_status") == "retracted":
        return "已撤回"
    if event.get("chain_status") == "observed":
        return "链上观察"
    if event.get("enrichment_status") == "pending":
        return "补全中"
    if event.get("chain_status") == "finalized":
        return "已确认"
    return "已记录"


def _markdown(network: str, netuid: int, name: str, events: list[dict[str, Any]], generated_at: str) -> str:
    title = f"SN{netuid} {name}".strip()
    lines = [
        f"# {title} 事件更新记录",
        "",
        f"- 网络：{network}",
        f"- 导出时间：{generated_at}",
        f"- 事件数量：{len(events)}",
        "- 排序：事件发生时间正序",
        "",
    ]
    if not events:
        lines.append("暂无已采集事件。")
        return "\n".join(lines) + "\n"
    for index, event in enumerate(events, 1):
        lines.extend([
            f"## {index}. {_event_time(event.get('event_at'))} · {event.get('event_type') or '事件'}",
            "",
            f"**摘要：** {event.get('summary') or '—'}",
            "",
            f"- 来源：{event.get('source') or '—'}",
            f"- 状态：{_status(event)}",
            f"- 事件名称：{event.get('event_name') or '—'}",
            f"- 首次采集：{event.get('first_seen_at') or '—'}",
            f"- 证据链接：{event.get('source_url') or '—'}",
            f"- 事件 ID：{event.get('event_id')}",
            "",
        ])
    return "\n".join(lines)


def _csv(network: str, netuid: int, name: str, events: list[dict[str, Any]], generated_at: str) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "event_time", "first_seen_at", "event_type", "event_name", "source",
        "status", "summary", "event_id", "source_url", "block_number",
    ])
    for event in events:
        writer.writerow([
            _event_time(event.get("event_at")),
            event.get("first_seen_at") or "",
            event.get("event_type") or "",
            event.get("event_name") or "",
            event.get("source") or "",
            _status(event),
            event.get("summary") or "",
            event.get("event_id") or "",
            event.get("source_url") or "",
            event.get("block_number") or "",
        ])
    return output.getvalue().encode("utf-8-sig")


def _json_document(network: str, netuid: int, name: str, events: list[dict[str, Any]], generated_at: str) -> str:
    payload = {
        "network": network,
        "netuid": netuid,
        "name": name,
        "exported_at": generated_at,
        "order": "event_at ascending, then first_seen_at ascending",
        "event_count": len(events),
        "events": events,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"


def _html_document(network: str, netuid: int, name: str, events: list[dict[str, Any]], generated_at: str) -> str:
    title = html.escape(f"SN{netuid} {name}".strip())
    rows = []
    for index, event in enumerate(events, 1):
        source_url = str(event.get("source_url") or "")
        source = html.escape(source_url) if source_url else "—"
        source_cell = f'<a href="{source}" target="_blank" rel="noopener noreferrer">证据</a>' if source_url else source
        rows.append(
            "<tr>"
            f"<td>{index}</td><td>{html.escape(_event_time(event.get('event_at')))}</td>"
            f"<td>{html.escape(str(event.get('event_type') or '—'))}</td>"
            f"<td>{html.escape(str(event.get('summary') or '—'))}</td>"
            f"<td>{html.escape(_status(event))}</td>"
            f"<td>{html.escape(str(event.get('source') or '—'))}</td>"
            f"<td>{source_cell}</td>"
            "</tr>"
        )
    body = "".join(rows) if rows else '<tr><td colspan="7">暂无已采集事件</td></tr>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} 事件更新记录</title>
<style>
body{{margin:0;padding:32px;background:#f5f7f7;color:#18211f;font:14px/1.65 "Microsoft YaHei UI",sans-serif}}
main{{max-width:1180px;margin:auto;background:white;padding:30px;border-radius:14px;box-shadow:0 12px 36px #0001}}
h1{{margin-top:0}} .meta{{color:#5f6d69;margin-bottom:24px}} table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px;border-bottom:1px solid #dce4e1;text-align:left;vertical-align:top}} th{{background:#f0f5f3}}
td:nth-child(2){{white-space:nowrap}} a{{color:#087d5b}} .empty{{color:#667}}
</style></head><body><main>
<h1>{title} 事件更新记录</h1>
<div class="meta">网络：{html.escape(network)} · 导出时间：{html.escape(generated_at)} · 事件数量：{len(events)} · 按事件发生时间正序</div>
<table><thead><tr><th>#</th><th>事件时间</th><th>类型</th><th>摘要</th><th>状态</th><th>来源</th><th>证据</th></tr></thead>
<tbody>{body}</tbody></table></main></body></html>
"""
