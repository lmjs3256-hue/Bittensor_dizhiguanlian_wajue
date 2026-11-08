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


def test_reject_moves_out_of_book():
    """否定交易所同样把地址移出库（0.8.18 起的规则）。

    理由：这个地址已经有明确结论（不是交易所），不该继续占着人工整理的库；
    以后关联到别的库时再单独加回去。标签归位到「人工否定交易所」组。
    """

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            item = service.decide_exchange_address(A, "reject")["item"]

            assert item["exchange_rejected"] is True
            assert item["book"] == ""
            assert item["label"] == "人工否定交易所"
            # 库被搬空，台账一并清掉。
            assert service.address_books() == []
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


def test_unmark_public_moves_out_of_book():
    """取消公共标记同样直接移出库，不再把地址还回原库（0.8.18 起的规则）。

    理由：已经判定"公共地址不成立"的地址，不该继续占着人工整理的那个库；
    以后关联到别的库时再单独加回去。标签回到未命名，按分类归组。
    """

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name="6NCK", seed=A, addresses=[], category="群友")
            service.decide_public_address(A, True)
            assert service.address_books() == []

            item = service.decide_public_address(A, False)["item"]

            assert item["book"] == ""
            # 自动标签被清掉，回到未命名（按分类归组）。
            assert item["label_custom"] is False
            # 库不复活：不再为"还回去"重建台账。
            assert service.address_books() == []
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


def test_reset_moves_out_of_book_and_clears_auto_label():
    """重置为待确认也移出库，并把自动标签清成未命名（否则名字与结论自相矛盾）。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            service.decide_exchange_address(A, "confirm")
            item = service.decide_exchange_address(A, "reset")["item"]

            assert item["exchange_confirmed"] is False
            assert item["exchange_rejected"] is False
            assert item["book"] == ""
            assert item["label_custom"] is False
            assert service.address_books() == []
        finally:
            database.engine.dispose()


def test_reject_keeps_hand_written_label():
    """拒否定时人工起的名字不受影响（只移库）。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            service.import_rows(
                [{"address": A, "label": "我起的名字", "book": BOOK}], "manual"
            )
            item = service.decide_exchange_address(A, "reject")["item"]

            assert item["label"] == "我起的名字"
            assert item["book"] == ""
        finally:
            database.engine.dispose()


def test_row_id_entry_reject_also_moves_out():
    """地址库清单那条入口（row_id）的否定也必须移出库，与地址入口一致。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            with database.session() as db:
                row = db.scalar(
                    select(AddressLibrary).where(AddressLibrary.address == A)
                )
                row.category = "EXCHANGE"
                row.categories = '["EXCHANGE"]'
                db.flush()
                row_id = row.id

            item = service.decide_exchange(row_id, "reject")["item"]

            assert item["exchange_rejected"] is True
            assert item["book"] == ""
            assert item["label"] == "人工否定交易所"
        finally:
            database.engine.dispose()


# -- 0.8.25：备注不移库 + 验证者人工标记 -------------------------------------

def test_note_never_changes_book_membership():
    """备注只是角色标注，绝不能改库归属（用户明确要求）。

    三个真实案例：在 DSV 库里给地址写「中转地址 / 币安中转地址」备注，
    它们必须原地不动、仍在 DSV。
    """

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(
                name="DSV", seed=A, addresses=[B, C], category="群友"
            )
            before = {
                row["address"]: (row["book"], row["label"], row["category"])
                for row in service.library()
            }
            for address, note in ((A, "中转地址"), (B, "币安中转地址"), (C, "币安中转地址")):
                service.save_note(address, note)

            after = {
                row["address"]: (row["book"], row["label"], row["category"])
                for row in service.library()
            }
            assert before == after, "写备注前后库归属必须完全一致"
            assert all(row["book"] == "DSV" for row in service.library())
            notes = {row["address"]: row["note"] for row in service.library()}
            assert notes[A] == "中转地址"
            assert notes[B] == notes[C] == "币安中转地址"
        finally:
            database.engine.dispose()


def test_note_on_unknown_address_is_not_a_book():
    """给不在库里的地址写备注：不能凭空造出「其他地址库」。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            item = service.save_note(C, "某个不在库的地址")["item"]
            assert item["book"] == ""
            assert item["note_only"] is True, "仅备注记录要能被前端识别出来"
            assert service.address_books() == [], "不该因此产生任何地址库"
        finally:
            database.engine.dispose()


