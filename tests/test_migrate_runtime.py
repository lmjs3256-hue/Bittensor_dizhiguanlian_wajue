"""跨机迁移工具 `tools/migrate_runtime.py` 的行为验证。

为什么需要它：地址库可以导出 JSON 再导入，但那个 JSON **只含地址库**。
本地索引、历史查询、巡检日志都在同一个 SQLite 里，换机器就丢——

- 本地索引丢了要从链头重扫（几万块）；
- 巡检游标丢了，已查过的地址要全部重查；
- 历史查询/入库日志丢了，页面直接空白。

要求：**幂等合并**，不是覆盖。远端可能已经跑过几轮巡检，不能被冲掉。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import migrate_runtime as mig  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    """建一个与真实库同构的最小库（只含迁移涉及的表）。"""

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table transfers (
            id integer primary key autoincrement,
            event_key text unique,
            tx_hash text, block_number integer, block_hash text,
            event_index integer, from_address text, to_address text,
            amount_rao integer, amount_tao numeric, timestamp text,
            edge_type text, netuid integer, hotkey text, created_at text,
            constraint uq_transfer_chain_event unique (block_number, event_index)
        );
        create table relation_archives (
            id integer primary key autoincrement,
            job_id text, seed text, payload text, created_at text
        );
        create table auto_import_log (
            id integer primary key autoincrement,
            address text, book text, trigger_address text, trigger_book text,
            amount_tao numeric, tx_count integer, stake_count integer,
            kind text, tx_hash text, first_block integer, last_block integer,
            reason text, status text, created_at text, revoked_at text
        );
        create table watch_state (
            address text primary key, last_block integer,
            last_checked_at text, checked_count integer, last_error text
        );
        create table watch_candidates (
            id integer primary key autoincrement,
            address text, book text, cumulative_tao numeric,
            first_block integer, last_block integer, tx_count integer,
            stake_count integer, trigger_address text,
            constraint uq_candidate_address_book unique (address, book)
        );
        create table book_pair_stats (
            id integer primary key autoincrement,
            book_a text, book_b text, total_tao numeric, tx_count integer,
            constraint uq_book_pair unique (book_a, book_b)
        );
        create table watch_runs (
            id integer primary key autoincrement,
            rpc_url text, status text, manual integer,
            addresses_checked integer, new_found integer, inserted integer,
            requests integer, error text, started_at text, finished_at text
        );
        """
    )
    conn.commit()
    return conn


def _seed_source(path: Path) -> None:
    conn = _make_db(path)
    conn.executemany(
        "insert into transfers (event_key,tx_hash,block_number,event_index,"
        "from_address,to_address,amount_rao,amount_tao,edge_type,created_at) "
        "values (?,?,?,?,?,?,?,?,?,?)",
        [
            ("k1", "0x1", 100, 0, "A", "B", 5_000_000_000, 5.0, "tao_transfer", "t1"),
            ("k2", "0x2", 101, 1, "B", "C", 3_000_000_000, 3.0, "tao_transfer", "t2"),
            ("k3", "0x3", 102, 2, "C", "D", 1_000_000_000, 1.0, "tao_transfer", "t3"),
        ],
    )
    conn.execute(
        "insert into relation_archives (job_id,seed,payload,created_at) values (?,?,?,?)",
        ("job-1", "A", '{"related":[]}', "t1"),
    )
    conn.execute(
        "insert into auto_import_log (address,book,amount_tao,tx_count,stake_count,"
        "kind,first_block,last_block,reason,status,created_at) "
        "values (?,?,?,?,?,?,?,?,?,?,?)",
        ("E", "6NCK", 45.0, 1, 0, "tao", 100, 100, "r", "active", "t1"),
    )
    conn.executemany(
        "insert into watch_state (address,last_block,last_checked_at,checked_count) "
        "values (?,?,?,?)",
        [("A", 500, "t1", 2), ("B", 600, "t2", 1)],
    )
    conn.execute(
        "insert into watch_candidates (address,book,cumulative_tao,first_block,"
        "last_block,tx_count,stake_count) values (?,?,?,?,?,?,?)",
        ("F", "6NCK", 12.0, 100, 100, 1, 0),
    )
    conn.execute(
        "insert into book_pair_stats (book_a,book_b,total_tao,tx_count) values (?,?,?,?)",
        ("6NCK", "DSV", 100.0, 4),
    )
    conn.execute(
        "insert into watch_runs (rpc_url,status,manual,addresses_checked,"
        "new_found,inserted,requests,error,started_at) values (?,?,?,?,?,?,?,?,?)",
        ("wss://x", "complete", 0, 10, 1, 1, 20, "", "t1"),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def source_db(tmp_path):
    path = tmp_path / "source.db"
    _seed_source(path)
    return path


@pytest.fixture()
def empty_target(tmp_path):
    path = tmp_path / "target.db"
    _make_db(path).close()
    return path


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(f"select count(*) from {table}").fetchone()[0]
            for table in mig.TABLES
        }
    finally:
        conn.close()


# -- 基本迁移 ---------------------------------------------------------------
def test_preview_does_not_write(source_db, empty_target):
    report = mig.run_merge(source_db, empty_target, apply=False)
    assert report.applied is False
    assert sum(t.inserted for t in report.tables) > 0
    # 预览后目标库必须还是空的
    assert all(v == 0 for v in _counts(empty_target).values())


