"""Copy a legacy Subnet Pulse database into the merged event-monitor DB.

The merged tool keeps event history separate from the wallet address index.  This
utility uses SQLite's online backup API, then clears the old monitoring
confirmation list by default so the merged event monitor starts empty until the
user explicitly selects addresses from the shared known-address library.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import event_database_path


def migrate_event_database(
    source: Path,
    target: Path,
    *,
    clear_watchlist: bool = True,
    replace: bool = False,
) -> dict[str, int | str]:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"源事件库不存在：{source}")
    if source == target:
        raise ValueError("源事件库和目标事件库不能是同一个文件")
    if target.exists() and target.stat().st_size > 0 and not replace:
        raise FileExistsError(f"目标事件库已存在：{target}；确认覆盖时加 --replace")
    target.parent.mkdir(parents=True, exist_ok=True)

    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=15) as src, sqlite3.connect(target, timeout=15) as dst:
        src.backup(dst)
        tables = {
            row[0]
            for row in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        event_count = 0
        if "events" in tables:
            event_count = int(dst.execute("SELECT COUNT(*) FROM events").fetchone()[0] or 0)
        watched_before = 0
        if "watched_addresses" in tables:
            watched_before = int(
                dst.execute("SELECT COUNT(*) FROM watched_addresses").fetchone()[0] or 0
            )
            if clear_watchlist:
                dst.execute("DELETE FROM watched_addresses")
        dst.commit()
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        watched_after = 0
        if "watched_addresses" in tables:
            watched_after = int(
                dst.execute("SELECT COUNT(*) FROM watched_addresses").fetchone()[0] or 0
            )
    return {
        "source": str(source),
        "target": str(target),
        "events": event_count,
        "watchlist_before": watched_before,
        "watchlist_after": watched_after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移旧 Subnet Pulse 事件库")
    parser.add_argument("source", nargs="?", type=Path, default=Path("G:/Bittensor_event/data/events.db"))
    parser.add_argument("--target", type=Path, default=event_database_path())
    parser.add_argument("--replace", action="store_true", help="允许覆盖已有目标事件库")
    parser.add_argument(
        "--keep-watchlist",
        action="store_true",
        help="保留旧监控名单；默认清空，合并后需从共享已知地址库重新勾选确认",
    )
    args = parser.parse_args(argv)
    try:
        result = migrate_event_database(
            args.source,
            args.target,
            clear_watchlist=not args.keep_watchlist,
            replace=args.replace,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print a concise failure
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    print(
        f"迁移完成：{result['events']} 条事件，监控名单 "
        f"{result['watchlist_before']} → {result['watchlist_after']}；目标 {result['target']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