def test_note_only_flag_clears_once_address_joined_a_book():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.save_note(C, "先写备注")
            assert service.address_books() == []
            service.create_address_book(name="DSV", seed=C, addresses=[], category="群友")

            row = next(item for item in service.library() if item["address"] == C)
            assert row["book"] == "DSV"
            assert row["note_only"] is False
            assert row["note"] == "先写备注", "入库不该冲掉已写的备注"
        finally:
            database.engine.dispose()


def test_mark_validator_moves_out_of_book():
    """验证者与交易所/公共地址同口径：下结论即移出库。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            item = service.decide_validator_address(A, True)["item"]

            assert item["book"] == ""
            assert "VALIDATOR" in item["categories"]
            assert item["label"] == "验证者地址"
            assert service.address_books() == []
        finally:
            database.engine.dispose()


def test_unmark_validator_moves_out_and_clears_auto_label():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            service.decide_validator_address(A, True)
            item = service.decide_validator_address(A, False)["item"]

            assert item["book"] == ""
            assert "VALIDATOR" not in item["categories"]
            assert item["label_custom"] is False
        finally:
            database.engine.dispose()


def test_three_terminating_roles_share_the_same_behaviour():
    """会终止关联的只有这三类，且行为必须一致（用户明确要求）。"""

    def run(kind):
        with TemporaryDirectory() as directory:
            database, service = build_service(directory)
            try:
                service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
                if kind == "exchange":
                    item = service.decide_exchange_address(A, "confirm")["item"]
                elif kind == "validator":
                    item = service.decide_validator_address(A, True)["item"]
                else:
                    item = service.decide_public_address(A, True)["item"]
                return item["book"], bool(service.address_books()), item["label"]
            finally:
                database.engine.dispose()

    for kind in ("exchange", "validator", "public"):
        book, books_left, label = run(kind)
        assert book == "", f"{kind} 应移出库"
        assert books_left is False, f"{kind} 的库搬空后台账应清理"
        assert label, f"{kind} 应有结论性标签"


# -- 0.8.25：备注不移库 + 验证者人工标记 -------------------------------------

def test_note_never_changes_book_membership():
    """备注只是角色标注，绝不能改库归属（用户明确要求）。

    三个真实案例：在 DSV 库里给地址写「中转地址 / 币安中转地址」备注，
    它们必须原地不动、仍在 DSV。
    """

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(
                name="DSV", seed=A, addresses=[B, C], category="群友"
            )
            before = {
                row["address"]: (row["book"], row["label"], row["category"])
                for row in service.library()
            }
            for address, note in ((A, "中转地址"), (B, "币安中转地址"), (C, "币安中转地址")):
                service.save_note(address, note)

            after = {
                row["address"]: (row["book"], row["label"], row["category"])
                for row in service.library()
            }
            assert before == after, "写备注前后库归属必须完全一致"
            assert all(row["book"] == "DSV" for row in service.library())
            notes = {row["address"]: row["note"] for row in service.library()}
            assert notes[A] == "中转地址"
            assert notes[B] == notes[C] == "币安中转地址"
        finally:
            database.engine.dispose()


def test_note_on_unknown_address_is_not_a_book():
    """给不在库里的地址写备注：不能凭空造出「其他地址库」。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            item = service.save_note(C, "某个不在库的地址")["item"]
            assert item["book"] == ""
            assert item["note_only"] is True, "仅备注记录要能被前端识别出来"
            assert service.address_books() == [], "不该因此产生任何地址库"
        finally:
            database.engine.dispose()