def test_apply_migrates_all_tables(source_db, empty_target):
    mig.run_merge(source_db, empty_target, apply=True)
    counts = _counts(empty_target)
    assert counts["transfers"] == 3
    assert counts["relation_archives"] == 1
    assert counts["auto_import_log"] == 1
    assert counts["watch_state"] == 2
    assert counts["watch_candidates"] == 1
    assert counts["book_pair_stats"] == 1
    assert counts["watch_runs"] == 1


def test_merge_is_idempotent(source_db, empty_target):
    """重复合并不能产生重复数据——远端可能已经跑过几轮，不能被冲成两份。"""

    mig.run_merge(source_db, empty_target, apply=True)
    first = _counts(empty_target)
    second_report = mig.run_merge(source_db, empty_target, apply=True)
    assert _counts(empty_target) == first
    assert sum(t.inserted for t in second_report.tables) == 0
    assert sum(t.skipped for t in second_report.tables) > 0


# -- 关键：不能冲掉目标已有数据 ---------------------------------------------
def test_existing_target_rows_survive(source_db, empty_target):
    """远端已有的巡检游标 / 索引要先留着，只补缺失的。"""

    conn = sqlite3.connect(empty_target)
    conn.execute(
        "insert into watch_state (address,last_block,last_checked_at,checked_count) "
        "values (?,?,?,?)",
        ("A", 9999, "remote", 42),  # 与源库的 A 冲突，但值不同
    )
    conn.execute(
        "insert into transfers (event_key,tx_hash,block_number,event_index,"
        "from_address,to_address,amount_rao,amount_tao,edge_type,created_at) "
        "values (?,?,?,?,?,?,?,?,?,?)",
        ("remote-k", "0xr", 900, 0, "X", "Y", 1, 1.0, "tao_transfer", "tr"),
    )
    conn.commit()
    conn.close()

    mig.run_merge(source_db, empty_target, apply=True)

    conn = sqlite3.connect(empty_target)
    try:
        # 远端那条 A 的游标必须保留（不被源库的 500 覆盖）
        row = conn.execute(
            "select last_block, checked_count from watch_state where address='A'"
        ).fetchone()
        assert row == (9999, 42)
        # 远端自己的索引记录保留，源库 3 条补齐 → 共 4 条
        assert conn.execute("select count(*) from transfers").fetchone()[0] == 4
        assert conn.execute(
            "select count(*) from transfers where event_key='remote-k'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_same_block_slot_not_duplicated(source_db, empty_target):
    """transfers 有 (block_number, event_index) 唯一约束，跨机合并要按它去重。"""

    conn = sqlite3.connect(empty_target)
    # 同一区块槽位但 event_key 不同（不同机器生成的 key 可能不同）
    conn.execute(
        "insert into transfers (event_key,block_number,event_index,from_address,"
        "to_address,amount_rao,amount_tao,edge_type,created_at) "
        "values (?,?,?,?,?,?,?,?,?)",
        ("other-key", 100, 0, "A", "B", 5_000_000_000, 5.0, "tao_transfer", "tx"),
    )
    conn.commit()
    conn.close()

    mig.run_merge(source_db, empty_target, apply=True)
    conn = sqlite3.connect(empty_target)
    try:
        # 槽位 (100,0) 已存在 → 源库那条要跳过，不能因为 key 不同就插进去
        assert conn.execute(
            "select count(*) from transfers where block_number=100 and event_index=0"
        ).fetchone()[0] == 1
        assert conn.execute("select count(*) from transfers").fetchone()[0] == 3
    finally:
        conn.close()


# -- 导出 -------------------------------------------------------------------
def test_export_contains_only_runtime_tables(source_db, tmp_path):
    out = tmp_path / "pack.db"
    mig.run_export(source_db, out)
    assert out.exists()
    conn = sqlite3.connect(out)
    try:
        names = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table'")
        }
        assert set(mig.TABLES) <= names
        # 地址库不在迁移范围（它走 JSON 导出/导入）
        assert "address_library" not in names
        assert conn.execute("select count(*) from transfers").fetchone()[0] == 3
    finally:
        conn.close()


def test_export_refuses_to_overwrite(source_db, tmp_path):
    out = tmp_path / "pack.db"
    out.write_bytes(b"x")
    with pytest.raises(SystemExit):
        mig.run_export(source_db, out)


def test_export_only_selected_tables(source_db, tmp_path):
    out = tmp_path / "logs.db"
    mig.run_export(source_db, out, ["relation_archives", "auto_import_log"])
    conn = sqlite3.connect(out)
    try:
        names = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table'")
        }
        assert {"relation_archives", "auto_import_log"} <= names
        assert "transfers" not in names, "没选 transfers 就不该导进来"
    finally:
        conn.close()


def test_export_pack_can_be_merged(source_db, empty_target, tmp_path):
    """导出的包要能直接当 merge 的源（端到端）。"""

    out = tmp_path / "pack.db"
    mig.run_export(source_db, out)
    mig.run_merge(out, empty_target, apply=True)
    counts = _counts(empty_target)
    assert counts["transfers"] == 3
    assert counts["watch_state"] == 2


# -- 安全性 -----------------------------------------------------------------
def test_machine_bound_tables_are_not_migrated():
    """额度统计 / 授权相关不进迁移范围，换机器本来就该重来。"""

    assert "api_requests" in mig.SKIPPED
    assert "api_requests" not in mig.TABLES
    assert "license" not in " ".join(mig.TABLES).lower()


def test_same_file_rejected(source_db):
    with pytest.raises(SystemExit):
        mig.run_merge(source_db, source_db, apply=False)
