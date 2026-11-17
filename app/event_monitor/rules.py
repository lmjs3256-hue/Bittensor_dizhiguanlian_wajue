from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_rules(path: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    source = catalog.get("source", {})
    rules: dict[tuple[str, str], dict[str, Any]] = {}
    for rule in catalog.get("rules", []):
        rule = {**rule, "evidence_source": source}
        key = (_norm(rule["module"]), _norm(rule["event_name"]))
        rules[key] = rule
    return rules, source


def load_simulation_signals(path: Path) -> list[dict[str, Any]]:
    """Load the user-facing simulation catalog from the same rules file as collectors."""
    catalog = json.loads(path.read_text(encoding="utf-8"))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in catalog.get("simulation_signals", []):
        if not isinstance(item, dict):
            continue
        signal_id = str(item.get("id") or "").strip()
        if not signal_id or signal_id in seen:
            continue
        required = ("label", "event_type", "event_name", "description", "summary")
        if any(not str(item.get(name) or "").strip() for name in required):
            continue
        seen.add(signal_id)
        result.append({
            "id": signal_id,
            "label": str(item["label"]).strip(),
            "event_type": str(item["event_type"]).strip(),
            "event_name": str(item["event_name"]).strip(),
            "description": str(item["description"]).strip(),
            "summary": str(item["summary"]).strip(),
            "completed_summary": str(item.get("completed_summary") or "").strip(),
            "supports_completion": bool(item.get("supports_completion", False)),
            "simulation_kind": str(item.get("simulation_kind") or "subnet").strip(),
            "netuid_required": bool(item.get("netuid_required", True)),
        })
    return result


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def get_rule(rules: dict[tuple[str, str], dict[str, Any]], module: Any, event_name: Any) -> dict[str, Any] | None:
    module_key, event_key = _norm(module), _norm(event_name)
    return rules.get((module_key, event_key)) or rules.get(("subtensormodule", event_key))


def event_fields(attributes: Any, rule: dict[str, Any]) -> dict[str, Any]:
    if isinstance(attributes, dict):
        result = {str(key): _plain(value) for key, value in attributes.items()}
        values = list(attributes.values())
        for name, index in rule.get("field_indices", {}).items():
            if isinstance(index, int) and 0 <= index < len(values):
                result[name] = _plain(values[index])
        return result
    values = attributes if isinstance(attributes, (list, tuple)) else [attributes]
    return {
        name: _plain(values[index])
        for name, index in rule.get("field_indices", {}).items()
        if isinstance(index, int) and 0 <= index < len(values)
    }


def _plain(value: Any) -> Any:
    if hasattr(value, "value"):
        return _plain(value.value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "serialize"):
        try:
            return _plain(value.serialize())
        except Exception:
            pass
    return str(value)
