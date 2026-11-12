"""多人同时查询：按发起先后排队，一个跑完下一个才开始。

**用户要求（原话）**：「多个人同时发起地址扫描查询任务，会出现抢额度，甚至
等待太久。能否按触发扫描的先后顺序安排工单，工单结束返回结果，每个人只能
单独看自己的查询记录，但每个人对地址库的维护都会同步到主机，同一个地址不同
人操作标记不一样，以最后标记的为主。」

原行为：`due()` 返回**所有**到点的作业，调度器逐个 `run_round`。两人同时
查询时，两个作业各拿一半预算、都变慢，用户看到的就是"抢额度、等太久"。

现行为：

- `create()` 给每个作业分配递增的 `queue_no`，状态先置 `queued`；
- `due()` **只返回队首那一个**；
- `run_round()` 里再守一道：不是队首直接返回（防止别处误调）；
- `_finish()` 收尾后主动 `_promote_next()`，后面的人不用多等一个 tick。

**注意**：队列只管"谁先跑"，不管"能不能查到"。地址库是全机共享的，
谁的标记后到就覆盖谁——这是用户明确要的（"以最后标记的为主"）。
"""

from __future__ import annotations

import pytest

from app.db import Database
from app.jobs import RelationJobManager
from app.service import LinkService

SEED_A = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
SEED_B = "5CF6Ruq6u7DoF6TYxyaFzEuviWi7tjvJtz19XgPS2b7XDCFM"
SEED_C = "5HCT4AarReToT1BKyLtJXJfSLs4zRS7dENnZ7iysqrqxXyV7"


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "queue.db")
    db.initialize()
    svc = LinkService(db, "wss://example.invalid")
    mgr = RelationJobManager(round_interval=60, max_rounds=5, max_addresses=50)
    yield mgr, svc
    db.engine.dispose()


def _stall_rounds(mgr, counter):
    """把"跑一轮"替换成"记一笔就停在 waiting"，方便观察排队行为。"""

    def patched(job):
        counter.append(job.id)
        job.rounds_done += 1
        job.total_requests += 1
        job.status = "waiting"
        job.next_round_at = 1e18  # 永远不到点，模拟"在等下一轮"

    mgr._run_round_locked = patched


def _create(mgr, svc, seed, owner):
    return mgr.create(
        svc,
        seed=seed,
        depth=1,
        min_edge_tao="0",
        min_total_tao="0",
        owner=owner,
    )


# -- 核心：同一时刻只有一个在推进 -------------------------------------------
def test_only_head_job_runs(env):
    mgr, svc = env
    rounds = []
    _stall_rounds(mgr, rounds)

    first = _create(mgr, svc, SEED_A, "张三")
    second = _create(mgr, svc, SEED_B, "李四")
    third = _create(mgr, svc, SEED_C, "王五")

    assert first.status == "waiting", "第一个应该跑过一轮"
    assert second.status == "queued", f"第二个应排队，实际 {second.status}"
    assert third.status == "queued"
    assert second.rounds_done == 0
    assert third.rounds_done == 0
    # 被推进过的只有队首那一个（它可能因状态变化被多次尝试，但绝不是别人）
    assert set(rounds) == {first.id}, f"有非队首工单被推进：{rounds}"


def test_run_round_refuses_non_head(env):
    """就算有别的代码直接调 run_round，也不能越过队首。"""

    mgr, svc = env
    rounds = []
    _stall_rounds(mgr, rounds)

    first = _create(mgr, svc, SEED_A, "张三")
    second = _create(mgr, svc, SEED_B, "李四")

    mgr.run_round(second)  # 故意越过队首
    assert second.rounds_done == 0, "越过队首被允许了，队列形同虚设"
    assert first.id in rounds


def test_finish_promotes_next(env):
    """前一个收尾后，后一个立刻接上，不用等调度 tick。"""

    mgr, svc = env
    _stall_rounds(mgr, [])

    first = _create(mgr, svc, SEED_A, "张三")
    second = _create(mgr, svc, SEED_B, "李四")
    assert second.status == "queued"

    mgr._finish(first, status="complete")

    assert second.rounds_done == 1, "前一个收尾后第二个没接上"
    assert second.status == "waiting"


def test_queue_order_follows_arrival(env):
    """队列严格按发起先后，不按 seed、不按数据量。"""

    mgr, svc = env
    _stall_rounds(mgr, [])

    jobs = [
        _create(mgr, svc, SEED_C, "王五"),  # 先发起
        _create(mgr, svc, SEED_A, "张三"),
        _create(mgr, svc, SEED_B, "李四"),
    ]
    overview = mgr.queue_overview()
    assert [item["owner"] for item in overview["items"]] == ["王五", "张三", "李四"]
    assert [job.queue_no for job in jobs] == [1, 2, 3]


