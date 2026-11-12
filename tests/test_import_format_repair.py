"""导入地址时自动修正格式：去重、认备注、跳过非法、容错各种分隔。

**用户要求（原话）**：「导入地址模块增加自动修正格式功能，比如我发的这份
地址，可能有重复，有备注，有空格等，我直接导入要能自动修正格式，返回实际
导入几个，几个重复的，几个地址不正确的。导出的时候是标准的格式。」
以及补充：「有备注仍然自动保存备注」。

旧行为：`parse_plain_addresses` 每行只取「逗号前第一段」再取「第一个空白
前的 token」。一行里写了多个地址时，第二段之后**静默丢弃**——不报错，
用户以为全导进去了；备注也全丢。

现在：先按 base58 字符集把「形似 SS58」的片段全抠出来，再用真校验和
（`is_valid_ss58`，blake2b）判定，因此不依赖行结构与分隔符。地址后紧跟
的文字被识别为备注并保存。
"""

from __future__ import annotations

import pytest

from app.db import Database
from app.inputs import parse_messy_addresses
from app.service import LinkService

# 全部是校验和合法的真实 SS58（长度 48）。
A = "5DohFrx7u4HFuBYgncNY4FivhjpNLnuYtLu9gKJDU1zBx5EW"
B = "5FeqQ5L16UWbgxEZs83fHFU7A3Sp161tKTCbBQpXDbixDTL8"
C = "5HYYMyRXHB2KvLDUoWKTtzqYGbTYbdd4D3QTYdqoGscmmndg"
D = "5D1V8ZonkgNJFSDshEyaSk8Cg6MwcrUuWbfXehXnwS1A7x5H"


@pytest.fixture()
def svc(tmp_path):
    db = Database(tmp_path / "import.db")
    db.initialize()
    service = LinkService(db, "wss://example.invalid")
    yield service
    db.engine.dispose()


# -- 一行多个地址：旧解析器会丢，这里是核心回归 --------------------------------
def test_multiple_addresses_on_one_line_are_all_kept():
    rows, report = parse_messy_addresses(f"{A},{B}   {C}\t{D}")
    assert [row["address"] for row in rows] == [A, B, C, D]
    assert report["duplicate"] == []
    assert report["invalid"] == []


def test_multiple_addresses_do_not_leak_into_remark():
    """一行多个地址时，后面的地址不能被当成前面那个的备注。"""

    rows, _ = parse_messy_addresses(f"{A} {B}")
    assert [row["label"] for row in rows] == ["", ""]


# -- 备注自动保存 -------------------------------------------------------------
def test_remark_after_comma_is_saved_as_label():
    rows, report = parse_messy_addresses(f"{A},存储钱包")
    assert rows[0]["label"] == "存储钱包"
    assert report["remarks"] == ["存储钱包"]


def test_remark_after_space_is_saved_as_label():
    rows, _ = parse_messy_addresses(f"{A}  主钱包  ")
    assert rows[0]["label"] == "主钱包"


def test_full_width_comma_is_a_separator():
    rows, _ = parse_messy_addresses(f"{A}，群友")
    assert rows[0]["label"] == "群友"


def test_remark_is_not_eaten_by_trailing_comma():
    rows, report = parse_messy_addresses(f"{A},")
    assert rows[0]["label"] == ""
    assert report["remarks"] == []


def test_csv_extra_columns_do_not_become_remark():
    """``地址,主钱包,其它列`` —— 只取紧邻的第一段，不要把后面列拼进来。"""

    rows, _ = parse_messy_addresses(f"{A},主钱包,x")
    assert rows[0]["label"] == "主钱包"


# -- 去重 --------------------------------------------------------------------
def test_duplicate_keeps_first_remark_and_is_counted():
    rows, report = parse_messy_addresses(f"{A},甲\n{A},乙\n{B},")
    assert [row["address"] for row in rows] == [A, B]
    assert rows[0]["label"] == "甲", "重复项保留第一次出现的备注"
    assert report["duplicate"] == [A]


def test_duplicates_are_counted_per_occurrence():
    rows, report = parse_messy_addresses(f"{A}\n{A}\n{A}\n{B}")
    assert len(rows) == 2
    assert report["duplicate"] == [A, A]


# -- 非法与粘贴垃圾 -----------------------------------------------------------
def test_invalid_address_is_reported_not_silently_dropped():
    broken = A[:-1] + "X"  # 改掉校验位，长度仍对但校验和不过
    rows, report = parse_messy_addresses(f"{B}\n{broken}")
    assert [row["address"] for row in rows] == [B]
    assert broken in report["invalid"]


def test_paste_garbage_glued_to_address_is_stripped():
    """``地址vH3o`` 这种复制残留：地址要救回来，垃圾不能变成备注。"""

    rows, report = parse_messy_addresses(f"{A}vH3o")
    assert rows[0]["address"] == A
    assert rows[0]["label"] == ""
    assert report["invalid"] == []


