# -*- coding: utf-8 -*-
"""确认交易所 / 公共地址后，必须真的从原地址库里"消失"（0.8.17）。

用户报告的现场：地址 `5FqLi5EG1XJS...` 名字是「SN46项目方相关」，在扫描结果里
点「人工确认交易所」，结果只完成了"移出库"这一步：

1. 地址库里它仍在「SN46项目方相关」这个组下面；
2. 扫描结果刷新后，库名徽标还是「SN46项目方相关」。

根因：`create_address_book` 建库时把库名**同时**写进 `book` 和 `label`
（`label=book_name`）。确认交易所只清了 `book`，`label` 还留着旧库名，而分组
和前端的库名徽标读的都是 `label`。另外地址搬空后 `address_books` 台账仍留着
那条记录，界面上就是个「0 个地址」的空库。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import select

from app.db import AddressLibrary, Database
from app.service import LinkService, book_derived_label, library_group_key


A = "5FqLi5EG1XJSjWRJrwGDkAFKHKcKPoaibdTSCm8ECCozM2T6"
B = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
C = "5Ek7jBfCW13KmTAvnqm2cHmkyCNVJaUJ7mZMFaARXK2F6iNp"
BOOK = "SN46项目方相关"


def build_service(directory):
    database = Database(Path(directory) / "exchange-label.db")
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def test_confirm_exchange_clears_book_derived_label():
    """核心回归：库名派生出来的 label 必须跟着一起归位。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(
                name=BOOK, seed=A, addresses=[], category="大V"
            )
            with database.session() as db:
                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == A)
                )
                # 建库时 label 与 book 相同：这是"库名复制品"的判据。
                assert row.label == BOOK == row.book

            item = service.decide_exchange_address(A, "confirm")["item"]

            assert item["exchange_confirmed"] is True
            assert item["book"] == ""
            assert item["category"] == "EXCHANGE"
            # 不再显示成旧库名，而是落到交易所确认组。
            assert item["label"] == "人工确认交易所"
            with database.session() as db:
                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == A)
                )
                assert library_group_key(row) == ("label", "人工确认交易所")
        finally:
            database.engine.dispose()


def test_confirm_exchange_keeps_hand_written_label():
    """人工起的标签不能被当成库名复制品冲掉（只是要移出库）。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(
                name=BOOK, seed=A, addresses=[], category="大V"
            )
            # 人工改成自己的命名（与库名不同）
            service.import_rows(
                [{"address": A, "label": "我自己的备注名", "book": BOOK}], "manual"
            )

            item = service.decide_exchange_address(A, "confirm")["item"]

            assert item["label"] == "我自己的备注名"
            assert item["book"] == ""
        finally:
            database.engine.dispose()


def test_confirm_exchange_keeps_pure_manual_label_without_book():
    """没有库、只有人工标签的地址：标签原样保留。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows([{"address": B, "label": "纯人工标签", "category": "群友"}], "m")
            item = service.decide_exchange_address(B, "confirm")["item"]
            assert item["label"] == "纯人工标签"
            assert item["book"] == ""
        finally:
            database.engine.dispose()


def test_confirm_exchange_via_row_id_also_moves_out_of_book():
    """地址库清单那条路径（按 row_id）以前漏了清 book，行为必须和上面一致。

    这个入口只对已经是 EXCHANGE 分类、待人工确认的地址开放（界面上那些
    「确认交易所」按钮就长在这样的行上），所以先把地址设成待确认状态。
    """

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            # 造出「EXCHANGE 分类 + 库名派生标签 + 未人工确认」的待确认行。
            with database.session() as db:
                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == A)
                )
                row.category = "EXCHANGE"
                row.categories = '["EXCHANGE", "\\u5927V"]'
                row.exchange_confirmed = False
                db.flush()
                row_id = row.id

            item = service.decide_exchange(row_id, "confirm")["item"]

            assert item["exchange_confirmed"] is True
            assert item["book"] == ""
            assert item["category"] == "EXCHANGE"
            assert item["label"] == "人工确认交易所"
        finally:
            database.engine.dispose()


