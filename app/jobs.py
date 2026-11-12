"""分轮关联查询作业。

Taostats 每分钟只允许 5 次请求，一次查询通常查不完多层关联。作业把爬取
状态留在进程内存，间隔一段时间推进一轮，直到队列清空，再统一合成最终
结果；期间前端轮询进度即可。
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .crawl import KIND_LABELS, TaostatsCrawl
from .service import LinkService, RelationContext, taostats_request_budget


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_ROUND_INTERVAL_SECONDS = _env_int("BITTENSOR_JOB_INTERVAL_SECONDS", 62)
DEFAULT_MAX_ROUNDS = _env_int("BITTENSOR_JOB_MAX_ROUNDS", 12)
DEFAULT_MAX_ADDRESSES = _env_int("BITTENSOR_JOB_MAX_ADDRESSES", 150)
# 每多一层，地址数量大致按扇出增长。默认 150 是按 3 级估的护栏；
# 层级放到 4～10 级时如果还卡 150，往往第 4 层没走完就被截停，
# 用户会误以为是「没有更多关联」。所以按「层级 × 50」放大护栏，
# 并把实际生效的上限回传给前端显示。环境变量仍是硬覆盖的下限。
ADDRESSES_PER_DEPTH = _env_int("BITTENSOR_JOB_ADDRESSES_PER_DEPTH", 50)
JOB_RETENTION_SECONDS = 30 * 60



def address_cap_for_depth(depth: int, base: int = DEFAULT_MAX_ADDRESSES) -> int:
    """按层级放大的地址护栏：3 级仍是 150，10 级放宽到 500。"""

    try:
        hops = max(1, int(depth))
    except (TypeError, ValueError):
        hops = 3
    return max(base, hops * ADDRESSES_PER_DEPTH)


@dataclass
class RelationJob:
    id: str
    service: LinkService
    context: RelationContext
    crawl: TaostatsCrawl
    params: dict[str, Any]
    created_at: float
    round_interval: float
    max_rounds: int
    max_addresses: int
    status: str = "pending"
    rounds_done: int = 0
    total_requests: int = 0
    next_round_at: float = 0.0
    result: dict[str, Any] | None = None
    error: str = ""
    finished_at: float = 0.0
    archive_id: int | None = None
    # 扫描开始前为「起始地址画像」已花掉的请求数：只有第一轮要从额度里扣，
    # 后面每轮之间已经隔了 62 秒，不受同一分钟限速影响。
    prefetched_requests: int = 0
    # 连续「0 请求」轮次：被限流或没有可用数据时不再空转下去。
    empty_rounds: int = 0
    cancel_requested: bool = False
    finalized: bool = False
    stopped: bool = False
    # -- 多人共用一台主机时需要的归属信息 --------------------------------
    # owner 是「谁发起的」。现在只用来给查询记录打标，让每个人只看自己的
    # 历史；地址库是全机共享的（用户要求"维护都同步到主机"）。
    owner: str = ""
    # 排队序号：按发起先后递增，队列按它先进先出。
    queue_no: int = 0
    # 轮到这个工单开跑的时刻；0 表示还没轮到（排队中）。
    started_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self):
        return self.crawl.state

    @property
    def requests_last_round(self) -> int:
        return self.state.rounds[-1] if self.state.rounds else 0

    @property
    def is_finished(self) -> bool:
        return self.status in {"complete", "cancelled", "failed"}


class RelationJobManager:
    """进程内的作业表；服务是单进程运行的，不需要外部队列。"""

    def __init__(
        self,
        *,
        round_interval: int = DEFAULT_ROUND_INTERVAL_SECONDS,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_addresses: int = DEFAULT_MAX_ADDRESSES,
    ) -> None:
        self._jobs: dict[str, RelationJob] = {}
        self._lock = threading.Lock()
        self.round_interval = round_interval
        self.max_rounds = max_rounds
        self.max_addresses = max_addresses
        # 排队序号：多人同时点「扫描关联」时按发起先后排工单。
        # 只在 create() 里递增，进程重启归零（工单本身也在内存里，一致）。
        self._queue_seq = 0
        # 当前真正在推进的那个工单 id。只靠"比较 queue_no"在并发下不够：
        # create_async 起了后台线程，调度器也在轮询，两个线程可能同时判断
        # "我是队首"然后一起跑（实测出现过）。用一个显式占位标记来判断，
        # 并配一把锁，保证同一时刻只有一个能进来。
        self._running_id = ""
        self._run_gate = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------
    def create(
        self,
        service: LinkService,
        *,
        seed: str,
        depth: int,
        min_edge_tao: str,
        min_total_tao: str,
        include_incoming: bool = True,
        include_outgoing: bool = True,
        filter_exchanges: bool = False,
        filter_validators: bool = False,
        filter_systems: bool = False,
        filter_publics: bool = False,
        removed_addresses: list[str] | None = None,
        round_interval: int | None = None,
        max_rounds: int | None = None,
        prefetched_requests: int = 0,
        owner: str = "",
        defer_start: bool = False,
    ) -> RelationJob:
        context = service.prepare_relation_context(
            seed=seed,
            depth=depth,
            min_edge_tao=min_edge_tao,
            min_total_tao=min_total_tao,
            include_incoming=include_incoming,
            include_outgoing=include_outgoing,
            data_source="taostats",
            filter_exchanges=filter_exchanges,
            filter_validators=filter_validators,
            filter_systems=filter_systems,
            filter_publics=filter_publics,
            removed_addresses=removed_addresses or (),
            prefetched_requests=prefetched_requests,
        )
        job = RelationJob(
            id=uuid.uuid4().hex[:12],
            service=service,
            context=context,
            crawl=service.new_crawl(context),
            params={
                "seed": seed,
                "depth": depth,
                "min_edge_tao": min_edge_tao,
                "min_total_tao": min_total_tao,
                "include_incoming": include_incoming,
                "include_outgoing": include_outgoing,
                "filter_exchanges": filter_exchanges,
                "filter_validators": filter_validators,
                "filter_publics": filter_publics,
                "removed_addresses": sorted(set(removed_addresses or ())),
            },
            created_at=time.time(),
            round_interval=float(round_interval or self.round_interval),
            max_rounds=int(max_rounds or self.max_rounds),
            max_addresses=address_cap_for_depth(depth, self.max_addresses),
            prefetched_requests=max(0, int(prefetched_requests or 0)),
            owner=str(owner or "").strip(),
            # 先入队，不立刻开跑：多人同时点会互相抢额度（两个工单各吃
            # 一半预算，谁都要多等几轮）。改成按顺序一个一个跑。
            status="queued",
        )
        with self._lock:
            self._queue_seq += 1
            job.queue_no = self._queue_seq
            self._jobs[job.id] = job
        job.crawl.start()  # 先把起始地址排进队列，轮到它时有活可干
        # 只有队列里排第一的才开跑，其余等前面收尾。
        #
        # ``defer_start=True`` 时一律不在这里跑：调用方（HTTP 接口）需要立刻
        # 返回，首轮交给后台线程。原因是第一轮要真发 API 请求，十几秒甚至更久，
        # 同步跑会让接口卡住、前端点了按钮很久没反应。
        if not defer_start and self._next_queued() is job:
            self._start_job(job)
        return job

    def create_async(self, service: LinkService, **kwargs: Any) -> RelationJob:
        """建作业并**立刻返回**，首轮交给后台线程去跑。

        为什么要有这个：``create()`` 会同步跑第一轮，而第一轮要真发 HTTP
        请求（十几秒甚至更久）。于是 ``POST /api/relations/jobs`` 迟迟不返回，
        前端只能干等——用户看到的就是「点了按钮很久没反应」「分轮框延迟很久
        才出现」。多人同时发起时，后一个人的请求还可能直接超时。

        ``create_async`` 只做入队（很快），首轮交给后台线程；接口立刻返回
        job_id，前端马上就能把进度框画出来。队列保证同一时刻只有一个在跑。
        """

        job = self.create(service, defer_start=True, **kwargs)
        # 首轮放到后台：接口立刻返回 job_id，前端马上能画进度框。
        # 队列守卫在 run_round 里，所以这里即使立刻起线程，也不会越过队首。
        threading.Thread(
            target=self._start_job,
            args=(job,),
            name=f"job-start-{job.id}",
            daemon=True,
        ).start()
        return job

    def _next_queued(self) -> RelationJob | None:
        """取队首工单：所有未收尾工单里 queue_no 最小的那个。

        返回 None 表示队列空。注意「已经在跑」的也会被返回——由调用方
        判断，因为它需要知道"当前该跑谁"。
        """

        with self._lock:
            active = [
                job
                for job in self._jobs.values()
                if not job.is_finished and job.status in {"queued", "running", "waiting", "finalizing"}
            ]
        if not active:
            return None
        # 只看 queue_no：**谁排前面谁就是队首**，与它是否已经开跑无关。
        # 以前这里用 started_at 参与了排序判断，结果「已开跑」的会被跳过，
        # 第二个工单一 latched 上 started_at 就也自认为是队首，两家一起跑。
        return min(active, key=lambda job: (job.queue_no, job.created_at))

    def _start_job(self, job: RelationJob) -> None:
        """让一个排队中的工单开跑第一轮。

        顺序很关键：**先确认自己是队首，再标记 started_at**。
        反过来的话（先标记再跑），标记完它就不再"排队"，而后面那个
        也没被拦住——实测两个工单会同时 running。
        """

        if job.is_finished:
            return
        if job.status not in {"queued", "waiting"}:
            return
        if self._next_queued() is not job:
            return  # 前面还有人，乖乖等
        job.started_at = job.started_at or time.time()
        if job.rounds_done == 0:
            job.status = "running"
        self.run_round(job)

    def queue_overview(self, me: str = "") -> dict[str, Any]:
        """队列总览：给界面显示「谁在跑、我排第几、还要多久」。

        ``me`` 是当前使用者的标识，用来标出"哪一条是我自己的"。不传就是
        纯客观列表（没有"我"的概念，单人使用时的表现）。
        """

        now = time.time()
        with self._lock:
            active = [
                job
                for job in self._jobs.values()
                if not job.is_finished
                and job.status in {"queued", "running", "waiting", "finalizing"}
            ]
        active.sort(key=lambda job: (job.queue_no, job.created_at))
        items = []
        for index, job in enumerate(active):
            rounds_left = max(1, int(job.max_rounds) - int(job.rounds_done))
            items.append(
                {
                    "job_id": job.id,
                    "seed": job.context.seed,
                    "owner": job.owner,
                    "mine": bool(me) and job.owner == me,
                    "status": job.status,
                    # 队列里第一条就是正在跑的那条，位置从 1 开始。
                    "position": index + 1,
                    "created_at": job.created_at,
                    "rounds_done": job.rounds_done,
                    "max_rounds": job.max_rounds,
                    "remaining_requests": job.state.pending_total,
                    # 粗估：前面每个工单还要跑几轮 × 轮间隔。不精确，但能让人
                    # 心里有数——总比只显示"排队中"好。
                    "estimated_wait_seconds": int(
                        rounds_left * max(job.round_interval, 1)
                    ),
                }
            )
        mine = next((item for item in items if item["mine"]), None)
        return {
            "total": len(items),
            "running": next(
                (item for item in items if item["status"] in {"running", "waiting", "finalizing"}),
                None,
            ),
            "mine": mine,
            "items": items,
            "me": me,
            "server_time": now,
        }

    def queue_position(self, job: RelationJob) -> int:
        """这个工单在队里排第几：**1 = 下一个就跑**，0 = 已经开跑（不在队里）。

        和 :meth:`queue_overview` 里的 ``position`` 同一口径，别让两个地方
        对"第几位"给出不同答案（前端同时读这两个字段）。
        """

        if job.started_at or job.is_finished:
            return 0
        with self._lock:
            ahead = [
                other
                for other in self._jobs.values()
                if not other.is_finished
                and other.queue_no < job.queue_no
                and other.status in {"queued", "running", "waiting", "finalizing"}
            ]
        # 已经在跑的不占"排队位置"：队首排队的人就是下一个，所以是 1。
        running = [other for other in ahead if other.started_at]
        return len(ahead) - len(running) + 1

    def get(self, job_id: str) -> RelationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> RelationJob | None:
        """停止续查，并立刻用已取得的记录合成一份部分结果。"""

        job = self.get(job_id)
        if job is None or job.is_finished:
            return job
        job.cancel_requested = True
        job.error = job.error or "已手动停止续查"
        # 正在跑的那一轮会自己收尾；否则立即在本线程合成结果。
        if job._lock.acquire(blocking=False):
            try:
                if not job.is_finished:
                    self._finish(job, status="cancelled", cap_note=self._stop_note(job))
            finally:
                job._lock.release()
        return job

    def _stop_note(self, job: RelationJob) -> str:
        state = job.state
        remaining = state.pending_total
        if not remaining:
            return "已手动停止续查；停止时待查队列已清空。"
        return (
            f"已手动停止续查：完成 {job.rounds_done} 轮、{job.total_requests} 次请求；"
            f"仍有 {state.pending_transfer} 个转账明细 / {state.pending_stake} 个子网请求"
            f"（共 {remaining} 个）未查询，结果只包含已取得的记录，不是完整历史。"
        )

    def due(self, now: float | None = None) -> list[RelationJob]:
        """到点该推进的工单。**一次只给一个**，按发起先后排。

        以前这里返回所有到点的作业，多人同时查询时会一起跑：两个工单各拿
        一半额度、都变慢，用户看到的就是"抢额度、等太久"。现在改成严格排队
        ——同一时刻只有一个工单在推进，前面收尾了后面的才开跑。

        另外把「排队中」的工单也纳入：轮到它时由这里开始第一轮。
        """

        now = time.time() if now is None else now
        head = self._next_queued()
        if head is None:
            return []
        if head.status == "queued":
            # 轮到它了（前面都收尾了）：立刻开跑。
            return [head]
        if head.status == "waiting" and head.next_round_at <= now:
            return [head]
        return []

    def cleanup(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if job.is_finished and now - job.finished_at > JOB_RETENTION_SECONDS
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)

    # -- 推进 -------------------------------------------------------------
    def run_round(self, job: RelationJob) -> None:
        """推进一个工单的一轮。**必须排队**——不许越过前面的工单抢额度。

        这是队列的最终保证点：调度器、create()、_promote_next()、后台线程
        都会走到这里。两道闸：

        1. 必须是队首（按 ``queue_no`` 最小的那个）；
        2. 全局只有一个"正在推进"的占位名额（``_running_id``）。

        只靠第 1 条在并发下不够：两个线程可能**同时**判断"我是队首"然后一起
        跑（实测出现过：create_async 的后台线程 + 调度器撞在一起，两个工单
        都成了 running）。第 2 条用线程锁把这个窗口彻底关掉。

        **接力必须等闸门放开后再做**：``_run_round_locked`` 可能在本轮里就把
        自己收尾（``_finish``），而 ``_finish`` 会想拉起下一个工单。此时
        ``_run_gate`` 还被当前线程攥着，下一个工单会被标成 ``running`` 却
        跑不动，永久卡死（线上实测踩到，见 ``_promote_next`` 的说明）。
        所以这里不在锁内接力，而是记一笔，等 finally 里闸门释放后再拉。
        """

        if job.is_finished:
            return
        if not self._run_gate.acquire(blocking=False):
            return  # 已经有别的工单在推进，直接让开
        handed_off = False
        try:
            # 抢到名额后重新确认身份：可能在这期间队首变了。
            head = self._next_queued()
            if head is None or head.id != job.id:
                return
            if self._running_id and self._running_id != job.id:
                return
            if not job._lock.acquire(blocking=False):
                return
            self._running_id = job.id
            try:
                self._run_round_locked(job)
            finally:
                self._running_id = ""
                job._lock.release()
                # 本轮把自己收尾了：出闸门后要把队里下一个拉起来。
                handed_off = job.is_finished
        finally:
            self._run_gate.release()
            if handed_off:
                self._promote_next()


    def _run_round_locked(self, job: RelationJob) -> None:
        state = job.state
        if job.cancel_requested:
            self._finish(job, status="cancelled", cap_note=self._stop_note(job))
            return
        if not state.has_work:
            self._finish(job)
            return
        if job.rounds_done >= job.max_rounds:
            self._finish(
                job,
                cap_note=(
                    f"已达到分轮续查上限（本次设定 {job.max_rounds} 轮），仍有 "
                    f"{state.pending_total} 个待查请求未执行；"
                    "可调高轮数后重新发起查询继续。"
                ),
            )
            return
        if len(state.address_depth) > job.max_addresses:
            self._finish(
                job,
                cap_note=(
                    f"发现地址已超过 {job.max_addresses} 个上限，为避免无限扩散已停止续查；"
                    f"仍有 {state.pending_total} 个待查请求未执行。"
                ),
            )
            return

        job.status = "running"
        state.api_error = ""
        # 第一轮要扣掉「起始地址画像」已经花掉的请求（同一分钟内），
        # 之后每轮隔 62 秒，额度自然重置，就不再扣。
        # 单轮预算跟随「每分钟额度」：升级套餐后一轮能吃更多，
        # 否则每分钟剩下的额度全浪费。
        budget = taostats_request_budget()
        if job.rounds_done == 0:
            budget = max(0, budget - job.prefetched_requests)
        try:
            # should_stop 让"停止续查"在本轮任意两个请求之间生效：
            # 当前请求一结束就收尾，不等本轮剩余请求（每个最长 20 秒）。
            spent = job.crawl.run_round(
                budget,
                should_stop=lambda: job.cancel_requested,
            )
        except Exception as exc:  # pragma: no cover - 兜底，正常错误在 crawl 内部处理
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
            # 失败也写一条查询记录：否则用户在“查询记录”里找不到这次查询，
            # 会以为记录漏了、跟实际操作对不上。
            self._archive_failed(job)
            return
        job.rounds_done += 1
        job.total_requests += spent

        if job.cancel_requested:
            self._finish(job, status="cancelled", cap_note=self._stop_note(job))
            return
        if state.fatal_error:
            self._finish(job)
            return
        if not state.has_work:
            self._finish(job)
            return
        if spent == 0:
            # 本轮一次请求都没成功（通常是限流或上游不可用）：不再快速空转，
            # 连续 3 轮拿不到数据就收尾，避免把轮数浪费在等待上。
            job.empty_rounds += 1
            if job.empty_rounds >= 3:
                self._finish(
                    job,
                    cap_note=(
                        "连续 3 轮没有取得任何数据（可能被 Taostats 限流或服务不可用），"
                        f"已停止续查；仍有 {state.pending_total} 个待查请求未执行。"
                    ),
                )
                return
        else:
            job.empty_rounds = 0
        job.status = "waiting"
        job.next_round_at = time.time() + job.round_interval

    def _archive_failed(self, job: RelationJob) -> None:
        """失败作业补一条查询记录（不含结果），保证记录条数与实际操作一致。"""

        if job.finalized:
            return
        job.finalized = True
        job.next_round_at = 0.0
        notes = list(job.state.warnings)
        notes.append(f"查询失败：{job.error}")
        job.result = {
            "seed": job.context.seed,
            "related": [],
            "edges": [],
            "library_matches": [],
            "seed_library_matches": [],
            "warnings": notes,
            "data_source": "taostats",
            "max_depth": job.context.depth,
            "job": {
                "id": job.id,
                "rounds": job.rounds_done,
                "max_rounds": job.max_rounds,
                "requests": job.total_requests,
                "round_requests": list(job.state.rounds),
                "stopped": False,
                "max_addresses": job.max_addresses,
                "depth": job.context.depth,
            },
        }
        try:
            job.archive_id = job.service.archive_relation(
                job.result,
                job_id=job.id,
                kind="job",
                status="failed",
                rounds=job.rounds_done,
                requests=job.total_requests,
                owner=job.owner,
            )
            job.result["archive_id"] = job.archive_id
        except Exception:  # noqa: BLE001 - 归档失败不该再抛错盖掉原始错误
            pass

    def _promote_next(self) -> None:
        """当前工单收尾后，把队里下一个开起来。

        不开的话队列会一直停在原地：due() 虽然会把队首的 queued 交出去，
        但那只在调度器 tick 时发生；这里主动拉一下，用户不用多等一个 tick。

        **必须等 ``_run_gate`` 释放后再调**。它内部会走到 ``run_round``，
        而那把闸门是不可重入的普通 ``Lock``；如果在前一个工单还攥着闸门时
        调用，下一个工单会被标成 ``running``/写了 ``started_at``，却因为拿不到
        闸门直接 return——留下一个"看起来在跑、其实一次没跑"的假活口，
        而 ``due()`` 只捡 ``queued``/``waiting``，它就此永久卡死（线上实测：
        李四的工单卡在 running、rounds_done=0，最后被 cleanup 悄悄清掉，
        查询记录里也没有它）。

        因此调用点只有两个：``run_round`` 的 finally（闸门刚放开）和
        ``_finish`` 从外部被直接调用时（那条路径本来就没持闸门）。
        """

        head = self._next_queued()
        if head is None or head.status != "queued":
            return
        # 闸门还被别人攥着就交给对方收尾时接力，不要在这里硬拉：
        # 硬拉只会把工单标成假 running。
        if self._run_gate.locked():
            return
        try:
            self._start_job(head)
        except Exception:  # noqa: BLE001 - 拉下一个失败不能影响已收尾的工单
            pass


    def _finish(
        self, job: RelationJob, *, cap_note: str = "", status: str = "complete"
    ) -> None:
        if job.finalized:
            return
        job.finalized = True
        # 合成结果（含余额 RPC、结果归档）期间明确标记状态，前端显示“正在合成结果”，
        # 避免跑满轮数后仍显示“正在查询”让人以为卡死。
        job.status = "finalizing"
        state = job.state
        if cap_note:
            state.warnings.append(cap_note)
        try:
            job.result = job.service.finalize_relations(
                job.context,
                job.crawl,
                pending_note="",
                # 爬取已经结束，余额查询不再和关联请求抢额度。
                balance_budget_left=taostats_request_budget(),
                # 停止续查时跳过余额 RPC：用户要的是马上看到已有结果。
                include_balances=status != "cancelled",
                # 两批余额 RPC 合计最多 12 秒：地址再多、节点再慢也要立刻出结果。
                balance_deadline=time.monotonic() + 12.0,
            )
        except Exception as exc:  # pragma: no cover - 兜底，避免作业卡在 running
            job.status = "failed"
            job.error = f"结果合成失败：{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
            self._promote_next()  # 我挂了，也得让后面排队的人跑起来
            return
        job.result["job"] = {
            "id": job.id,
            "rounds": job.rounds_done,
            "max_rounds": job.max_rounds,
            "requests": job.total_requests,
            "round_requests": list(state.rounds),
            "capped": bool(cap_note) and status != "cancelled",
            "stopped": status == "cancelled",
            "pending_transfer": state.pending_transfer,
            "pending_stake": state.pending_stake,
            "remaining_requests": state.pending_total,
            # 层级相关的护栏要一起存进归档：事后翻「查询记录」时能看出
            # 这次被 150 个地址卡住，还是层级本来就只扫了 3 级。
            "max_addresses": job.max_addresses,
            "depth": job.context.depth,
        }
        job.stopped = status == "cancelled"
        job.status = status
        job.next_round_at = 0.0
        job.finished_at = time.time()
        # 结果落库：即使页面关掉或忘了导出，也能在“查询记录”里找回来。
        try:
            job.archive_id = job.service.archive_relation(
                job.result,
                job_id=job.id,
                kind="job",
                status=status,
                rounds=job.rounds_done,
                requests=job.total_requests,
                owner=job.owner,
            )
            job.result["archive_id"] = job.archive_id
        except Exception as exc:  # noqa: BLE001 - 归档失败不影响本次结果
            job.result.setdefault("warnings", []).append(f"查询记录保存失败：{exc}")
        # 本工单结束，队列里下一个立刻开跑：不这样的话后面的人要多等一个
        # 调度 tick，多人排队时每个都白等几秒。
        self._promote_next()

    # -- 输出 -------------------------------------------------------------
    def payload(self, job: RelationJob) -> dict[str, Any]:
        state = job.state
        now = time.time()
        if job.status == "waiting":
            next_round_in = max(0, int(round(job.next_round_at - now)))
        else:
            next_round_in = 0
        remaining = state.pending_total
        plan = (
            job.crawl.preview_next_round(taostats_request_budget())
            if state.has_work and not job.is_finished
            else {"requests": 0, "counts": {}, "samples": []}
        )
        position = self.queue_position(job)
        # 排队还要等多久：粗估「前面每个工单大约跑几轮 × 轮间隔」。
        # 不精确（每个工单数据量不同），但比"什么都没说"好得多——
        # 用户最怕的就是「等太久还不知道在等什么」。
        queue_wait = 0
        if position > 0:
            with self._lock:
                ahead = [
                    other
                    for other in self._jobs.values()
                    if not other.is_finished
                    and other.queue_no < job.queue_no
                    and other.status in {"queued", "running", "waiting", "finalizing"}
                ]
            rounds_left = sum(
                max(1, int(other.max_rounds) - int(other.rounds_done)) for other in ahead
            )
            queue_wait = int(rounds_left * max(job.round_interval, 1))
        return {
            "job_id": job.id,
            "status": job.status,
            "owner": job.owner,
            # 排队位置：1 = 下一个就跑；0 = 已经开跑。
            "queue_position": position,
            "queue_wait_seconds": queue_wait,
            "queue_no": job.queue_no,
            "cancel_requested": job.cancel_requested and not job.is_finished,
            "seed": job.context.seed,
            "created_at": job.created_at,
            "rounds_done": job.rounds_done,
            "max_rounds": job.max_rounds,
            "round_interval_seconds": job.round_interval,
            "requests_total": job.total_requests,
            "requests_last_round": job.requests_last_round,
            "requests_per_round": taostats_request_budget(),
            "prefetched_requests": job.prefetched_requests,
            "pending_transfer": state.pending_transfer,
            "pending_stake": state.pending_stake,
            "remaining_requests": remaining,
            "estimated_rounds_left": (
                math.ceil(remaining / taostats_request_budget())
                if remaining
                else 0
            ),
            "queued_addresses": len(state.address_depth),
            "max_addresses": job.max_addresses,
            "remote_records": len(state.records_by_key),
            "next_round_in": next_round_in,
            "next_round_plan": plan,
            "last_round": (state.round_log[-1] if state.round_log else None),
            "rounds_log": list(state.round_log),
            "kind_labels": KIND_LABELS,
            "error": job.error,
            # 区分「key 无效」与「够不到 Taostats」：页面据此给不同建议。
            "api_error": state.api_error,
            "api_error_kind": state.api_error_kind,
            "archive_id": job.archive_id,
            "warnings": list(dict.fromkeys(state.warnings))[:8],
            "result": job.result,
        }