def test_paste_garbage_before_real_remark_keeps_the_remark():
    rows, _ = parse_messy_addresses(f"{A}vH3o,存储")
    assert rows[0]["address"] == A
    assert rows[0]["label"] == "存储"


def test_short_paste_residue_in_remark_is_flagged_but_saved():
    """像 ``vH3o`` 这样的残留备注仍然保存（用户要求），但要能被提示出来核对。"""

    rows, report = parse_messy_addresses(f"{A},vH3o")
    assert rows[0]["label"] == "vH3o", "备注要自动保存"
    assert report["suspect"] == ["vH3o"], "同时列入可疑，让用户能发现"


def test_chinese_remark_is_never_flagged_as_suspect():
    _, report = parse_messy_addresses(f"{A},存储钱包")
    assert report["suspect"] == []


# -- 空格 / 表头 / 注释 -------------------------------------------------------
def test_surrounding_whitespace_is_trimmed():
    rows, _ = parse_messy_addresses(f"   {A}   \n\t{B}\t")
    assert [row["address"] for row in rows] == [A, B]


def test_exported_csv_header_is_skipped():
    text = "address,label,category,note\n" f"{A},主钱包,其他,\n" f"{B},,其他,\n"
    rows, _ = parse_messy_addresses(text)
    assert [row["address"] for row in rows] == [A, B]


def test_blank_lines_and_hash_comment_lines_are_ignored():
    text = f"\n# 这是说明\n\n{A}\n# 又一行说明\n{B}\n"
    rows, _ = parse_messy_addresses(text)
    assert [row["address"] for row in rows] == [A, B]


def test_address_with_hash_remark_is_not_truncated():
    """``地址#备注``：地址前面已有真地址，所以 # 不是注释起始。"""

    rows, _ = parse_messy_addresses(f"{A}#备注")
    assert rows[0]["address"] == A


# -- 端到端：导入统计与落库 ---------------------------------------------------
def test_import_returns_counts_and_saves_remarks(svc):
    broken = C[:-1] + "X"  # 校验和不对
    text = f"{A},存储钱包\n{B},主钱包\n{A},重复的\n{C}\n{broken},"
    result = svc.import_upload("yuma.txt", text)

    assert result["inserted"] == 3, "A/B/C 三个有效地址"
    assert result["duplicate"] == 1, "A 重复一次"
    assert result["invalid"] == 1, "校验和不对的那个"
    assert result["remarks"] == 2, "两个带备注的"
    assert result["total_found"] == 5, "3 有效 + 1 重复 + 1 非法"

    library = {item["address"]: item for item in svc.library()}
    assert library[A]["label"] == "存储钱包"
    assert library[B]["label"] == "主钱包"
    assert library[A].get("label_custom") is True, "自定义标签要标记出来"
    assert C in library, "C 本身合法，应当入库"
    assert broken not in library, "校验和不对的地址不能入库"


def test_import_does_not_clear_existing_remark(svc):
    """再次导入同地址但没写备注时，原有备注不能被清空。"""

    svc.import_upload("first.txt", f"{A},存储钱包")
    svc.import_upload("second.txt", f"{A},")

    library = {item["address"]: item for item in svc.library()}
    assert library[A]["label"] == "存储钱包"


def test_clean_file_imports_without_any_problems(svc):
    result = svc.import_upload("clean.txt", f"{A}\n{B}\n{C}\n")
    assert result["inserted"] == 3
    assert result["duplicate"] == 0
    assert result["invalid"] == 0
    assert result["remarks"] == 0


def test_reimporting_same_file_is_idempotent(svc):
    text = f"{A},存储钱包\n{B},主钱包\n"
    first = svc.import_upload("a.txt", text)
    second = svc.import_upload("a.txt", text)

    assert first["inserted"] == 2
    assert second["inserted"] == 0, "第二次不该重复新增"
    assert second["duplicate"] == 0, "文件内部没有重复"


def test_export_is_standard_csv_with_header(svc):
    """导出必须是标准格式：带表头、地址是独立列。"""

    svc.import_upload("a.txt", f"{A},存储钱包")
    content, media = svc.export_library("csv")

    first_line = content.splitlines()[0]
    assert first_line.startswith("address,"), "导出要有 address 表头"
    assert A in content
    assert "text/csv" in media
    assert "," in first_line, "标准 CSV 用逗号分隔"


def test_export_roundtrip_preserves_remark(svc):
    """导出的标准格式再导回来，备注还要在。"""

    svc.import_upload("a.txt", f"{A},存储钱包\n{B},主钱包")
    exported, _ = svc.export_library("csv")

    other = LinkService(Database(svc.database.path.parent / "other.db"), "wss://x")
    other.database.initialize()
    try:
        other.import_upload("exported.csv", exported)
        library = {item["address"]: item for item in other.library()}
        assert library[A]["label"] == "存储钱包"
        assert library[B]["label"] == "主钱包"
    finally:
        other.database.engine.dispose()
