"""两个地址之间的转账明细检索。

两种用法：

1. **地址 × 地址库**：给一个地址和一个标签组 / 自建库，找出这个地址跟库里哪些
   地址真的发生过转账，逐笔列出时间、金额、方向、区块和交易哈希。
2. **地址 × 地址**：给两个地址，先精确查它们之间的直接转账；没有直连时顺着
   双方各自的转账流水找中间地址，拼出 A → 中间 → … → B 的链路。

数据源两条腿：

- **本地索引**（``transfers`` 表）：免费，但只覆盖本机采集过的区块。
- **Taostats**：``from`` / ``to`` 精确匹配，用来取两地址之间的全部直连；组模式下
  则按地址翻页取流水，再与库成员求交（比逐对查询省请求）。

Taostats 有本机限速（每分钟 5 次），所以每次检索带预算：预算用完就停下，
返回已经查到的部分并标记 ``truncated``，而不是整体报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import and_, or_, select

from . import taostats
from .db import AddressLibrary, Transfer
from .relation_config import DEFAULT_RELATION_DEPTH, MAX_RELATION_DEPTH

RAO_PER_TAO = 1_000_000_000
# 与 taostats.LOCAL_REQUESTS_PER_MINUTE 对齐；单次检索最多花这么多次请求。
DEFAULT_REQUEST_BUDGET = taostats.LOCAL_REQUESTS_PER_MINUTE
# 每一层从当前候选里挑多少个"最像中转"的地址继续往下查。
MAX_INTERMEDIATES = 3
# 一次最多返回几条链路。
MAX_PATHS = 3
# 翻页上限与关联层级用同一个天花板：层级是「最多走几跳」，翻页上限也就是
# 「最多往回翻几页」，两者都跟 MAX_RELATION_DEPTH 对齐，避免一个放开一个没放。
MAX_PAGES = MAX_RELATION_DEPTH


@dataclass
class Member:
    address: str
    label: str = ""
    group: str = ""
    category: str = ""
    note: str = ""


@dataclass
class InterlinkQuery:
    source: str
    members: list[Member]
    mode: str = "address"  # "address" | "group"
    max_hops: int = DEFAULT_RELATION_DEPTH
    max_pages: int = DEFAULT_RELATION_DEPTH
    min_tao: float = 0.0
    use_local: bool = True
    use_taostats: bool = True
    group_name: str = ""
    group_kind: str = ""

    @property
    def target_addresses(self) -> list[str]:
        seen: list[str] = []
        for member in self.members:
            if member.address and member.address != self.source and member.address not in seen:
                seen.append(member.address)
        return seen


def _to_tao(value: Any) -> float:
    try:
        return float(int(str(value))) / RAO_PER_TAO
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class _Fetcher:
    """带预算和缓存统计的 Taostats 页面取数器。"""

    budget: int = DEFAULT_REQUEST_BUDGET
    requests: int = 0
    cache_hits: int = 0
    pages: int = 0
    stop_reason: str = ""

    def page(self, **kwargs: Any) -> taostats.TransferPage | None:
        if self.requests >= self.budget:
            self.stop_reason = self.stop_reason or "budget"
            return None
        try:
            page = taostats.get_transfer_page_query(**kwargs)
        except taostats.TaostatsRateLimitError:
            self.stop_reason = "rate_limit"
            return None
        except taostats.TaostatsError as exc:
            self.stop_reason = f"error:{exc}"
            return None
        if page.from_cache:
            self.cache_hits += 1
        else:
            self.requests += 1
            self.pages += 1
        return page


def taostats_row_to_tx(row: dict[str, Any], source: str) -> dict[str, Any]:
    sender = str(row.get("from") or "")
    receiver = str(row.get("to") or "")
    if sender == source:
        direction, counterparty = "out", receiver
    elif receiver == source:
        direction, counterparty = "in", sender
    else:
        direction, counterparty = "", sender if receiver == source else receiver
    try:
        block = int(row.get("block_number") or 0)
    except (TypeError, ValueError):
        block = 0
    return {
        "from": sender,
        "to": receiver,
        "direction": direction,
        "counterparty": counterparty,
        "amount_tao": round(_to_tao(row.get("amount")), 8),
        "timestamp": str(row.get("timestamp") or ""),
        "block_number": block,
        "tx_hash": str(row.get("transaction_hash") or ""),
        "extrinsic_id": str(row.get("extrinsic_id") or ""),
        "edge_type": "tao_transfer",
        "source": "taostats",
    }


def _dedupe_key(tx: dict[str, Any]) -> tuple:
    return (
        tx.get("tx_hash") or tx.get("extrinsic_id") or "",
        tx.get("block_number") or 0,
        round(float(tx.get("amount_tao") or 0.0), 8),
        tx.get("from") or "",
        tx.get("to") or "",
    )


def _merge_tx(bucket: list[dict[str, Any]], tx: dict[str, Any], seen: set[tuple]) -> None:
    key = _dedupe_key(tx)
    if key in seen:
        return
    seen.add(key)
    bucket.append(tx)


def local_transfers(
    database: Any, source: str, targets: Iterable[str]
) -> list[dict[str, Any]]:
    """本地索引里 source 与 targets 之间的转账。"""
    addresses = [address for address in targets if address]
    if not addresses:
        return []
    with database.session() as db:
        stmt = (
            select(Transfer)
            .where(
                or_(
                    and_(
                        Transfer.from_address == source,
                        Transfer.to_address.in_(addresses),
                    ),
                    and_(
                        Transfer.to_address == source,
                        Transfer.from_address.in_(addresses),
                    ),
                )
            )
            .order_by(Transfer.block_number.desc())
        )
        rows = db.scalars(stmt).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        sender = row.from_address or ""
        receiver = row.to_address or ""
        if sender == source:
            direction, counterparty = "out", receiver
        else:
            direction, counterparty = "in", sender
        out.append(
            {
                "from": sender,
                "to": receiver,
                "direction": direction,
                "counterparty": counterparty,
                "amount_tao": round(float(row.amount_tao or 0), 8),
                "timestamp": row.timestamp or "",
                "block_number": int(row.block_number or 0),
                "tx_hash": row.tx_hash or "",
                "extrinsic_id": "",
                "edge_type": row.edge_type or "tao_transfer",
                "source": "local",
            }
        )
    return out


def group_members(
    database: Any, *, kind: str, name: str, exclude: str = ""
) -> list[Member]:
    """按 ``book`` / ``label`` 口径取出一组成员（与前端分组口径一致）。"""
    name = (name or "").strip()
    if not name:
        return []
    with database.session() as db:
        if kind == "book":
            rows = db.scalars(
                select(AddressLibrary).where(AddressLibrary.book == name)
            ).all()
        else:
            rows = db.scalars(
                select(AddressLibrary).where(
                    AddressLibrary.label == name, AddressLibrary.book == ""
                )
            ).all()
    members: list[Member] = []
    for row in rows:
        if exclude and row.address == exclude:
            continue
        members.append(
            Member(
                address=row.address,
                label=row.label or "",
                group=row.book or row.label or "",
                category=row.category or "",
                note=row.note or "",
            )
        )
    return members


def _neighbors(
    fetcher: _Fetcher,
    address: str,
    *,
    pages: int,
    min_tao: float = 0.0,
) -> dict[str, list[dict[str, Any]]]:
    """某地址最近的转账，按对手方归堆。"""
    bucket: dict[str, list[dict[str, Any]]] = {}
    for page_no in range(1, max(1, pages) + 1):
        page = fetcher.page(address=address, page=page_no)
        if page is None:
            break
        for row in page.rows:
            tx = taostats_row_to_tx(row, address)
            if not tx["counterparty"]:
                continue
            if tx["amount_tao"] < min_tao:
                continue
            bucket.setdefault(tx["counterparty"], []).append(tx)
        if not page.has_next:
            break
    return bucket


def _best(txs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not txs:
        return None
    return max(txs, key=lambda tx: abs(float(tx.get("amount_tao") or 0)))


def _summary(
    transactions: list[dict[str, Any]], members: dict[str, Member]
) -> list[dict[str, Any]]:
    """按对手地址聚合：进出金额、笔数、最近时间。"""
    grouped: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        address = tx["counterparty"]
        item = grouped.setdefault(
            address,
            {
                "address": address,
                "label": members.get(address, Member(address)).label,
                "group": members.get(address, Member(address)).group,
                "category": members.get(address, Member(address)).category,
                "in_tao": 0.0,
                "out_tao": 0.0,
                "tx_count": 0,
                "last_seen": "",
            },
        )
        if tx["direction"] == "out":
            item["out_tao"] += tx["amount_tao"]
        else:
            item["in_tao"] += tx["amount_tao"]
        item["tx_count"] += 1
        stamp = tx["timestamp"] or ""
        if stamp > (item["last_seen"] or ""):
            item["last_seen"] = stamp
    rows = list(grouped.values())
    for row in rows:
        row["in_tao"] = round(row["in_tao"], 8)
        row["out_tao"] = round(row["out_tao"], 8)
    rows.sort(key=lambda row: (row["in_tao"] + row["out_tao"], row["tx_count"]), reverse=True)
    return rows


def search(database: Any, query: InterlinkQuery) -> dict[str, Any]:
    source = query.source
    targets = query.target_addresses
    member_map = {member.address: member for member in query.members}
    transactions: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    paths: list[dict[str, Any]] = []
    notes: list[str] = []
    fetcher = _Fetcher(budget=DEFAULT_REQUEST_BUDGET)

    # 1) 本地索引（免费，先查）
    if query.use_local and targets:
        local_rows = local_transfers(database, source, targets)
        for tx in local_rows:
            if tx["amount_tao"] < query.min_tao:
                continue
            _merge_tx(transactions, tx, seen)
        if local_rows:
            notes.append(f"本地索引命中 {len(local_rows)} 笔（覆盖本机采集过的区块）")

    # 2) Taostats
    truncated = False
    if query.use_taostats and targets:
        if query.mode == "group" or len(targets) > 2:
            # 组模式：翻 source 的流水，和库成员求交。1 页 200 条最省。
            target_set = set(targets)
            pages = max(1, query.max_pages)
            for page_no in range(1, pages + 1):
                page = fetcher.page(address=source, page=page_no)
                if page is None:
                    truncated = True
                    break
                for row in page.rows:
                    tx = taostats_row_to_tx(row, source)
                    if tx["counterparty"] not in target_set:
                        continue
                    if tx["amount_tao"] < query.min_tao:
                        continue
                    _merge_tx(transactions, tx, seen)
                if not page.has_next:
                    break
                if page_no == pages:
                    truncated = True
        else:
            # 地址模式：from/to 精确匹配，不受分页窗口影响。
            for sender, receiver in ((source, targets[0]), (targets[0], source)):
                for page_no in range(1, max(1, query.max_pages) + 1):
                    page = fetcher.page(
                        from_address=sender, to_address=receiver, page=page_no
                    )
                    if page is None:
                        truncated = True
                        break
                    for row in page.rows:
                        tx = taostats_row_to_tx(row, source)
                        if tx["amount_tao"] < query.min_tao:
                            continue
                        _merge_tx(transactions, tx, seen)
                    if not page.has_next:
                        break
                    if page_no == query.max_pages:
                        truncated = True

            # 3) 没有直连时找中间地址（只在"地址 × 地址"模式下有意义）
            if not transactions and query.max_hops >= 2:
                paths, path_notes, path_truncated = _walk_paths(
                    fetcher, source, targets[0], query
                )
                notes.extend(path_notes)
                truncated = truncated or path_truncated

    if fetcher.stop_reason == "rate_limit":
        notes.append("已达 Taostats 本地限速（5 次/分钟），本次只查到这里；等一分钟再点一次可继续。")
    elif fetcher.stop_reason == "budget":
        notes.append("本次检索的请求预算已用完，结果可能不完整；再点一次检索可以接着查。")

    summary = _summary(transactions, member_map)
    return {
        "source": source,
        "mode": query.mode,
        "group": {"kind": query.group_kind, "name": query.group_name}
        if query.mode == "group"
        else None,
        "targets": [
            {
                "address": member.address,
                "label": member.label,
                "group": member.group,
                "category": member.category,
            }
            for member in query.members
            if member.address != source
        ],
        "transactions": sorted(
            transactions, key=lambda tx: (tx["timestamp"] or "", tx["block_number"]), reverse=True
        ),
        "summary": summary,
        "paths": paths,
        "matched_addresses": len(summary),
        "stats": {
            "requests": fetcher.requests,
            "cached": fetcher.cache_hits,
            "pages": fetcher.pages,
            "truncated": truncated,
            "stop_reason": fetcher.stop_reason,
            "min_tao": query.min_tao,
            "max_hops": query.max_hops,
            "target_count": len(targets),
        },
        "notes": notes,
    }


def _oriented_step(
    forward_txs: list[dict[str, Any]],
    backward_txs: list[dict[str, Any]],
    node: str,
    next_node: str,
) -> dict[str, Any] | None:
    """取 ``node → next_node`` 这一跳的代表性转账（优先金额最大的正向那笔）。

    双向搜索时两边的取数方向相反：从 source 出发那一岸 ``node`` 是当前地址；
    从 target 出发那一岸反过来，``next_node`` 才在邻居堆的键上。所以两堆都传进来，
    先找「钱真的从 node 流向 next_node」的记录，再退回反向记录。
    """

    for bucket in (forward_txs, backward_txs):
        for tx in bucket:
            if tx.get("from") == node and tx.get("to") == next_node:
                return tx
    for bucket in (forward_txs, backward_txs):
        for tx in bucket:
            if tx.get("from") == next_node and tx.get("to") == node:
                return tx
    return _best(list(forward_txs) + list(backward_txs))


@dataclass
class _Side:
    """双向搜索里的一岸：带深度的待展开队列 + 父指针。

    队列里每个元素是 ``(地址, 距离起点的跳数)``，所以不需要再用额外的层级状态
    去推「当前在第几层」——以前那套 ``depth`` / ``advance()`` 在两岸交替推进时
    很容易把深度算错。
    """

    start: str
    limit: int
    layers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    parent: dict[str, str] = field(default_factory=dict)
    dist: dict[str, int] = field(default_factory=dict)
    queue: list[tuple[str, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.queue = [(self.start, 0)]
        self.dist = {self.start: 0}

    def can_step(self, fetcher: _Fetcher) -> bool:
        return bool(self.queue) and fetcher.requests < fetcher.budget

    def step(self, fetcher: _Fetcher, query: InterlinkQuery) -> bool:
        """展开队首一个地址；返回是否真的发了请求。"""

        if not self.can_step(fetcher):
            return False
        address, distance = self.queue.pop(0)
        neighbors = _neighbors(fetcher, address, pages=1, min_tao=query.min_tao)
        if not neighbors:
            return True
        self.layers[address] = dict(neighbors)
        if distance >= self.limit:
            return True
        ranked = sorted(
            neighbors,
            key=lambda item: sum(abs(tx["amount_tao"]) for tx in neighbors[item]),
            reverse=True,
        )
        for candidate in ranked[:MAX_INTERMEDIATES]:
            if candidate in self.dist:
                continue
            self.dist[candidate] = distance + 1
            self.parent[candidate] = address
            self.queue.append((candidate, distance + 1))
        return True

    @property
    def deepest(self) -> int:
        return max(self.dist.values(), default=0)


def _walk_paths(
    fetcher: _Fetcher,
    source: str,
    target: str,
    query: InterlinkQuery,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """从两端同时向外展开，找 ``source → … → target`` 的链路。

    原来是「1 跳查共同对手方、2 跳再从邻居里挑中转」两段硬编码，最多只能到 3 跳。
    改成双向分层 BFS 后任意深度都是同一套代码：两岸各展开一半层数，在中间相遇，
    再沿父指针回溯完整节点序列与逐跳转账。加层级不用再补一段「4 跳怎么写」。

    两岸**交替**展开而不是一侧走到底：Taostats 每分钟只给 5 次请求，一侧吃光预算
    之后另一侧就再也取不到数据，会误报「没找到通路」。
    """

    notes: list[str] = []
    max_hops = max(1, int(query.max_hops or DEFAULT_RELATION_DEPTH))
    if fetcher.requests >= fetcher.budget:
        return [], ["请求预算已用完，没能继续找中间地址；等一分钟或再点一次检索。"], True

    left_limit = max(1, (max_hops + 1) // 2)
    right_limit = max(1, max_hops - left_limit)
    left = _Side(source, left_limit)
    right = _Side(target, right_limit)

    # 两岸交替各发一次请求：Taostats 只给 5 次/分钟，一侧走到底会把预算吃光，
    # 另一侧连一页都取不到，于是误报「没找到通路」。
    while left.can_step(fetcher) or right.can_step(fetcher):
        for side in (left, right):
            if not side.can_step(fetcher):
                continue
            side.step(fetcher, query)

    truncated = fetcher.requests >= fetcher.budget
    if not left.layers or not right.layers:
        if truncated:
            notes.append(
                "本次请求预算只够展开一侧，没能拼链路；等一分钟再点一次「开始检索」，"
                "已取到的分页会缓存 5 分钟、不重复计费，会接着往更深处走。"
            )
        else:
            notes.append("没能取到双方转账流水，无法拼链路（可能是限速）。")
        return [], notes, True

    left_depth = left.dist
    right_depth = right.dist

    # 两岸相遇：同一个地址两侧都出现过，且两侧深度之和不超过 max_hops。
    meetings = [
        address
        for address in left_depth
        if address in right_depth
        and address not in (source, target)
        and left_depth[address] + right_depth[address] <= max_hops
    ]
    meetings.sort(key=lambda item: (left_depth[item] + right_depth[item], -left_depth[item]))

    if not meetings:
        reached = max(left.deepest, right.deepest)
        if truncated:
            notes.append(
                f"本次预算只展开到约 {reached} 跳，还没遇到共同地址；"
                "已取到的分页会缓存 5 分钟，再点一次「开始检索」会接着往更深处走。"
            )
        else:
            notes.append(
                f"在已取的流水范围内没找到 {max_hops} 跳以内的通路（各方只翻了最近一页），"
                "可以调大「翻页上限」或降低「最低 TAO」再试。"
            )
        return [], notes, truncated

    def chain_to(side: _Side, end: str) -> list[str]:
        chain = [end]
        current = end
        guard = 0
        while current != side.start and guard <= max_hops + 1:
            current = side.parent.get(current, "")
            if not current:
                return []
            chain.append(current)
            guard += 1
        chain.reverse()
        return chain if chain and chain[0] == side.start else []

    paths: list[dict[str, Any]] = []
    for meeting in meetings[:MAX_PATHS]:
        left_chain = chain_to(left, meeting)
        right_chain = chain_to(right, meeting)
        if not left_chain or not right_chain:
            continue
        nodes = left_chain + list(reversed(right_chain))[1:]
        if nodes[0] != source or nodes[-1] != target:
            continue
        if len(nodes) - 1 > max_hops:
            continue

        steps: list[dict[str, Any]] = []
        for position in range(len(nodes) - 1):
            current, following = nodes[position], nodes[position + 1]
            forward = left.layers.get(current, {}).get(following) or left.layers.get(
                following, {}
            ).get(current, [])
            backward = right.layers.get(current, {}).get(following) or right.layers.get(
                following, {}
            ).get(current, [])
            step = _oriented_step(forward, backward, current, following)
            if step is None:
                steps = []
                break
            steps.append(step)
        if len(steps) != len(nodes) - 1:
            continue

        paths.append(
            {
                "hops": len(nodes) - 1,
                "nodes": nodes,
                "steps": steps,
                "via": nodes[1] if len(nodes) > 2 else "",
                "evidence": sum(len(txs) for txs in left.layers.values())
                + sum(len(txs) for txs in right.layers.values()),
            }
        )

    if not paths:
        notes.append("找到了两岸的公共地址，但凑不齐逐跳明细；换组地址或调低「最低 TAO」再试。")
    elif truncated:
        notes.append(
            f"找到 {len(paths)} 条通路；本次预算已用完，更短或更远的链路可能还没查到，"
            "再点一次「开始检索」可以在缓存基础上继续。"
        )
    return paths, notes, truncated
