# -*- coding: utf-8 -*-
"""跨机同步包（schema v2）的「最后写入优先」（LWW）合并回归测试。

覆盖：
- 导出 JSON 带 created_at/updated_at 且 schema_version=2，CSV 同样携带时间戳列；
- 传入行 updated_at 较新时覆盖 note、并以空值清空 book（修复系统角色误挂用户库的污染）；
- 传入行 updated_at 较旧时不覆盖现有值；
- 无时间戳的旧包（v1 / 纯 TXT）仍按保守合并导入，行为不变。
"""

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import select

import app.service as service_module
from app.db import Database
from app.service import LinkService


# 复用其他测试里已确认合法的 SS58 地址。
ADDR_A = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
ADDR_B = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


def build_service(directory, name="sync.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def _ts(offset_days: int) -> str:
    """相对当前 UTC 的 ISO 时间戳，用于构造「较新 / 较旧」的同步包。"""

    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat()


def _fetch(service, address):
    for item in service.library():
        if item["address"] == address:
            return item
    return None


def test_export_json_carries_timestamps_and_schema_v2():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows([{"address": ADDR_A, "note": "hello"}], "manual")
            content, media = service.export_library("json")
            assert "application/json" in media
            payload = json.loads(content)
            # 同步包升级到 v2。
            assert payload.get("schema_version") == 2
            assert payload.get("format") == "bittensor-address-library"
            items = payload["addresses"]
            assert len(items) == 1
            item = items[0]
            assert item["address"] == ADDR_A
            # 时间戳字段存在且非空。
            assert item.get("created_at")
            assert item.get("updated_at")
        finally:
            database.engine.dispose()


def test_export_csv_carries_timestamp_columns():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows([{"address": ADDR_A, "book": "home"}], "manual")
            content, media = service.export_library("csv")
            assert "text/csv" in media
            reader = csv.DictReader(io.StringIO(content))
            assert "created_at" in reader.fieldnames
            assert "updated_at" in reader.fieldnames
            rows = list(reader)
            assert rows and rows[0]["address"] == ADDR_A
            assert rows[0]["updated_at"]
        finally:
            database.engine.dispose()


def test_lww_newer_incoming_overrides_note_and_clears_book():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            # 第一次导入：旧备注 + 误挂的用户库。
            service.import_rows(
                [{"address": ADDR_A, "note": "旧的", "book": "6NCK"}], "manual"
            )
            # 第二次导入：来自另一台机器的较新同步包，备注更新、book 已清空。
            result = service.import_rows(
                [
                    {
                        "address": ADDR_A,
                        "note": "新的",
                        "book": "",
                        "updated_at": _ts(1),
                    }
                ],
                "sync.json",
            )
            assert result["updated"] == 1
            current = _fetch(service, ADDR_A)
            # 较新一方为准：备注被覆盖、book 被清空（污染修复）。
            assert current["note"] == "新的"
            assert (current["book"] or "") == ""
        finally:
            database.engine.dispose()


def test_lww_older_incoming_does_not_override():
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows(
                [{"address": ADDR_A, "note": "当前", "book": "home"}], "manual"
            )
            # 较旧的同步包不应回写。
            result = service.import_rows(
                [
                    {
                        "address": ADDR_A,
                        "note": "过时",
                        "book": "stale",
                        "updated_at": _ts(-1),
                    }
                ],
                "sync.json",
            )
            assert result["updated"] == 0
            assert result["skipped"] == 1
            current = _fetch(service, ADDR_A)
            assert current["note"] == "当前"
            assert current["book"] == "home"
        finally:
            database.engine.dispose()


def test_lww_book_clear_fixes_system_role_pollution():
    """模拟「公共地址在用户库（6NCK）」的污染，经较新同步包后 book 被清空。"""
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            # 污染态：PUBLIC 地址却挂在用户库 6NCK 下。
            service.import_rows(
                [{"address": ADDR_B, "category": "PUBLIC", "book": "6NCK"}], "manual"
            )
            # 主机把该公共地址移出用户库（book 置空），导出较新同步包。
            result = service.import_rows(
                [
                    {
                        "address": ADDR_B,
                        "category": "PUBLIC",
                        "book": "",
                        "updated_at": _ts(1),
                    }
                ],
                "sync.json",
            )
            assert result["updated"] == 1
            current = _fetch(service, ADDR_B)
            # 污染被同步清掉：公共地址不再挂在用户库下。
            assert current["category"] == "PUBLIC"
            assert (current["book"] or "") == ""
        finally:
            database.engine.dispose()


def test_v1_export_without_timestamps_imports_conservatively():
    """无时间戳的旧包（如纯 TXT / 老 CSV）仍按保守合入，不触发 LWW。"""
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.import_rows([{"address": ADDR_A, "note": "初始"}], "manual")
            # 纯地址 TXT：没有时间戳，应正常新增/更新且不报错。
            result = service.import_rows(
                [{"address": ADDR_A, "note": "覆盖"}], "manual-txt"
            )
            assert result["updated"] == 1
            current = _fetch(service, ADDR_A)
            # 保守合并：非空备注直接覆盖（旧行为不变）。
            assert current["note"] == "覆盖"
        finally:
            database.engine.dispose()