def test_empty_book_registry_is_removed():
    """库被搬空后，台账里不留「0 个地址」的空壳。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[B], category="大V")
            assert [item["name"] for item in service.address_books()] == [BOOK]

            # 还有一条在库里 → 保留
            service.decide_exchange_address(A, "confirm")
            books = service.address_books()
            assert [item["name"] for item in books] == [BOOK]
            assert books[0]["count"] == 1

            # 搬空 → 台账记录一并删除
            service.decide_exchange_address(B, "confirm")
            assert service.address_books() == []
        finally:
            database.engine.dispose()


def test_mark_public_address_moves_out_of_book_with_label():
    """公共地址与交易所同性质，同样的残留问题一并修。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name="6NCK", seed=C, addresses=[], category="群友")
            item = service.decide_public_address(C, True)["item"]

            assert item["book"] == ""
            assert item["label"] == "公共地址"
            assert service.address_books() == []
            with database.session() as db:
                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == C)
                )
                assert library_group_key(row) == ("label", "公共地址")
        finally:
            database.engine.dispose()


def test_book_derived_label_ignores_manual_and_mismatched():
    """辅助函数的口径：只有 label == book 才算库名复制。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            with database.session() as db:
                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == A)
                )
                assert book_derived_label(row) == BOOK

                row.book = ""
                # 库已被清空而 label 还在：此时无法证明是库名复制，保守不动。
                assert book_derived_label(row) == ""

                row.book = BOOK
                row.label = "手写标签"
                assert book_derived_label(row) == ""

                row.book = ""
                row.label = ""
                assert book_derived_label(row) == ""
        finally:
            database.engine.dispose()


def test_reject_does_not_move_out_of_book():
    """否定交易所只是清掉候选身份，不该把人整理好的库归属拆掉。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            item = service.decide_exchange_address(A, "reject")["item"]

            assert item["exchange_rejected"] is True
            assert item["book"] == BOOK
            assert item["label"] == BOOK
        finally:
            database.engine.dispose()


def test_confirm_then_reject_relabels_to_rejection_group():
    """已确认后又改判否定：标签跟着改，避免停在「人工确认交易所」组里。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            service.decide_exchange_address(A, "confirm")
            item = service.decide_exchange_address(A, "reject")["item"]

            assert item["exchange_rejected"] is True
            assert item["label"] == "人工否定交易所"
        finally:
            database.engine.dispose()


def test_unmark_public_restores_original_book():
    """取消公共标记要把地址还回原来的库，不能变成一个「公共地址」孤儿。

    标记时若把库搬空了，台账会被清掉；取消时得把台账一并重建。
    """

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name="6NCK", seed=A, addresses=[], category="群友")
            service.decide_public_address(A, True)
            assert service.address_books() == []

            item = service.decide_public_address(A, False)["item"]

            assert item["label"] == "6NCK"
            assert item["book"] == "6NCK"
            # 原库名标记用完即清，别留在备注里当噪音。
            assert "[原库]" not in (item["note"] or "")
            books = service.address_books()
            assert [(b["name"], b["count"]) for b in books] == [("6NCK", 1)]
        finally:
            database.engine.dispose()


def test_unmark_public_without_book_returns_to_unnamed():
    """本来就没有库的裸地址：取消标记后回到按分类归组，不该留「公共地址」标签。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows([{"address": B, "category": "群友"}], "manual")
            service.decide_public_address(B, True)
            item = service.decide_public_address(B, False)["item"]

            assert item["book"] == ""
            assert item["label_custom"] is False
        finally:
            database.engine.dispose()


def test_public_and_exchange_paths_behave_the_same():
    """用户要求：标为公共地址的逻辑与人工确认交易所一致。"""

    def snapshot(kind: str):
        with TemporaryDirectory() as directory:
            database, service = build_service(directory)
            try:
                service.create_address_book(
                    name=BOOK, seed=A, addresses=[], category="大V"
                )
                if kind == "exchange":
                    item = service.decide_exchange_address(A, "confirm")["item"]
                else:
                    item = service.decide_public_address(A, True)["item"]
                with database.session() as db:
                    row = db.scalar(
                        select(AddressLibrary).where(AddressLibrary.address == A)
                    )
                    group = library_group_key(row)
                return {
                    "book": item["book"],
                    "group_kind": group[0],
                    "group_name_is_label": group[1] == item["label"],
                    "books_left": service.address_books(),
                }
            finally:
                database.engine.dispose()

    exchange = snapshot("exchange")
    public = snapshot("public")
    # 除了标签名本身，两者的行为必须完全对齐。
    assert exchange["book"] == public["book"] == ""
    assert exchange["group_kind"] == public["group_kind"] == "label"
    assert exchange["group_name_is_label"] is True
    assert public["group_name_is_label"] is True
    assert exchange["books_left"] == public["books_left"] == []