def test_note_only_flag_clears_once_address_joined_a_book():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.save_note(C, "先写备注")
            assert service.address_books() == []
            service.create_address_book(name="DSV", seed=C, addresses=[], category="群友")

            row = next(item for item in service.library() if item["address"] == C)
            assert row["book"] == "DSV"
            assert row["note_only"] is False
            assert row["note"] == "先写备注", "入库不该冲掉已写的备注"
        finally:
            database.engine.dispose()


def test_mark_validator_moves_out_of_book():
    """验证者与交易所/公共地址同口径：下结论即移出库。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            item = service.decide_validator_address(A, True)["item"]

            assert item["book"] == ""
            assert "VALIDATOR" in item["categories"]
            assert item["label"] == "验证者地址"
            assert service.address_books() == []
        finally:
            database.engine.dispose()


def test_unmark_validator_moves_out_and_clears_auto_label():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
            service.decide_validator_address(A, True)
            item = service.decide_validator_address(A, False)["item"]

            assert item["book"] == ""
            assert "VALIDATOR" not in item["categories"]
            assert item["label_custom"] is False
        finally:
            database.engine.dispose()


def test_three_terminating_roles_share_the_same_behaviour():
    """会终止关联的只有这三类，且行为必须一致（用户明确要求）。"""

    def run(kind):
        with TemporaryDirectory() as directory:
            database, service = build_service(directory)
            try:
                service.create_address_book(name=BOOK, seed=A, addresses=[], category="大V")
                if kind == "exchange":
                    item = service.decide_exchange_address(A, "confirm")["item"]
                elif kind == "validator":
                    item = service.decide_validator_address(A, True)["item"]
                else:
                    item = service.decide_public_address(A, True)["item"]
                return item["book"], bool(service.address_books()), item["label"]
            finally:
                database.engine.dispose()

    for kind in ("exchange", "validator", "public"):
        book, books_left, label = run(kind)
        assert book == "", f"{kind} 应移出库"
        assert books_left is False, f"{kind} 的库搬空后台账应清理"
        assert label, f"{kind} 应有结论性标签"


def test_note_only_rows_import_into_new_book_with_notes_kept():
    """备注过的地址在建库时能正常进新库，备注保留（用户关心的主路径）。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            for address, note in ((A, "中转地址"), (B, "币安中转地址")):
                service.save_note(address, note)
            assert all(row["note_only"] for row in service.library())

            result = service.create_address_book(
                name="DSV2", seed="", addresses=[A, B], include_seed=False, category="群友"
            )

            assert result["attached"] == 2, "已备注的地址应在建库时被归入"
            rows = {row["address"]: row for row in service.library()}
            assert all(rows[a]["book"] == "DSV2" for a in (A, B))
            assert all(rows[a]["note_only"] is False for a in (A, B))
            assert rows[A]["note"] == "中转地址"
            assert rows[B]["note"] == "币安中转地址"
        finally:
            database.engine.dispose()


def test_adopt_note_only_into_existing_book():
    """「仅备注」组的一键归库：归进已有库，备注保留。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.create_address_book(name="DSV", seed=C, addresses=[], category="群友")
            service.save_note(A, "中转地址")
            ids = [row["id"] for row in service.library() if row["note_only"]]
            assert len(ids) == 1

            result = service.adopt_note_only(ids, "DSV")

            assert result["adopted"] == 1
            row = next(item for item in service.library() if item["address"] == A)
            assert row["book"] == "DSV"
            assert row["note"] == "中转地址"
            assert row["note_only"] is False
            counts = {book["name"]: book["count"] for book in service.address_books()}
            assert counts["DSV"] == 2
        finally:
            database.engine.dispose()


def test_adopt_note_only_skips_system_roles():
    """系统角色（交易所/验证者/公共地址）不该被这个入口抢进用户库。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.save_note(A, "先写个备注")
            service.decide_public_address(A, True)
            ids = [row["id"] for row in service.library()]
            try:
                service.adopt_note_only(ids, "DSV")
            except Exception as exc:  # InputError：没有可归库的
                assert "无需处理" in str(exc) or "都已归入别的库" in str(exc)
            else:
                raise AssertionError("系统角色不该被归入用户地址库")
            row = next(item for item in service.library() if item["address"] == A)
            assert "PUBLIC" in row["categories"]
        finally:
            database.engine.dispose()


