"""启动恢复：处理上次进程被强杀留下的孤儿扫描。

背景（2026-11 实际故障）：服务被 taskkill 后重启，上一进程的 ScanJob 还是
`running`、ScanLock 租约也还在，新进程的手动扫描一直返回 409
（"a scan job is already running"），最长要等租约过期（8 分钟），
用户看到的就是"扫描坏了、连续几次都失败"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import Database, ScanJob, ScanLock


RPC_URL = "wss://entrypoint-finney.opentensor.ai:443"


def _seed_orphan(database: Database, *, status: str = "running") -> tuple[int, datetime]:
    lease = datetime.now(timezone.utc) + timedelta(minutes=8)
    with database.session() as db:
        job = ScanJob(rpc_url=RPC_URL, status=status, start_block=100)
        db.add(job)
        db.flush()
        job_id = job.id
        db.add(ScanLock(lock_name="chain-scan", owner_job=job_id, lease_until=lease))
    return job_id, lease


def test_restart_marks_orphan_running_scan_as_interrupted():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "recover.db")
        try:
            database.initialize()
            job_id, _ = _seed_orphan(database)

            # 模拟进程重启：initialize 再来一次
            database.initialize()

            with database.session() as db:
                job = db.get(ScanJob, job_id)
                assert job.status == "failed"
                assert "重启" in job.error
                assert job.finished_at is not None
        finally:
            database.engine.dispose()


def test_restart_releases_scan_lease_immediately():
    """租约必须在启动时释放，否则新扫描要白等 8 分钟。"""

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "recover.db")
        try:
            database.initialize()
            _seed_orphan(database)

            database.initialize()

            with database.session() as db:
                lock = db.get(ScanLock, "chain-scan")
                assert lock is not None
                assert lock.owner_job is None
                # SQLite 不保留时区，读回来是 naive，按 UTC 补上再比。
                lease = lock.lease_until.replace(tzinfo=timezone.utc)
                assert lease <= datetime.now(timezone.utc) + timedelta(seconds=2)
        finally:
            database.engine.dispose()


def test_completed_scan_is_left_untouched():
    """已经完成的任务不能被改写状态。"""

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "recover.db")
        try:
            database.initialize()
            job_id, _ = _seed_orphan(database, status="complete")

            changed = database.recover_interrupted_scans()

            assert changed == 0
            with database.session() as db:
                assert db.get(ScanJob, job_id).status == "complete"
        finally:
            database.engine.dispose()


def test_recover_is_idempotent():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "recover.db")
        try:
            database.initialize()
            _seed_orphan(database)

            assert database.recover_interrupted_scans() == 1
            assert database.recover_interrupted_scans() == 0
        finally:
            database.engine.dispose()
