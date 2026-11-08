# -*- coding: utf-8 -*-
"""修正历史数据：确认交易所/公共地址后，把残留的旧库名标签归位（0.8.17）。

0.8.17 之前的版本只清 ``book``，不清由库名复制而来的 ``label``。于是在地址库
清单里，这些地址仍然以旧库名分组显示；扫描结果的库名徽标也还挂着旧库名——
用户看到的现象就是"确认了交易所，但 SN46 那个标识还在"。

本脚本只做一件很窄的事，且**先预览、确认后才写库**：

    找出「已确认交易所 / 已标公共地址」+「已不在任何库里（book=''）」+
    「label 恰好等于某个现存地址库名」的地址，把这些 label 换成
    「人工确认交易所」/「公共地址」。

判据必须同时满足「label 是现存库名」，否则会误伤官方交易所名单（Kraken /
Binance 等的 label 就是交易所名，绝不能改）。

用法：

    .venv\\Scripts\\python.exe tools/fix_exchange_label_residue.py            # 只看不改
    .venv\\Scripts\\python.exe tools/fix_exchange_label_residue.py --apply    # 真的写库
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import database_path  # noqa: E402
from app.db import AddressBook, AddressLibrary, Database  # noqa: E402

CONFIRMED_LABEL = "人工确认交易所"
PUBLIC_LABEL = "公共地址"
KEEP_LABELS = {"", CONFIRMED_LABEL, "人工否定交易所", PUBLIC_LABEL}


def _is_public(row: AddressLibrary) -> bool:
    raw = (row.categories or "").upper()
    return "PUBLIC" in raw or (row.category or "").upper() == "PUBLIC"


def find_residue(database: Database) -> list[dict]:
    """找出需要归位的记录。判据保守到"宁可漏、不可错"。"""

    with database.session() as db:
        book_names = {row.name for row in db.scalars(select(AddressBook)).all()}
        rows = db.scalars(
            select(AddressLibrary).where(
                (AddressLibrary.book == "") | (AddressLibrary.book.is_(None))
            )
        ).all()
        found = []
        for row in rows:
            label = (row.label or "").strip()
            if label in KEEP_LABELS:
                continue
            # 只处理"已得出结论"的地址：确认交易所 或 已标公共地址。
            if not row.exchange_confirmed and not _is_public(row):
                continue
            # 关键判据：label 恰好是某个现存地址库的名字，才认定是库名残留。
            if label not in book_names:
                continue
            found.append(
                {
                    "id": row.id,
                    "address": row.address,
                    "label": label,
                    "book": row.book or "",
                    "category": row.category or "",
                    "source": row.source or "",
                    "target": PUBLIC_LABEL if _is_public(row) else CONFIRMED_LABEL,
                }
            )
        return found


def apply_fix(database: Database, items: list[dict]) -> int:
    if not items:
        return 0
    changed = 0
    with database.session() as db:
        for item in items:
            row = db.get(AddressLibrary, item["id"])
            if row is None:
                continue
            # 再校验一次，避免预览与实际之间库名被改过。
            if (row.label or "").strip() != item["label"]:
                continue
            row.label = item["target"]
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写库；不加这个参数只预览",
    )
    parser.add_argument(
        "--db",
        default="",
        help="指定数据库路径（默认用 data/wallet_link.db）",
    )
    args = parser.parse_args()

    path = Path(args.db) if args.db else database_path()
    if not path.exists():
        print(f"找不到数据库：{path}")
        return 1
    print(f"数据库：{path}")
    database = Database(path)
    try:
        items = find_residue(database)
        if not items:
            print("没有发现需要修正的记录。")
            return 0
        print(f"\n发现 {len(items)} 条「确认后仍残留旧库名」的地址：\n")
        print(f"{'id':>6}  {'原标签':<22} {'改为':<16} 来源")
        print("-" * 78)
        for item in items:
            print(
                f"{item['id']:>6}  {item['label']:<22} {item['target']:<16} {item['source']}"
            )
            print(f"        {item['address']}")
        if not args.apply:
            print("\n以上只是预览，没有改动任何数据。")
            print("确认无误后加 --apply 再执行一次：")
            print("  .venv\\Scripts\\python.exe tools/fix_exchange_label_residue.py --apply")
            return 0
        changed = apply_fix(database, items)
        print(f"\n已修正 {changed} 条。")
        print("刷新页面（Ctrl+F5）后，这些地址会出现在「人工确认交易所」/「公共地址」分组里。")
        return 0
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