# -- 0.8.28：关联强度筛选与排序（静态断言，锁住口径） --------------------

def _app_js():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return (root / "app" / "static" / "app.js").read_text(encoding="utf-8")


def test_strength_filter_is_part_of_base_visible_rows():
    """强度筛选必须进 relatedBaseRows()。

    只有进这里，建库（relatedVisibleRows）和导出才会自动跟着走；
    如果只在前端表格里过滤，建库就会把被强度筛掉的地址也导进去。
    """

    js = _app_js()
    start = js.index("function relatedBaseRows()")
    block = js[start: js.index("\n}\n", start)]
    assert "rowPassesStrengthFilter" in block, "强度筛选必须并入基础可见集合"
    assert "strengthFilter" in block


def test_strength_floor_values_are_ordered():
    """门槛定义必须单调：强及以上 3 > 中及以上 2 > 含弱 1。"""

    js = _app_js()
    start = js.index("const STRENGTH_FLOOR = {")
    block = js[start: js.index("};", start)]
    assert "strong: 3" in block
    assert "mid: 2" in block
    assert "weak: 1" in block


def test_table_renders_through_sorted_rows():
    """表格必须走 relatedVisibleRows()（含排序）。

    这里防的是"点了表头顺序不变"这类回归：直接用 baseRows.filter 会绕过
    sortByStrength，状态变了但界面没变。
    """

    js = _app_js()
    start = js.index("function refreshRelatedTable()")
    block = js[start: js.index("\n}\n", start)]
    assert "relatedVisibleRows()" in block, "表格渲染必须经过排序函数"
    assert "baseRows.filter(rowPassesAmountFilter)" not in block


def test_strength_header_is_the_sort_control():
    """用户要求"直接在关联强度上做功能，不另外加按钮"。"""

    js = _app_js()
    assert "data-sort-strength" in js
    # 表头本身就是 button，不能另起一个独立排序按钮
    assert "strength-head" in js
    assert 'class="strength-sort' in js


# -- 0.8.28：关联强度筛选与排序（静态断言，锁住口径） --------------------

def _app_js():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return (root / "app" / "static" / "app.js").read_text(encoding="utf-8")


def test_strength_filter_is_part_of_base_visible_rows():
    """强度筛选必须进 relatedBaseRows()。

    只有进这里，建库（relatedVisibleRows）和导出才会自动跟着走；
    如果只在前端表格里过滤，建库就会把被强度筛掉的地址也导进去。
    """

    js = _app_js()
    start = js.index("function relatedBaseRows()")
    block = js[start: js.index("\n}\n", start)]
    assert "rowPassesStrengthFilter" in block, "强度筛选必须并入基础可见集合"
    assert "strengthFilter" in block


def test_strength_floor_values_are_ordered():
    """门槛定义必须单调：强及以上 3 > 中及以上 2 > 含弱 1。"""

    js = _app_js()
    start = js.index("const STRENGTH_FLOOR = {")
    block = js[start: js.index("};", start)]
    assert "strong: 3" in block
    assert "mid: 2" in block
    assert "weak: 1" in block


def test_table_renders_through_sorted_rows():
    """表格必须走 relatedVisibleRows()（含排序）。

    这里防的是"点了表头顺序不变"这类回归：直接用 baseRows.filter 会绕过
    sortByStrength，状态变了但界面没变。
    """

    js = _app_js()
    start = js.index("function refreshRelatedTable()")
    block = js[start: js.index("\n}\n", start)]
    assert "relatedVisibleRows()" in block, "表格渲染必须经过排序函数"
    assert "baseRows.filter(rowPassesAmountFilter)" not in block


def test_strength_header_is_the_sort_control():
    """用户要求"直接在关联强度上做功能，不另外加按钮"。"""

    js = _app_js()
    assert "data-sort-strength" in js
    # 表头本身就是 button，不能另起一个独立排序按钮
    assert "strength-head" in js
    assert 'class="strength-sort' in js
