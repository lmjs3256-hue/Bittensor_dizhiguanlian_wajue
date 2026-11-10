"""跨机迁移「运行时数据」：本地索引、查询记录、巡检日志。

**为什么需要这个工具**

地址库可以导出 JSON 再导入（`/api/library/export` → `/api/library/import`），
但那个 JSON **只含地址库**。以下这些都不在里面，换机器就丢：

| 表 | 内容 | 丢了会怎样 |
| --- | --- | --- |
| `transfers` | 本地索引（链上转账明细） | 要从当前链头重扫，攒回几万块要几小时 |
| `relation_archives` | 历史查询快照 | 历史页空白，没法回看/重导出 |
| `auto_import_log` | 巡检自动入库流水 | 入库日志空白 |
| `watch_state` | 巡检游标（查到哪个块） | 巡检从头开始，已查过的要重查 |
| `watch_candidates` / `book_pair_stats` | 巡检候选累计、库配对统计 | 自动入库的累计金额归零 |
| `watch_runs` | 巡检轮次统计 | 进度窗口从 0 开始 |

这些都在同一个 SQLite 文件里，所以"拷库"是最省事的。但**如果远端已经有数据了**
（比如你已经导入过地址库、也跑过几轮巡检），直接覆盖会把远端的数据冲掉。
所以本工具做的是**幂等合并**，而不是覆盖。

**设计要点**

1. **只读源库**，绝不写源库（`mode=ro` 打开）。
2. **目标按唯一键去重**，重复的跳过：
   - `transfers` → `event_key`（源端也保证唯一）+ `(block_number, event_index)`
   - `watch_state` → `address`（主键）
   - `book_pair_stats` → `(book_a, book_b)`
   - `watch_candidates` → `(address, book)`
   - `relation_archives` / `auto_import_log` / `watch_runs` → 按业务内容判重，不靠 id
3. **不搬迁与机器绑定的东西**：`settings.json`（含 DPAPI 加密的 key）、`license.key`、
   `api_requests`（本机额度统计）。这些换机器后本来就该重来。
4. **默认只预览**，`--apply` 才写。

**用法**

```bash
# 1) 在本机导出一个迁移包（只读源库，安全）
python tools/migrate_runtime.py export --out D:\\migrate-20261110.db

# 2) 把 migrate-*.db 传到远端，然后合并进远端的库
python tools/migrate_runtime.py merge --from migrate-20261110.db --target data/wallet_link.db

# 3) 确认统计无误后再真正写入
python tools/migrate_runtime.py merge --from migrate-20261110.db --target data/wallet_link.db --apply
```

也可以直接拿**完整库文件**当迁移源（`--from data/wallet_link.db`），工具只读需要的表。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 需要迁移的表 → 去重键说明（仅用于日志展示）
TABLES = (
    "transfers",
    "relation_archives",
    "auto_import_log",
    "watch_state",
    "watch_candidates",
    "book_pair_stats",
    "watch_runs",
)

# 明确不迁移的表（换机器应该重来 / 与机器绑定）
SKIPPED = {
    "api_requests": "本机额度统计，换机器后应重新计数",
    "scan_jobs": "扫描任务历史，目标机器会重新记录",
    "chain_cursors": "本地索引入口，目标机器自己维护",
    "import_batch": "撤回快照，只对产生它的那台机器有意义",
}


@dataclass
class TableReport:
    name: str
    source_rows: int = 0
    inserted: int = 0
    skipped: int = 0
    note: str = ""


@dataclass
class Report:
    tables: list[TableReport] = field(default_factory=list)
    applied: bool = False

    def add(self, report: TableReport) -> None:
        self.tables.append(report)

    def render(self) -> str:
        lines = []
        lines.append(f"{'表':<20}{'源':>10}{'已存在跳过':>12}{'将写入':>10}  说明")
        lines.append("-" * 78)
        for item in self.tables:
            lines.append(
                f"{item.name:<20}{item.source_rows:>10}{item.skipped:>12}"
                f"{item.inserted:>10}  {item.note}"
            )
        lines.append("-" * 78)
        total_in = sum(t.inserted for t in self.tables)
        total_skip = sum(t.skipped for t in self.tables)
        verb = "已写入" if self.applied else "将写入"
        lines.append(f"合计：{verb} {total_in} 行，跳过 {total_skip} 行（目标已存在）")
        if not self.applied:
            lines.append("")
            lines.append("这是预览。确认无误后加 --apply 真正写入。")
        return "\n".join(lines)


def _open_ro(path: Path) -> sqlite3.Connection:
    """只读打开源库，避免误写。"""

    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"pragma table_info({table})")]


def _dest_columns(target: sqlite3.Connection, table: str, source_cols: list[str]) -> list[str]:
    """目标表要写的列：**排除自增主键 id**。

    源库和目标库的 id 各自独立自增。直接搬 id 会撞主键——实测就是
    ``UNIQUE constraint failed: transfers.id``。这些表里有业务唯一键
    （transfers.event_key、watch_state.address、book_pair_stats 的库对等）
    负责去重，id 交给目标库自己分配即可，无业务影响。

    ``watch_state`` 的 address 就是主键、``chain_cursors`` 用 rpc_url，
    这些属于业务键不算自增 id，照常迁移。
    """

    ids = {
        row[1]
        for row in target.execute(f"pragma table_info({table})")
        if row[5]  # pk 标志
    }
    # 只有当主键是自增 id 这一列时才排除；watch_state 的 address 虽然也是
    # 主键，但它是业务键（地址），必须带过去。
    auto_like = {"id"}
    return [
        col
        for col in _columns(target, table)
        if col in source_cols and not (col in ids and col in auto_like)
    ]


def _insert_rows(
    target: sqlite3.Connection,
    table: str,
    rows: list[sqlite3.Row],
    dest_cols: list[str],
    *,
    apply: bool,
) -> int:
    """按目标表实际列名插入，返回插入行数。"""

    if not rows or not apply:
        return len(rows)
    cols = ", ".join(dest_cols)
    marks = ", ".join("?" for _ in dest_cols)
    sql = f"insert into {table} ({cols}) values ({marks})"
    target.executemany(sql, [[row[col] for col in dest_cols] for row in rows])
    return len(rows)


def _select_in_chunks(
    source: sqlite3.Connection, table: str, keys: list[str], batch: int = 400
):
    """分块查源表，避免 IN (...) 参数过多。"""

    total = source.execute(f"select count(*) from {table}").fetchone()[0]
    offset = 0
    while offset < total:
        rows = source.execute(
            f"select * from {table} order by rowid limit ? offset ?", (batch, offset)
        ).fetchall()
        if not rows:
            break
        yield rows
        offset += len(rows)


def migrate_transfers(source, target, *, apply: bool) -> TableReport:
    report = TableReport("transfers", note="本地索引：按 event_key 去重")
    dest_cols = _dest_columns(target, "transfers", _columns(source, "transfers"))
    existing_keys = {
        row[0]
        for row in target.execute("select event_key from transfers")
        if row[0]
    }
    existing_slots = {
        (row[0], row[1])
        for row in target.execute("select block_number, event_index from transfers")
    }

    for chunk in _select_in_chunks(source, "transfers", ["event_key"]):
        batch: list[sqlite3.Row] = []
        for row in chunk:
            report.source_rows += 1
            key = row["event_key"]
            slot = (row["block_number"], row["event_index"])
            if key in existing_keys or slot in existing_slots:
                report.skipped += 1
                continue
            existing_keys.add(key)
            existing_slots.add(slot)
            batch.append(row)
        report.inserted += _insert_rows(
            target, "transfers", batch, dest_cols, apply=apply
        )
    return report


def migrate_watch_state(source, target, *, apply: bool) -> TableReport:
    report = TableReport("watch_state", note="巡检游标：按 address 去重")
    dest_cols = _dest_columns(target, "watch_state", _columns(source, "watch_state"))
    existing = {row[0] for row in target.execute("select address from watch_state")}
    for chunk in _select_in_chunks(source, "watch_state", ["address"]):
        batch = []
        for row in chunk:
            report.source_rows += 1
            if row["address"] in existing:
                report.skipped += 1
                continue
            existing.add(row["address"])
            batch.append(row)
        report.inserted += _insert_rows(
            target, "watch_state", batch, dest_cols, apply=apply
        )
    return report


def migrate_keyed(source, target, *, apply: bool, table: str, key_cols: list[str], note: str) -> TableReport:
    """通用：按一组列做唯一性判重。"""

    report = TableReport(table, note=note)
    src_cols = _columns(source, table)
    dest_cols = _dest_columns(target, table, src_cols)
    key_expr = ", ".join(key_cols)
    existing = {
        tuple(row) for row in target.execute(f"select {key_expr} from {table}")
    }
    for chunk in _select_in_chunks(source, table, key_cols):
        batch = []
        for row in chunk:
            report.source_rows += 1
            key = tuple(row[col] for col in key_cols)
            if key in existing:
                report.skipped += 1
                continue
            existing.add(key)
            batch.append(row)
        report.inserted += _insert_rows(target, table, batch, dest_cols, apply=apply)
    return report


def migrate_archives(source, target, *, apply: bool) -> TableReport:
    """历史查询快照：按 (job_id, created_at) 判重，不靠自增 id。"""

    report = TableReport("relation_archives", note="查询记录：按 job_id+时间 去重")
    src_cols = _columns(source, "relation_archives")
    dest_cols = _dest_columns(target, "relation_archives", src_cols)
    existing = {
        (row[0], row[1])
        for row in target.execute("select job_id, created_at from relation_archives")
    }
    for chunk in _select_in_chunks(source, "relation_archives", ["job_id"]):
        batch = []
        for row in chunk:
            report.source_rows += 1
            key = (row["job_id"], row["created_at"])
            if key in existing:
                report.skipped += 1
                continue
            existing.add(key)
            batch.append(row)
        report.inserted += _insert_rows(
            target, "relation_archives", batch, dest_cols, apply=apply
        )
    return report


def migrate_auto_imports(source, target, *, apply: bool) -> TableReport:
    """自动入库流水：按 (address, book, created_at) 判重。"""

    report = TableReport("auto_import_log", note="入库日志：按地址+库+时间去重")
    dest_cols = _dest_columns(target, "auto_import_log", _columns(source, "auto_import_log"))
    existing = {
        (row[0], row[1], row[2])
        for row in target.execute(
            "select address, book, created_at from auto_import_log"
        )
    }
    for chunk in _select_in_chunks(source, "auto_import_log", ["address"]):
        batch = []
        for row in chunk:
            report.source_rows += 1
            key = (row["address"], row["book"], row["created_at"])
            if key in existing:
                report.skipped += 1
                continue
            existing.add(key)
            batch.append(row)
        report.inserted += _insert_rows(
            target, "auto_import_log", batch, dest_cols, apply=apply
        )
    return report


def migrate_watch_runs(source, target, *, apply: bool) -> TableReport:
    """巡检轮次：按 (started_at, manual) 判重。"""

    report = TableReport("watch_runs", note="巡检轮次：按开始时间去重")
    dest_cols = _dest_columns(target, "watch_runs", _columns(source, "watch_runs"))
    existing = {
        (row[0], row[1])
        for row in target.execute("select started_at, manual from watch_runs")
    }
    for chunk in _select_in_chunks(source, "watch_runs", ["started_at"]):
        batch = []
        for row in chunk:
            report.source_rows += 1
            key = (row["started_at"], row["manual"])
            if key in existing:
                report.skipped += 1
                continue
            existing.add(key)
            batch.append(row)
        report.inserted += _insert_rows(
            target, "watch_runs", batch, dest_cols, apply=apply
        )
    return report


def run_merge(source_path: Path, target_path: Path, *, apply: bool) -> Report:
    if not source_path.exists():
        raise SystemExit(f"源文件不存在：{source_path}")
    if not target_path.exists():
        raise SystemExit(f"目标库不存在：{target_path}（先在目标机启动一次服务，让它建库）")
    if source_path.resolve() == target_path.resolve():
        raise SystemExit("源和目标是同一个文件，不需要迁移")

    source = _open_ro(source_path)
    target = sqlite3.connect(target_path)
    target.row_factory = sqlite3.Row
    report = Report(applied=apply)
    try:
        target.execute("begin")
        for table in TABLES:
            if not _table_exists(source, table):
                continue
            if not _table_exists(target, table):
                continue
            if table == "transfers":
                report.add(migrate_transfers(source, target, apply=apply))
            elif table == "watch_state":
                report.add(migrate_watch_state(source, target, apply=apply))
            elif table == "relation_archives":
                report.add(migrate_archives(source, target, apply=apply))
            elif table == "auto_import_log":
                report.add(migrate_auto_imports(source, target, apply=apply))
            elif table == "watch_runs":
                report.add(migrate_watch_runs(source, target, apply=apply))
            elif table == "watch_candidates":
                report.add(
                    migrate_keyed(
                        source, target, apply=apply, table=table,
                        key_cols=["address", "book"],
                        note="巡检候选：按地址+库去重",
                    )
                )
            elif table == "book_pair_stats":
                report.add(
                    migrate_keyed(
                        source, target, apply=apply, table=table,
                        key_cols=["book_a", "book_b"],
                        note="库配对统计：按库对去重",
                    )
                )
        if apply:
            target.commit()
        else:
            target.rollback()
    finally:
        source.close()
        target.close()
    return report


def run_export(source_path: Path, out_path: Path, tables: list[str] | None = None) -> str:
    """导出一个只含运行时数据的精简库，体积远小于完整库。

    ``tables`` 可只导其中几张（例如只想要历史查询、不搬几万条索引）。
    """

    if not source_path.exists():
        raise SystemExit(f"源库不存在：{source_path}")
    if out_path.exists():
        raise SystemExit(f"输出文件已存在，请换名字或先删除：{out_path}")

    wanted = [t for t in (tables or TABLES) if t in TABLES]
    if not wanted:
        raise SystemExit("没有可导出的表")

    source = _open_ro(source_path)
    out = sqlite3.connect(out_path)
    try:
        # 导出端不用 WAL，便于直接拷走单文件
        out.execute("pragma journal_mode=delete")
        out.execute("pragma synchronous=off")
        for table in wanted:
            if not _table_exists(source, table):
                continue
            ddl = source.execute(
                "select sql from sqlite_master where type='table' and name=?", (table,)
            ).fetchone()[0]
            out.execute(ddl)
            cursor = source.execute(f"select * from {table}")
            cols = [d[0] for d in cursor.description]
            marks = ", ".join("?" for _ in cols)
            batch: list[list] = []
            for row in cursor:
                batch.append(list(row))
                if len(batch) >= 2000:
                    out.executemany(
                        f"insert into {table} ({', '.join(cols)}) values ({marks})", batch
                    )
                    batch.clear()
            if batch:
                out.executemany(
                    f"insert into {table} ({', '.join(cols)}) values ({marks})", batch
                )
            # 不重建索引：合并时是顺序扫描本包、去重靠目标库的索引，
            # 包内索引用不上，却会让文件大 40%（实测 62.5MB vs 88.7MB）。
        out.commit()
        # VACUUM 去掉删除/回滚留下的空洞，通常能小一大截
        out.execute("vacuum")
        counts = {
            table: out.execute(f"select count(*) from {table}").fetchone()[0]
            for table in wanted
            if _table_exists(out, table)
        }
    finally:
        source.close()
        out.close()

    size_mb = out_path.stat().st_size / 1024 / 1024
    lines = [f"已导出：{out_path}（{size_mb:.1f} MB）", ""]
    for table, count in counts.items():
        lines.append(f"  {table:<22}{count:>8}")
    lines.append("")
    lines.append("这个文件只含运行时数据（不含地址库、key、授权），可以安全传输。")
    lines.append("在目标机上执行：")
    lines.append(
        f"  python tools/migrate_runtime.py merge --from {out_path.name} "
        f"--target data/wallet_link.db"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="迁移本地索引 / 查询记录 / 巡检日志到另一台机器（幂等合并，默认只预览）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="从本机库导出运行时数据包")
    exp.add_argument("--source", default="data/wallet_link.db", help="本机库路径")
    exp.add_argument("--out", required=True, help="输出文件名，如 migrate-20261110.db")
    exp.add_argument(
        "--only",
        default="",
        help=(
            "只导出指定表，逗号分隔。可选："
            + ", ".join(TABLES)
            + "。例如只搬查询记录和日志：--only relation_archives,auto_import_log,watch_state,watch_runs"
        ),
    )

    mrg = sub.add_parser("merge", help="把数据包合并进目标库（默认预览）")
    mrg.add_argument("--from", dest="source", required=True, help="数据包或完整库路径")
    mrg.add_argument("--target", default="data/wallet_link.db", help="目标库路径")
    mrg.add_argument("--apply", action="store_true", help="真正写入（不加则只预览）")

    args = parser.parse_args(argv)
    if args.command == "export":
        only = [name.strip() for name in (args.only or "").split(",") if name.strip()]
        print(run_export(Path(args.source), Path(args.out), only or None))
        return 0

    report = run_merge(Path(args.source), Path(args.target), apply=args.apply)
    print(report.render())
    if args.apply:
        print()
        print("迁移完成。请重启服务，然后核对：")
        print("  curl -s http://127.0.0.1:8766/api/overview   # 看 transfer_count / lag_blocks")
        print("  curl -s http://127.0.0.1:8766/api/watch/status")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