# -- 队列总览 ---------------------------------------------------------------
def test_queue_overview_marks_mine(env):
    mgr, svc = env
    _stall_rounds(mgr, [])

    _create(mgr, svc, SEED_A, "张三")
    _create(mgr, svc, SEED_B, "李四")

    overview = mgr.queue_overview("李四")
    assert overview["total"] == 2
    assert overview["running"]["owner"] == "张三"
    assert overview["mine"]["owner"] == "李四"
    assert overview["mine"]["position"] == 2, "李四排第二"
    # 张三那条不是我的
    assert overview["items"][0]["mine"] is False


def test_queue_position_is_one_based(env):
    mgr, svc = env
    _stall_rounds(mgr, [])

    first = _create(mgr, svc, SEED_A, "张三")
    second = _create(mgr, svc, SEED_B, "李四")

    # 已开跑的不算"排队"，返回 0
    assert mgr.queue_position(first) == 0
    assert mgr.queue_position(second) == 1


def test_payload_reports_queue_fields(env):
    """前端要靠 payload 里的 position / wait 显示"我排第几、还要多久"。"""

    mgr, svc = env
    _stall_rounds(mgr, [])

    _create(mgr, svc, SEED_A, "张三")
    second = _create(mgr, svc, SEED_B, "李四")

    payload = mgr.payload(second)
    assert payload["status"] == "queued"
    assert payload["queue_position"] == 1
    assert payload["owner"] == "李四"
    assert payload["queue_wait_seconds"] > 0, "要给出等待时长估计"
    assert payload["queue_no"] == 2


def test_owner_recorded_on_job(env):
    mgr, svc = env
    _stall_rounds(mgr, [])
    job = _create(mgr, svc, SEED_A, "  张三  ")
    assert job.owner == "张三", "owner 要去掉首尾空格"


def test_blank_owner_is_allowed(env):
    """单人使用 / 老客户端不带名字时，owner 为空，行为与以前一致。"""

    mgr, svc = env
    _stall_rounds(mgr, [])
    job = _create(mgr, svc, SEED_A, "")
    assert job.owner == ""
    assert mgr.payload(job)["owner"] == ""


# -- 收尾时接力的真实路径（踩过的坑）----------------------------------------
def test_promote_next_survives_held_run_gate(env):
    """前一个工单在 run_round 内部收尾时，后一个必须真的跑起来。

    这是线上实测踩到的死锁：``_finish()`` 是在 ``run_round()`` 里面被调的，
    此时线程已经持有 ``_run_gate``（普通 Lock，不可重入）。收尾后
    ``_promote_next()`` → ``_start_job()`` 先把下一个标成 ``running``、
    写 ``started_at``，然后调 ``run_round()`` —— 却拿不到 ``_run_gate``
    直接 return。结果下一个工单留下**假的 running 状态、rounds_done 仍是 0**，
    而 ``due()`` 只捡 ``queued``/``waiting``，于是它永远卡死：界面上一直
    "正在查询"，其实一次都没跑过，30 分钟保留期到就被 cleanup 悄悄清掉，
    查询记录里也没有它。

    直接从外部调 ``_finish()`` 测不出来（那时没持锁），必须走
    ``run_round()`` 这条真实路径。
    """

    mgr, svc = env
    ran: list[str] = []

    # defer_start=True：否则 create() 会同步把第一轮真跑掉（depth=1 数据量小，
    # 往往直接跑成 complete），等接管 _run_round_locked 时已经没有工单可测了。
    first = mgr.create(
        svc, seed=SEED_A, depth=1, min_edge_tao="0", min_total_tao="0",
        owner="张三", defer_start=True,
    )
    second = mgr.create(
        svc, seed=SEED_B, depth=1, min_edge_tao="0", min_total_tao="0",
        owner="李四", defer_start=True,
    )

    def patched(job):
        ran.append(job.id)
        job.rounds_done += 1
        job.total_requests += 1
        if job.id == first.id:
            # 第一个工单跑完一轮就没活了 → 走 _finish 收尾 → 接力下一个
            mgr._finish(job, status="complete")
            return
        job.status = "waiting"
        job.next_round_at = 1e18

    mgr._run_round_locked = patched

    # 走真实入口：内部会持有 _run_gate，再在 _finish 里接力
    mgr.run_round(first)

    assert first.is_finished
    assert second.id in ran, "前一个收尾后，第二个没有真正被推进（卡在假 running）"
    assert second.rounds_done >= 1, "第二个工单 rounds_done 仍为 0，说明只是被标了状态"
    # 关键：不能留下"running 但没跑"的假活口
    assert not (second.status == "running" and second.rounds_done == 0)
