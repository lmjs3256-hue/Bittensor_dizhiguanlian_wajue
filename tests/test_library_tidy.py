# -*- coding: utf-8 -*-
"""地址库「一键整理 / 修复污染」（tidy）回归测试。

覆盖：
- 多分类里的孤立分类名、重复项、缺失主分类被规整；
- 主分类本身非法时回落「其他」；
- 系统角色地址（PUBLIC / EXCHANGE 等）残留的用户库归属被清空；
- 交易所锁定残留（确认标记还在、但当前已不是交易所）被清掉；
- 官方 / 内置固定名单的交易所结论不被误清；
- dry_run 只报告、不落库；
- 整理后可用 undo_last_import 整体还原。
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app.service as service_module
from app.db import AddressLibrary, Database
from app.service import LinkService


ADDR_A = "5C51bmwMxk9TTswMj2wkiCvdAyWDo3wXw9i4Bcoqf93WjaHP"
ADDR_B = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
ADDR_C = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


def build_service(directory, name="tidy.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def _insert(database, **fields):
    """直接写库，绕过 import_rows 的归一化，模拟跨机同步 / 旧包留下的脏数据。"""

    with database.session() as db:
        db.add(AddressLibrary(**fields))


def _row(service, address):
    for item in service.library():
        if item["address"] == address:
            return item
    return None


def test_tidy_cleans_orphan_duplicate_and_missing_main():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            _insert(
                database,
                address=ADDR_A,
                category="群友",
                categories=json.dumps(["群友", "群友", "不存在的旧分类"], ensure_ascii=False),
            )
            report = service.tidy_library(dry_run=False)

            assert report["changed"] == 1
            assert report["fixes"]["categories"] == 1
            row = _row(service, ADDR_A)
            # 孤立分类名被丢弃、重复项去重，主分类保留并排第一。
            assert row["category"] == "群友"
            assert row["categories"] == ["群友"]
        finally:
            database.engine.dispose()


def test_tidy_fills_empty_main_category():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            # 主分类为空的畸形记录：补成默认分类「其他」，并同步到多分类。
            _insert(database, address=ADDR_A, category="", categories="")
            report = service.tidy_library(dry_run=False)

            assert report["changed"] == 1
            assert report["fixes"]["category"] == 1
            row = _row(service, ADDR_A)
            assert row["category"] == "其他"
            assert row["categories"] == ["其他"]
        finally:
            database.engine.dispose()


def test_tidy_clears_book_for_system_role():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            # 污染态：PUBLIC 地址却挂在用户自建库 6NCK 下。
            _insert(
                database,
                address=ADDR_A,
                category="大V",
                categories=json.dumps(["大V", "PUBLIC"], ensure_ascii=False),
                book="6NCK",
                label="公共汇聚地址",
            )
            report = service.tidy_library(dry_run=False)

            assert report["fixes"]["book"] == 1
            row = _row(service, ADDR_A)
            assert row["book"] == ""
            # 公共地址身份和人工分类都保留，只把库归属清掉。
            assert "PUBLIC" in row["categories"]
            assert row["label"] == "公共汇聚地址"
        finally:
            database.engine.dispose()


def test_tidy_clears_stale_exchange_flag():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            # 确认过交易所，但分类早已改回普通分组，锁定标记成了残留。
            _insert(
                database,
                address=ADDR_A,
                category="其他",
                categories=json.dumps(["其他"], ensure_ascii=False),
                exchange_confirmed=True,
            )
            report = service.tidy_library(dry_run=False)

            assert report["fixes"]["exchange"] == 1
            row = _row(service, ADDR_A)
            assert row["exchange_confirmed"] is False
        finally:
            database.engine.dispose()


def test_tidy_keeps_official_exchange_conclusion():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            # 官方名单同步来的交易所：分类固定为 EXCHANGE，锁定标记不能被清掉。
            _insert(
                database,
                address=ADDR_B,
                category="EXCHANGE",
                categories=json.dumps(["EXCHANGE"], ensure_ascii=False),
                exchange_confirmed=True,
                source="taostats:exchange",
            )
            report = service.tidy_library(dry_run=False)

            assert report["changed"] == 0
            row = _row(service, ADDR_B)
            assert row["exchange_confirmed"] is True
        finally:
            database.engine.dispose()


def test_tidy_dry_run_reports_without_writing():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            _insert(
                database,
                address=ADDR_A,
                category="群友",
                categories=json.dumps(["群友", "孤立名"], ensure_ascii=False),
            )
            report = service.tidy_library(dry_run=True)

            assert report["dry_run"] is True
            assert report["changed"] == 1
            # 预览不落库，脏分类原样保留。
            row = _row(service, ADDR_A)
            assert row["categories"] == ["群友", "孤立名"]
        finally:
            database.engine.dispose()


def test_undo_after_tidy_restores_original():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            _insert(
                database,
                address=ADDR_A,
                category="群友",
                categories=json.dumps(["群友", "孤立名"], ensure_ascii=False),
            )
            _insert(
                database,
                address=ADDR_C,
                category="大V",
                categories=json.dumps(["大V", "PUBLIC"], ensure_ascii=False),
                book="6NCK",
            )
            report = service.tidy_library(dry_run=False)
            assert report["changed"] == 2

            # 整理后两条都被修过。
            assert _row(service, ADDR_A)["categories"] == ["群友"]
            assert _row(service, ADDR_C)["book"] == ""

            undone = service.undo_last_import()
            assert undone["restored"] == 2

            # 撤回后恢复成整理前的脏数据。
            assert _row(service, ADDR_A)["categories"] == ["群友", "孤立名"]
            assert _row(service, ADDR_C)["book"] == "6NCK"
        finally:
            database.engine.dispose()


def test_tidy_ignores_empty_categories_field():
    """categories 为空串只是「只有主分类」的存储形态，不算污染。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            _insert(database, address=ADDR_A, category="其他", categories="")
            report = service.tidy_library(dry_run=False)
            assert report["changed"] == 0
            row = _row(service, ADDR_A)
            assert row["categories"] == ["其他"]
        finally:
            database.engine.dispose()


def test_tidy_ignores_pure_duplicate_categories():
    """纯重复的分类名读取时已去重，不影响语义，不该被判为待整理。"""

    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            _insert(
                database,
                address=ADDR_A,
                category="群友",
                categories=json.dumps(["群友", "群友"], ensure_ascii=False),
            )
            report = service.tidy_library(dry_run=False)
            assert report["changed"] == 0
        finally:
            database.engine.dispose()


def test_tidy_on_clean_library_reports_nothing():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows([{"address": ADDR_A, "category": "群友"}], "manual")
            report = service.tidy_library(dry_run=False)
            assert report["changed"] == 0
            assert service.last_import_batch()["kind"] == "import"
        finally:
            database.engine.dispose()
