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

RAO_PER_TAO = 1_000_000_000
# 与 taostats.LOCAL_REQUESTS_PER_MINUTE 对齐；单次检索最多花这么多次请求。
DEFAULT_REQUEST_BUDGET = taostats.LOCAL_REQUESTS_PER_MINUTE
# 找 2 跳以上时，从第一层邻居里挑多少个"最像中转"的地址继续查。
MAX_INTERMEDIATES = 3


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
    max_hops: int = 3
    max_pages: int = 3
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
                paths, path_notes, path_truncated = _search_paths(
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


def _search_paths(
    fetcher: _Fetcher, source: str, target: str, query: InterlinkQuery
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """A 与 B 没有直接转账时，用双方邻居找 1–2 跳链路。"""
    notes: list[str] = []
    paths: list[dict[str, Any]] = []
    if fetcher.requests >= fetcher.budget:
        return paths, ["请求预算已用完，没能继续找中间地址；稍后再点一次检索。"], True

    left = _neighbors(fetcher, source, pages=1, min_tao=query.min_tao)
    right = _neighbors(fetcher, target, pages=1, min_tao=query.min_tao)
    if not left or not right:
        notes.append("没能取到双方转账流水，无法拼链路（可能是请求预算或限速）。")
        return paths, notes, True

    # 1 跳：共同对手方
    shared = [address for address in left if address in right and address not in (source, target)]
    shared.sort(key=lambda address: len(left[address]) + len(right[address]), reverse=True)
    for middle in shared[:MAX_INTERMEDIATES]:
        step_one = _best(left[middle])
        step_two = _best(right[middle])
        if not step_one or not step_two:
            continue
        if step_one["direction"] == "out" and step_two["direction"] == "in":
            first, second = step_one, step_two
        elif step_one["direction"] == "in" and step_two["direction"] == "out":
            first, second = step_two, step_one
        else:
            first, second = step_one, step_two
        paths.append(
            {
                "hops": 2,
                "nodes": [source, middle, target],
                "steps": [first, second],
                "via": middle,
                "evidence": len(left.get(middle, [])) + len(right.get(middle, [])),
            }
        )

    if paths or query.max_hops < 3:
        return paths, notes, False

    # 2 跳：从 source 的邻居里挑几个"中转嫌疑"最大的，再各自展开一页找 target 的邻居
    candidates = [
        address
        for address in left
        if address not in (source, target) and address not in shared
    ]
    candidates.sort(
        key=lambda address: sum(abs(tx["amount_tao"]) for tx in left[address]), reverse=True
    )
    depth_two_truncated = False
    for middle in candidates[:MAX_INTERMEDIATES]:
        if fetcher.requests >= fetcher.budget:
            depth_two_truncated = True
            break
        hops = _neighbors(fetcher, middle, pages=1, min_tao=query.min_tao)
        common = [address for address in hops if address in right and address not in (source, target)]
        if not common:
            continue
        common.sort(key=lambda address: len(hops[address]), reverse=True)
        bridge = common[0]
        step_one = _best(left[middle])
        step_two = _best(hops[bridge])
        step_three = _best(right[bridge])
        if not step_one or not step_two or not step_three:
            continue
        paths.append(
            {
                "hops": 3,
                "nodes": [source, middle, bridge, target],
                "steps": [step_one, step_two, step_three],
                "via": middle,
                "evidence": len(left.get(middle, [])) + len(hops.get(bridge, [])) + len(right.get(bridge, [])),
            }
        )
        if len(paths) >= MAX_INTERMEDIATES:
            break

    if depth_two_truncated:
        notes.append("找 2 跳链路时请求预算用完，可能有更远的链路没查到。")
    if not paths:
        notes.append("在已取的流水范围内没有找到共同对手方（只覆盖了双方最近一页转账）。")
    return paths, notes, depth_two_truncated
