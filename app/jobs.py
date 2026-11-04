"""分轮关联查询作业。

Taostats 每分钟只允许 5 次请求，一次查询通常查不完三级关联。作业把爬取
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
from .service import MAX_TAOSTATS_REQUESTS_PER_QUERY, LinkService, RelationContext


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
JOB_RETENTION_SECONDS = 30 * 60


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
    # 连续「0 请求」轮次：被限流或没有可用数据时不再空转下去。
    empty_rounds: int = 0
    cancel_requested: bool = False
    finalized: bool = False
    stopped: bool = False
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
        removed_addresses: list[str] | None = None,
        round_interval: int | None = None,
        max_rounds: int | None = None,
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
            removed_addresses=removed_addresses or (),
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
                "removed_addresses": sorted(set(removed_addresses or ())),
            },
            created_at=time.time(),
            round_interval=float(round_interval or self.round_interval),
            max_rounds=int(max_rounds or self.max_rounds),
            max_addresses=self.max_addresses,
        )
        with self._lock:
            self._jobs[job.id] = job
        job.crawl.start()  # 先把起始地址排进队列，首轮才有活可干
        self.run_round(job)
        return job

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
        now = time.time() if now is None else now
        with self._lock:
            jobs = list(self._jobs.values())
        return [
            job
            for job in jobs
            if job.status == "waiting" and job.next_round_at <= now and not job.is_finished
        ]

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
        if job.is_finished:
            return
        if not job._lock.acquire(blocking=False):
            return
        try:
            self._run_round_locked(job)
        finally:
            job._lock.release()

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
        try:
            # should_stop 让"停止续查"在本轮任意两个请求之间生效：
            # 当前请求一结束就收尾，不等本轮剩余请求（每个最长 20 秒）。
            spent = job.crawl.run_round(
                MAX_TAOSTATS_REQUESTS_PER_QUERY,
                should_stop=lambda: job.cancel_requested,
            )
        except Exception as exc:  # pragma: no cover - 兜底，正常错误在 crawl 内部处理
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
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

    def _finish(
        self, job: RelationJob, *, cap_note: str = "", status: str = "complete"
    ) -> None:
        if job.finalized:
            return
        job.finalized = True
        state = job.state
        if cap_note:
            state.warnings.append(cap_note)
        try:
            job.result = job.service.finalize_relations(
                job.context,
                job.crawl,
                pending_note="",
                # 爬取已经结束，余额查询不再和关联请求抢额度。
                balance_budget_left=MAX_TAOSTATS_REQUESTS_PER_QUERY,
                # 停止续查时跳过余额 RPC：用户要的是马上看到已有结果。
                include_balances=status != "cancelled",
            )
        except Exception as exc:  # pragma: no cover - 兜底，避免作业卡在 running
            job.status = "failed"
            job.error = f"结果合成失败：{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
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
            )
            job.result["archive_id"] = job.archive_id
        except Exception as exc:  # noqa: BLE001 - 归档失败不影响本次结果
            job.result.setdefault("warnings", []).append(f"查询记录保存失败：{exc}")

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
            job.crawl.preview_next_round(MAX_TAOSTATS_REQUESTS_PER_QUERY)
            if state.has_work and not job.is_finished
            else {"requests": 0, "counts": {}, "samples": []}
        )
        return {
            "job_id": job.id,
            "status": job.status,
            "cancel_requested": job.cancel_requested and not job.is_finished,
            "seed": job.context.seed,
            "created_at": job.created_at,
            "rounds_done": job.rounds_done,
            "max_rounds": job.max_rounds,
            "round_interval_seconds": job.round_interval,
            "requests_total": job.total_requests,
            "requests_last_round": job.requests_last_round,
            "requests_per_round": MAX_TAOSTATS_REQUESTS_PER_QUERY,
            "pending_transfer": state.pending_transfer,
            "pending_stake": state.pending_stake,
            "remaining_requests": remaining,
            "estimated_rounds_left": (
                math.ceil(remaining / MAX_TAOSTATS_REQUESTS_PER_QUERY)
                if remaining
                else 0
            ),
            "queued_addresses": len(state.address_depth),
            "remote_records": len(state.records_by_key),
            "next_round_in": next_round_in,
            "next_round_plan": plan,
            "last_round": (state.round_log[-1] if state.round_log else None),
            "rounds_log": list(state.round_log),
            "kind_labels": KIND_LABELS,
            "error": job.error,
            "archive_id": job.archive_id,
            "warnings": list(dict.fromkeys(state.warnings))[:8],
            "result": job.result,
        }
