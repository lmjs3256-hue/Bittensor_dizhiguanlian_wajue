"""One-time inheritance of subnet profiles and GitHub mappings from Subnet Pulse."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


PROFILE_TABLES = (
    "subnet_profiles",
    "subnet_profile_archive",
    "repositories",
    "subnet_trading_state",
)


def default_legacy_event_db(root_dir: Path) -> Path | None:
    candidates = (
        root_dir.parent / "Bittensor_event" / "data" / "events.db",
        root_dir / "data" / "legacy_events.db",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def inherit_legacy_event_state(target: Path, source: Path) -> dict[str, Any]:
    """Copy profile-related rows into an empty merged event database.

    Existing target rows are never replaced.  Each table is copied only when
    the corresponding target table is empty, so chain collection can continue
    to update profiles normally after the one-time inheritance.
    """

    target = target.resolve()
    source = source.resolve()
    if target == source:
        return {"inherited": False, "reason": "source equals target", "tables": {}}
    if not source.is_file() or not target.is_file():
        return {"inherited": False, "reason": "database missing", "tables": {}}

    result: dict[str, Any] = {
        "inherited": False,
        "source": str(source),
        "tables": {},
    }
    connection = sqlite3.connect(target, timeout=30)
    try:
        connection.execute("ATTACH DATABASE ? AS legacy", (str(source),))
        for table in PROFILE_TABLES:
            target_columns = [
                row[1] for row in connection.execute(f'PRAGMA main.table_info("{table}")').fetchall()
            ]
            source_columns = [
                row[1] for row in connection.execute(f'PRAGMA legacy.table_info("{table}")').fetchall()
            ]
            columns = [name for name in target_columns if name in set(source_columns)]
            if not columns:
                continue
            target_count = int(
                connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0] or 0
            )
            if table == "repositories" and target_count:
                result["tables"][table] = {"before": target_count, "after": target_count, "skipped": True}
                continue
            source_count = int(
                connection.execute(f'SELECT COUNT(*) FROM legacy."{table}"').fetchone()[0] or 0
            )
            quoted = ",".join(f'"{name}"' for name in columns)
            connection.execute(
                f'INSERT OR IGNORE INTO main."{table}" ({quoted}) '
                f'SELECT {quoted} FROM legacy."{table}"'
            )
            after = int(
                connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0] or 0
            )
            result["tables"][table] = {
                "source": source_count,
                "before": target_count,
                "after": after,
                "skipped": target_count > 0 and after == target_count,
            }
        connection.commit()
        result["inherited"] = any(
            not info.get("skipped") and info.get("after", 0) > info.get("before", 0)
            for info in result["tables"].values()
        )
    finally:
        try:
            connection.execute("DETACH DATABASE legacy")
        except sqlite3.Error:
            pass
        connection.close()
    return result
