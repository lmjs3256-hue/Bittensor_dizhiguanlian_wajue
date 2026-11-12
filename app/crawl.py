"""可续跑的 Taostats 关联爬取。

单轮查询和分轮作业共用这套状态机：``run_round`` 只花掉给定的请求预算，
进度（待查队列、已查分页、累计记录）留在 :class:`CrawlState` 里，下一轮
从断点继续，不会重复消耗已经查过的地址和分页。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .relation_config import LinkPolicy, TransferRecord
from .taostats import (
    TaostatsAuthError,
    TaostatsRateLimitError,
    TaostatsUnavailableError,
)
from .validation import is_valid_ss58

# 请求优先级：数字越小越先花预算。
TIER_TRANSFER_SHALLOW = 0  # 转账明细首页 · 深度 0-1（一级关联）
TIER_STAKE_SHALLOW = 1  # 子网 stake 转移 · 深度 0-1
TIER_TRANSFER_DEEP = 2  # 转账明细首页 · 深度 >= 2
TIER_TRANSFER_MORE = 3  # 转账明细分页（第 2 页及以后）
TIER_STAKE_DEEP = 4  # 子网 stake 转移 · 深度 >= 2


@dataclass
class CrawlState:
    seed: str
    depth: int
    policy: LinkPolicy
    terminal_exchange_addresses: set[str] = field(default_factory=set)
    excluded_addresses: set[str] = field(default_factory=set)

    queue: list[tuple[int, int, int, str, str, int]] = field(default_factory=list)
    next_seq: int = 0
    queued_pages: set[tuple[str, int]] = field(default_factory=set)
    queued_stake: set[str] = field(default_factory=set)
    address_depth: dict[str, int] = field(default_factory=dict)
    records_by_key: dict[str, TransferRecord] = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)
    started: bool = False
    stake_disabled: bool = False
    pagination_complete: bool = True
    provider_window_truncated: bool = False
    api_error: str = ""
    # 出错类型：``"auth"``（key 无效）/ ``"network"``（连不上）/ ``""``。
    # 页面必须能区分「没有可用 key」和「够不到 Taostats」，否则修错方向。
    api_error_kind: str = ""
    fatal_error: str = ""

    api_requests: int = 0
    cache_hits: int = 0
    round_requests: int = 0
    rounds: list[int] = field(default_factory=list)
    # 每轮实际查了什么，供界面展示「第 N 轮查询内容」。
    round_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.queue)

    @property
    def pending_transfer(self) -> int:
        return sum(1 for item in self.queue if item[3] == "transfer")

    @property
    def pending_stake(self) -> int:
        return sum(1 for item in self.queue if item[3] == "stake")

    @property
    def pending_total(self) -> int:
        return len(self.queue)


# 请求类别到中文说明的映射，前端与接口共用一套口径。
KIND_LABELS = {
    "transfer_shallow": "一级关联转账",
    "stake_shallow": "一级子网转移",
    "transfer_deep": "二级及更深转账",
    "transfer_pages": "转账历史翻页",
    "stake_deep": "二级及更深子网转移",
}


def classify_request(tier: int, kind: str, page_number: int, depth: int) -> str:
    """把队列项归类，用于统计和界面提示。"""

    if kind == "stake":
        return "stake_shallow" if depth <= 1 else "stake_deep"
    if page_number > 1:
        return "transfer_pages"
    return "transfer_shallow" if depth <= 1 else "transfer_deep"


class TaostatsCrawl:
    """在同一份 :class:`CrawlState` 上按预算分多次推进的爬取器。"""

    def __init__(
        self,
        state: CrawlState,
        *,
        fetch_transfer: Callable[[str, int], Any],
        fetch_stake: Callable[[str, int], Any],
        record_request: Callable[..., None],
    ) -> None:
        self.state = state
        self._fetch_transfer = fetch_transfer
        self._fetch_stake = fetch_stake
        self._record_request = record_request

    # -- 队列维护 ---------------------------------------------------------
    def start(self) -> None:
        state = self.state
        if state.started:
            return
        state.started = True
        state.address_depth[state.seed] = 0
        self.queue_transfer(state.seed, 0, 1)
        self.queue_stake(state.seed, 0)

    def queue_transfer(self, addr: str, addr_depth: int, page_number: int) -> None:
        state = self.state
        if (addr, page_number) in state.queued_pages:
            return
        state.queued_pages.add((addr, page_number))
        if page_number > 1:
            tier = TIER_TRANSFER_MORE
        elif addr_depth <= 1:
            tier = TIER_TRANSFER_SHALLOW
        else:
            tier = TIER_TRANSFER_DEEP
        heapq.heappush(
            state.queue,
            (tier, addr_depth, state.next_seq, "transfer", addr, page_number),
        )
        state.next_seq += 1

    def queue_stake(self, addr: str, addr_depth: int) -> None:
        state = self.state
        if addr in state.queued_stake:
            return
        state.queued_stake.add(addr)
        tier = TIER_STAKE_SHALLOW if addr_depth <= 1 else TIER_STAKE_DEEP
        heapq.heappush(
            state.queue, (tier, addr_depth, state.next_seq, "stake", addr, 1)
        )
        state.next_seq += 1

    def schedule_neighbors(self, neighbors: set[str], current_depth: int) -> None:
        state = self.state
        next_depth = current_depth + 1
        if next_depth >= state.depth:
            return
        for neighbor in sorted(neighbors):
            if next_depth >= state.address_depth.get(neighbor, state.depth + 1):
                continue
            if neighbor in state.terminal_exchange_addresses:
                # 保留边，让公共交易所出现在结果里，但不穿过它的高频转出图。
                continue
            state.address_depth[neighbor] = next_depth
            self.queue_transfer(neighbor, next_depth, 1)
            self.queue_stake(neighbor, next_depth)

    # -- 推进 -------------------------------------------------------------
    def run_round(self, budget: int, *, should_stop=None) -> int:
        """花掉至多 ``budget`` 次请求；返回本轮实际请求数。

        ``should_stop`` 在每个请求开始前检查，返回 True 时立即收尾本轮。
        「停止续查」靠它做到：不用等本轮剩余请求（每个超时 20 秒）跑完。
        """

        state = self.state
        state.round_requests = 0
        self.start()
        tally = {key: 0 for key in KIND_LABELS}
        addresses_before = len(state.address_depth)
        records_before = len(state.records_by_key)
        while state.queue and state.round_requests < budget:
            if should_stop is not None and should_stop():
                break
            tier, current_depth, _, kind, address, page_number = heapq.heappop(
                state.queue
            )
            if current_depth >= state.depth:
                continue
            if address in state.terminal_exchange_addresses:
                continue
            category = classify_request(tier, kind, page_number, current_depth)
            if kind == "stake":
                tally[category] += 1
                outcome = self._run_stake(address, page_number, current_depth)
                if outcome == "retry":
                    # 限流/服务不可用：原样放回队列，下一轮再试，不静默丢请求。
                    tally[category] -= 1
                    self._requeue(tier, current_depth, kind, address, page_number)
                    break
                continue
            tally[category] += 1
            outcome = self._run_transfer(address, page_number, current_depth)
            if outcome != "ok":
                tally[category] -= 1
                if outcome == "retry":
                    self._requeue(tier, current_depth, kind, address, page_number)
                break
        state.rounds.append(state.round_requests)
        state.round_log.append(
            {
                "round": len(state.rounds),
                "requests": state.round_requests,
                "counts": {key: value for key, value in tally.items() if value},
                "new_addresses": len(state.address_depth) - addresses_before,
                "new_records": len(state.records_by_key) - records_before,
                "remaining": len(state.queue),
            }
        )
        return state.round_requests

    def _requeue(
        self, tier: int, depth: int, kind: str, address: str, page_number: int
    ) -> None:
        heapq.heappush(
            self.state.queue, (tier, depth, self.state.next_seq, kind, address, page_number)
        )
        self.state.next_seq += 1

    def preview_next_round(self, budget: int) -> dict[str, Any]:
        """只读地预估下一轮会查什么，不消耗请求、不改动队列。"""

        state = self.state
        counts = {key: 0 for key in KIND_LABELS}
        samples: list[dict[str, Any]] = []
        taken = 0
        for tier, depth, _, kind, address, page_number in sorted(state.queue):
            if taken >= budget:
                break
            if depth >= state.depth or address in state.terminal_exchange_addresses:
                continue
            category = classify_request(tier, kind, page_number, depth)
            counts[category] += 1
            taken += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "address": address,
                        "kind": kind,
                        "page": page_number,
                        "depth": depth,
                        "category": category,
                    }
                )
        return {
            "requests": taken,
            "counts": {key: value for key, value in counts.items() if value},
            "samples": samples,
        }

    def _run_transfer(self, address: str, page_number: int, current_depth: int) -> str:
        state = self.state
        try:
            page = self._fetch_transfer(address, page_number)
            if page.from_cache:
                state.cache_hits += 1
            else:
                state.round_requests += 1
                state.api_requests += 1
                self._record_request("/v1/transfers", success=True)
        except (TaostatsAuthError, TaostatsRateLimitError, TaostatsUnavailableError) as exc:
            if getattr(exc, "request_made", False):
                self._record_request(
                    "/v1/transfers",
                    status_code=getattr(exc, "status_code", None),
                    success=False,
                )
            state.api_error = str(exc)
            state.api_error_kind = "auth" if isinstance(exc, TaostatsAuthError) else "network"
            if isinstance(exc, TaostatsAuthError):
                # 密钥问题不会随时间自愈，分轮作业应直接结束而不是每分钟重试。
                state.fatal_error = str(exc)
                state.pagination_complete = False
                return "fatal"
            # 限流/服务不可用是暂时性的：请求放回队列下一轮再试，避免静默丢掉该地址。
            return "retry"

        if page.truncated_by_window:
            state.provider_window_truncated = True
            state.pagination_complete = False
            state.warnings.append(
                "Taostats 的服务端分页窗口未覆盖该地址全部匹配记录；"
                "本次结果不是完整历史。"
            )
        if page.next_page is not None:
            self.queue_transfer(address, current_depth, page.next_page)

        next_addresses: set[str] = set()
        for item in page.rows:
            try:
                source = str(item["from"]).strip()
                target = str(item["to"]).strip()
                block_number = int(item["block_number"])
                raw_amount = Decimal(str(item["amount"]))
                tx_hash = str(item.get("transaction_hash") or "")
                transfer_id = str(item.get("id") or "")
                timestamp = str(item.get("timestamp") or "")
            except (KeyError, TypeError, ValueError, InvalidOperation):
                state.pagination_complete = False
                state.warnings.append("Taostats 返回记录中有字段无法解析，已跳过该记录。")
                continue
            if not source or not target or not raw_amount.is_finite() or raw_amount < 0:
                state.pagination_complete = False
                state.warnings.append("Taostats 返回记录中有无效地址或金额，已跳过该记录。")
                continue
            amount_rao = int(raw_amount)
            if raw_amount != amount_rao:
                state.pagination_complete = False
                state.warnings.append("Taostats 返回了非整数 RAO 金额，已跳过该记录。")
                continue
            if address not in {source, target}:
                state.pagination_complete = False
                state.warnings.append("Taostats 返回了与查询地址无关的转账记录，已跳过该记录。")
                continue
            key = transfer_id or f"{block_number}:{tx_hash}:{source}:{target}:{amount_rao}"
            amount_tao = Decimal(amount_rao) / Decimal(1_000_000_000)
            state.records_by_key[key] = TransferRecord(
                tx_hash=tx_hash,
                block_number=block_number,
                from_address=source,
                to_address=target,
                amount_tao=amount_tao,
                timestamp=timestamp,
            )
            # 第 1 跳用「1 级关联最低 TAO」，更深的跳用「单边最低 TAO」。
            # 与 relation.discover() 共用 edge_floor()，避免「爬到的记录」和
            # 「最终算出的关联」用两套门槛。
            if amount_tao < state.policy.edge_floor(current_depth):
                continue
            if (
                source == address
                and state.policy.include_outgoing
                and target not in state.excluded_addresses
            ):
                next_addresses.add(target)
            if (
                target == address
                and state.policy.include_incoming
                and source not in state.excluded_addresses
            ):
                next_addresses.add(source)

        self.schedule_neighbors(next_addresses, current_depth)
        return "ok"

    def _run_stake(self, address: str, page_number: int, current_depth: int) -> str:
        state = self.state
        if state.stake_disabled:
            return "ok"
        stake_neighbors: set[str] = set()
        try:
            stake_page = self._fetch_stake(address, page_number)
            if stake_page.from_cache:
                state.cache_hits += 1
            else:
                state.round_requests += 1
                state.api_requests += 1
                self._record_request("/v1/subnets/stake-events", success=True)
            skipped_stake_rows = 0
            for item in stake_page.rows:
                if str(item.get("is_transfer") or "").lower() not in {"true", "1"}:
                    continue
                # Taostats 把「同一个 coldkey 下两个 hotkey 互换」也标成
                # is_transfer=true，id 形如
                # hotkey-swap-<block>-<netuid>-<old>-<new>-<coldkey>-in/out。
                # 它的 transfer_address 是 hotkey 而不是对手方 coldkey，amount 为 0，
                # 两条腿的 transfer_address / hotkey 恰好互为对方。当成跨钱包转账
                # 会在关联图里凭空造出一条 0 元直连边，把地址误判成一级关联。
                if str(item.get("id") or "").startswith("hotkey-swap-"):
                    continue
                try:
                    coldkey = str(item["coldkey"]).strip()
                    block_number = int(item["block_number"])
                    raw_amount = Decimal(str(item["amount"]))
                    amount_rao = int(raw_amount)
                    if raw_amount != amount_rao:
                        raise ValueError("non-integer RAO amount")
                    raw_netuid = item.get("netuid")
                    netuid = int(raw_netuid) if raw_netuid is not None else None
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    skipped_stake_rows += 1
                    continue
                # 0 金额事件没有任何资金往来，不能成为关联边；即便用户把
                # 「1 级关联最低 TAO」设成 0（不限金额），也不能漏进关联图。
                if amount_rao <= 0:
                    continue
                counterparty = stake_counterparty(item)
                hotkey = str(item.get("hotkey") or "").strip()
                # 对手方就是验证密钥本身时，同样说明 transfer_address 装的是
                # hotkey 而非另一个 coldkey，这条腿必须丢弃。
                if counterparty and hotkey and counterparty == hotkey:
                    continue
                action = str(item.get("action") or "").lower()
                if not coldkey or not counterparty or action not in {"stake", "unstake"}:
                    skipped_stake_rows += 1
                    continue
                # 方向依据文档中的腿语义推断：unstake 离开 coldkey，stake 到达
                # coldkey。这是推断，不是已验证的服务端返回契约。
                if action == "unstake":
                    source, target = coldkey, counterparty
                else:
                    source, target = counterparty, coldkey
                if source == target or address not in {source, target}:
                    continue
                amount_tao = Decimal(amount_rao) / Decimal(1_000_000_000)
                stake_id = str(item.get("id") or "")
                key = "stake:" + (
                    stake_id or f"{block_number}:{source}:{target}:{amount_rao}"
                )
                state.records_by_key[key] = TransferRecord(
                    tx_hash=str(item.get("extrinsic_id") or stake_id),
                    block_number=block_number,
                    from_address=source,
                    to_address=target,
                    amount_tao=amount_tao,
                    timestamp=str(item.get("timestamp") or ""),
                    edge_type="stake_transfer",
                    netuid=netuid,
                    hotkey=hotkey,
                )
                # 同上：第 1 跳按 1 级门槛，其余按单边门槛。
                if amount_tao < state.policy.edge_floor(current_depth):
                    continue
                if (
                    source == address
                    and state.policy.include_outgoing
                    and target not in state.excluded_addresses
                ):
                    stake_neighbors.add(target)
                if (
                    target == address
                    and state.policy.include_incoming
                    and source not in state.excluded_addresses
                ):
                    stake_neighbors.add(source)
            if skipped_stake_rows:
                state.warnings.append(
                    f"Taostats 返回 {skipped_stake_rows} 条子网事件缺少可解析的对手方或方向字段，已跳过；"
                    "这些记录未进入关联图。"
                )
        except TaostatsAuthError as exc:
            if getattr(exc, "request_made", False):
                self._record_request(
                    "/v1/subnets/stake-events",
                    status_code=getattr(exc, "status_code", None),
                    success=False,
                )
            # 同一把 key 失败后后续子网查询不会成功，停用并清空队列中的子网任务，
            # 避免重复告警占用后面的转账额度。
            state.stake_disabled = True
            state.queue[:] = [item for item in state.queue if item[3] != "stake"]
            heapq.heapify(state.queue)
            state.warnings.append(f"子网 stake 转移查询未完成：{exc}")
            return "ok"
        except (TaostatsRateLimitError, TaostatsUnavailableError) as exc:
            if getattr(exc, "request_made", False):
                self._record_request(
                    "/v1/subnets/stake-events",
                    status_code=getattr(exc, "status_code", None),
                    success=False,
                )
            state.warnings.append(f"子网 stake 转移查询未完成：{exc}")
            self.schedule_neighbors(stake_neighbors, current_depth)
            return "retry"
        self.schedule_neighbors(stake_neighbors, current_depth)
        return "ok"


def stake_counterparty(item: dict[str, Any]) -> str:
    """从 Taostats stake 事件里读取对手方地址。

    服务端文档只提供了 ``transfer_address`` 查询过滤，公开响应里该字段可能
    是纯地址，也可能是带 ``ss58`` 的对象。这里只返回合法 SS58，避免十六进制
    key 混进关联图。
    """

    value = item.get("transfer_address")
    if isinstance(value, dict):
        value = value.get("ss58")
    text = str(value or "").strip()
    return text if text and is_valid_ss58(text) else ""
