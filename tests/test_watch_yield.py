"""巡检给手动查询让路、以及「限速不能把地址标记成已巡检」的行为验证。

让路的判定只看两件事：任务有没有收尾、收尾后静默够不够。轮间 62 秒的
``waiting`` 属于未收尾，所以不会被误判成空闲。
"""

from types import SimpleNamespace

import pytest

import app.taostats as taostats
import app.watch as watch_module
from app.db import AddressLibrary, Database, WatchState
from app.jobs import (
    MANUAL_QUIET_SECONDS,
    MANUAL_STALL_SECONDS,
    RelationJobManager,
)
from app.settings_store import DEFAULT_WATCH_CONFIG
from app.taostats import TaostatsRateLimitError, TransferPage
from app.watch import WatchService


LIB_ADDRESS = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
PARTNER = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"


class FakeJob:
    """只实现 manual_busy 用到的字段，避免为调度判定去建真作业。"""

    def __init__(
        self,
        job_id: str,
        *,
        status: str = "running",
        created_at: float = 1000.0,
        finished_at: float = 0.0,
        rounds: int = 1,
        pending: int = 3,
        requests: int = 5,
    ) -> None:
        self.id = job_id
        self.status = status
        self.created_at = created_at
        self.finished_at = finished_at
        self.rounds_done = rounds
        self.total_requests = requests
        self.progress_at = 0.0
        self.progress_key: tuple = ()
        self.state = SimpleNamespace(rounds=[1] * rounds, pending_total=pending)

    @property
    def is_finished(self) -> bool:
        return self.status in {"complete", "cancelled", "failed"}


def manager_with(*jobs: FakeJob) -> RelationJobManager:
    manager = RelationJobManager()
    for job in jobs:
        manager._jobs[job.id] = job
    return manager


# -- 让路判定 ---------------------------------------------------------------


def test_idle_when_no_jobs():
    busy = manager_with().manual_busy(now=1000.0)

    assert busy["paused"] is False
    assert busy["reason"] == ""


def test_running_job_pauses_watch():
    busy = manager_with(FakeJob("a")).manual_busy(now=1000.0)

    assert busy["paused"] is True
    assert busy["reason"] == "running"
    assert busy["job_id"] == "a"


def test_round_interval_waiting_still_pauses():
    """轮间 62 秒等待（status=waiting）绝不能被当成空闲，否则会抢走配额。"""

    busy = manager_with(FakeJob("a", status="waiting")).manual_busy(now=1000.0)

    assert busy["paused"] is True
    assert busy["reason"] == "running"


def test_finalizing_pauses():
    busy = manager_with(FakeJob("a", status="finalizing")).manual_busy(now=1000.0)

    assert busy["paused"] is True


def test_cooldown_after_finish():
    manager = manager_with(FakeJob("a", status="complete", finished_at=1000.0))

    busy = manager.manual_busy(now=1000.0 + MANUAL_QUIET_SECONDS - 30)

    assert busy["paused"] is True
    assert busy["reason"] == "cooldown"
    assert busy["resume_in_seconds"] == 30


def test_resume_after_quiet_window():
    manager = manager_with(FakeJob("a", status="complete", finished_at=1000.0))

    busy = manager.manual_busy(now=1000.0 + MANUAL_QUIET_SECONDS + 1)

    assert busy["paused"] is False


def test_cooldown_counts_from_latest_finish():
    """连点两次查询：冷却从最后结束的那一次算起，不是最早那次。"""

    manager = manager_with(
        FakeJob("old", status="complete", finished_at=1000.0),
        FakeJob("new", status="failed", finished_at=1060.0),
    )

    busy = manager.manual_busy(now=1070.0)

    assert busy["job_id"] == "new"
    assert busy["resume_in_seconds"] == 110


def test_stalled_job_lets_watch_resume():
    job = FakeJob("a", created_at=1000.0)
    manager = manager_with(job)
    # 首次观察一定会记下进度签名，不会立刻判僵死。
    assert manager.manual_busy(now=1000.0)["paused"] is True

    busy = manager.manual_busy(now=1000.0 + MANUAL_STALL_SECONDS + 1)

    assert busy["paused"] is False
    assert busy["reason"] == "stalled"
    assert busy["stalled_jobs"] == 1


def test_progressing_job_is_not_stalled():
    job = FakeJob("a", created_at=1000.0)
    manager = manager_with(job)
    manager.manual_busy(now=1000.0)

    job.rounds_done = 2
    job.state = SimpleNamespace(rounds=[1, 1], pending_total=1)
    busy = manager.manual_busy(now=1000.0 + MANUAL_STALL_SECONDS + 1)

    assert busy["paused"] is True


# -- 限速不能吞地址 ---------------------------------------------------------


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "watch_yield.db")
    db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture()
def config():
    values = dict(DEFAULT_WATCH_CONFIG)
    values.update(
        {
            "threshold_tao": 30.0,
            "include_stake": False,
            "daily_budget": 10_000,
            "auto_import": True,
            "follow_auto_imported": False,
        }
    )
    return values


def _build(db: Database, address: str) -> None:
    with db.session() as session:
        session.add(
            AddressLibrary(
                address=address,
                label="",
                category="其他",
                note="",
                source="manual",
                book="6NCK",
                categories="[]",
            )
        )


def _rate_limited(**kwargs):
    raise TaostatsRateLimitError("5 次/分钟")


def test_rate_limit_does_not_mark_address_checked(database, monkeypatch, config):
    _build(database, LIB_ADDRESS)
    monkeypatch.setattr(taostats, "is_configured", lambda: True)
    monkeypatch.setattr(watch_module, "get_transfer_page_query", _rate_limited)

    result = WatchService(database)._check_address(LIB_ADDRESS, config)

    assert result["rate_limited"] is True
    with database.session() as db:
        state = db.get(WatchState, LIB_ADDRESS)
        assert state is not None
        # 关键：游标原地不动，否则这个地址要等下一整轮（约 2 天）才回来。
        assert state.last_checked_at is None
        assert int(state.checked_count or 0) == 0
        assert "稍后重试" in (state.last_error or "")


def test_rate_limited_address_is_retried(database, monkeypatch, config):
    """限速跳过之后，下一次要能把同一个地址补齐——地址没丢、金额不重复累加。"""

    _build(database, LIB_ADDRESS)
    monkeypatch.setattr(taostats, "is_configured", lambda: True)
    service = WatchService(database)
    monkeypatch.setattr(watch_module, "get_transfer_page_query", _rate_limited)

    assert service._check_address(LIB_ADDRESS, config)["rate_limited"] is True

    rows = [
        {
            "id": "0x1",
            "block_number": 900,
            "from": LIB_ADDRESS,
            "to": PARTNER,
            "amount": str(45 * 1_000_000_000),
            "transaction_hash": "0x1",
        }
    ]
    monkeypatch.setattr(
        watch_module,
        "get_transfer_page_query",
        lambda **kwargs: TransferPage(rows, None),
    )

    second = service._check_address(LIB_ADDRESS, config)

    assert second["rate_limited"] is False
    with database.session() as db:
        state = db.get(WatchState, LIB_ADDRESS)
        assert state.last_checked_at is not None
        assert int(state.checked_count or 0) == 1
        assert (state.last_error or "") == ""
        assert int(state.last_block or 0) == 900
