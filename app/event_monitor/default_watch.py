"""Seed event monitoring from named SN1-SN160 address books exactly once."""

from __future__ import annotations

import re
from typing import Any


DEFAULT_SUBNET_WATCH_STATE = "watch:default-subnet-books:v1"
SUBNET_BOOK = re.compile(r"^SN\s*(\d+)", re.IGNORECASE)


# 子网编号上限（1.0.29）：从 128 提高到 160，与链上当前子网上限保持一致。
# 前端弹窗默认预选、项目方同步等处都按这个口径，改这里要同步改前端与规则文档。
MAX_DEFAULT_SUBNET = 160


def eligible_default_subnet_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for item in items:
        book = str(item.get("book") or "").strip()
        match = SUBNET_BOOK.match(book)
        if not match:
            continue
        netuid = int(match.group(1))
        if 1 <= netuid <= MAX_DEFAULT_SUBNET:
            eligible.append(item)
    return eligible


def seed_default_subnet_watch(store: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
    state = store.get_cursor(DEFAULT_SUBNET_WATCH_STATE) or {}
    seeded_books = {str(name) for name in state.get("seeded_books", []) if str(name).strip()}
    if getattr(store, "address_list_locked", lambda: False)():
        return {
            "seeded": False,
            "reason": "address_list_locked",
            "seeded_books": sorted(seeded_books),
            "added": 0,
            "skipped": 0,
        }
    by_book: dict[str, list[dict[str, Any]]] = {}
    for item in eligible_default_subnet_items(items):
        book = str(item.get("book") or "").strip()
        by_book.setdefault(book, []).append(item)
    if not by_book:
        return {
            "seeded": False,
            "reason": "no_sn_books",
            "seeded_books": sorted(seeded_books),
            "added": 0,
            "skipped": 0,
        }

    added = 0
    skipped = 0
    new_books: list[str] = []
    new_book_counts: dict[str, int] = {}
    for book in sorted(by_book, key=lambda value: (int(SUBNET_BOOK.match(value).group(1)), value)):
        if book in seeded_books:
            continue
        book_added = 0
        for item in by_book[book]:
            address = str(item.get("address") or "").strip()
            if not address:
                skipped += 1
                continue
            try:
                store.add_watched_address(
                    "finney",
                    address,
                    str(item.get("label") or book)[:120],
                    0,
                    library_book=book,
                    address_role=(
                        "project_owner"
                        if item.get("source") == "bittensor:SubtensorModule.SubnetOwner"
                        else ""
                    ),
                )
                added += 1
                book_added += 1
            except ValueError:
                skipped += 1
        seeded_books.add(book)
        new_books.append(book)
        new_book_counts[book] = book_added

    if new_books:
        store.set_collector_state(
            DEFAULT_SUBNET_WATCH_STATE,
            {
                "seeded_books": sorted(seeded_books),
                "added": added,
                "skipped": skipped,
            },
        )
    return {
        "seeded": bool(new_books),
        "new_books": new_books,
        "new_book_counts": new_book_counts,
        "book_count": len(seeded_books),
        "added": added,
        "skipped": skipped,
    }
