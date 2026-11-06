"""Deterministic breadth-first address relation traversal."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from .relation_config import (
    DEFAULT_RELATION_DEPTH,
    MAX_RELATION_DEPTH,
    Edge,
    LibraryEntry,
    LinkPolicy,
    RelationResult,
    ScanReport,
    TransferRecord,
)


SUSPECT_EXCHANGE_MIN_TRANSFERS = 20
SUSPECT_EXCHANGE_MIN_COUNTERPARTIES = 10
SUSPECT_EXCHANGE_MIN_GROSS_TAO = Decimal("1000")
SUSPECT_EXCHANGE_MIN_BALANCE_TAO = Decimal("100")
SUSPECT_EXCHANGE_LOOKBACK_DAYS = 30


def _recent_activity_for_exchange(
    address: str,
    activity: list[TransferRecord],
    *,
    now: datetime | None = None,
) -> tuple[list[TransferRecord], set[str], Decimal]:
    now = now or datetime.now(timezone.utc)
    recent: list[TransferRecord] = []
    for row in activity:
        if not row.timestamp:
            continue
        try:
            timestamp = datetime.fromisoformat(row.timestamp.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp >= now - timedelta(days=SUSPECT_EXCHANGE_LOOKBACK_DAYS):
                recent.append(row)
        except ValueError:
            continue
    counterparties = {
        (row.to_address if row.from_address == address else row.from_address)
        for row in recent
        if address in {row.from_address, row.to_address}
    }
    gross_tao = sum((row.amount_tao for row in recent), Decimal("0"))
    return recent, counterparties, gross_tao


def exchange_balance_candidates(
    transfers: Iterable[TransferRecord],
    library: Iterable[LibraryEntry] = (),
) -> list[str]:
    """Find high-activity addresses that need a live balance check first."""

    rows = _canonical_dedupe(transfers)
    entries = {entry.address: entry for entry in library}
    activity: dict[str, list[TransferRecord]] = defaultdict(list)
    for row in rows:
        activity[row.from_address].append(row)
        activity[row.to_address].append(row)
    candidates = []
    for address, records in activity.items():
        entry = entries.get(address)
        if entry and (
            entry.exchange_confirmed
            or entry.exchange_rejected
            or _entry_has(entry, "VALIDATOR")
            or _entry_has(entry, "PUBLIC")
        ):
            continue
        recent, counterparties, gross_tao = _recent_activity_for_exchange(address, records)
        if (
            len(recent) >= SUSPECT_EXCHANGE_MIN_TRANSFERS
            and len(counterparties) >= SUSPECT_EXCHANGE_MIN_COUNTERPARTIES
            and gross_tao >= SUSPECT_EXCHANGE_MIN_GROSS_TAO
        ):
            candidates.append(address)
    return sorted(candidates)


def _is_system(address: str, prefix: str) -> bool:
    return bool(prefix) and address.startswith(prefix)


def _oriented_path(seed: str, hops: list[TransferRecord]) -> list[dict[str, object]]:
    """把 BFS 路径整理成「起始地址 → … → 当前地址」的顺序。

    链上转账记录的 from/to 是真实资金方向，某一跳可能是对方打给当前地址，
    直接铺开会让路径看起来从别的地址起步。这里按行走顺序给出
    path_from → path_to，并用 direction 标注这一跳是转出（out）还是转入（in）。
    """

    steps: list[dict[str, object]] = []
    current = seed
    for transfer in hops:
        if transfer.from_address == current:
            next_address, direction = transfer.to_address, "out"
        elif transfer.to_address == current:
            next_address, direction = transfer.from_address, "in"
        else:
            # 路径断开（理论上不会发生）：保留已确认的跳，避免展示错误链路。
            break
        steps.append(
            {
                "tx_hash": transfer.tx_hash,
                "block_number": transfer.block_number,
                "from_address": transfer.from_address,
                "to_address": transfer.to_address,
                "amount_tao": str(transfer.amount_tao),
                "timestamp": transfer.timestamp,
                "edge_type": transfer.edge_type,
                "netuid": transfer.netuid,
                "path_from": current,
                "path_to": next_address,
                "direction": direction,
            }
        )
        current = next_address
    return steps


def _entry_categories(entry: LibraryEntry) -> set[str]:
    """一条地址库记录的全部分类（主分类 + 多分类），统一成大写。

    官方交易所可能是「大V + EXCHANGE」并存：主分类留给用户维护，交易所
    身份追加在多分类里，所以判定必须看全集。
    """

    values = {str(entry.category or "").upper()}
    values.update(str(value).upper() for value in entry.categories)
    return {value for value in values if value}


def _entry_has(entry: LibraryEntry, name: str) -> bool:
    return name.upper() in _entry_categories(entry)


def terminal_roles(library: Iterable[LibraryEntry]) -> dict[str, tuple[str, ...]]:
    """哪些地址是「关联终点」以及各自身份（交易所/验证者/公共地址/系统）。

    判定口径（与 discover 完全一致，这里抽出来是为了让历史结果校正
    复用同一套规则，避免两处写法漂移）：

    - 交易所：``EXCHANGE`` 分类且已人工确认；或人工在标签/备注里写明「交易所」
      的（未确认的交易所候选不算，避免把候选误当终点剪掉线索）。
    - 验证者：``VALIDATOR``（人工标记或官方名单同步）。
    - 公共地址：``PUBLIC``（人工标记或内置固定名单）。
    - 「系统」分类的地址。

    这些地址本身仍会作为关联地址出现，它自己的收款方/付款方不再进入结果。
    同一地址可能同时命中多类（例如「大V + EXCHANGE」）。
    """

    roles: dict[str, set[str]] = {}
    for entry in library:
        address = str(getattr(entry, "address", "") or "").strip()
        if not address:
            continue
        categories = _entry_categories(entry)
        named: set[str] = set()
        if "EXCHANGE" in categories and entry.exchange_confirmed:
            named.add("EXCHANGE")
        if "VALIDATOR" in categories:
            named.add("VALIDATOR")
        if "PUBLIC" in categories:
            named.add("PUBLIC")
        if not named and not entry.exchange_rejected and (
            "交易所" in (entry.note or "") or "交易所" in (entry.label or "")
        ):
            named.add("EXCHANGE")
        if (
            "系统" in (entry.categories or ())
            or "SYSTEM" in (entry.categories or ())
            or str(entry.category or "") in {"系统", "SYSTEM"}
        ):
            named.add("SYSTEM")
        if named:
            roles.setdefault(address, set()).update(named)
    return {address: tuple(sorted(names)) for address, names in roles.items()}


def terminal_addresses(library: Iterable[LibraryEntry]) -> set[str]:
    """哪些地址是「关联终点」：查到即截止，不再向下扩展其转账。"""

    return set(terminal_roles(library))


# 强度档位：4 极强 / 3 强 / 2 中 / 1 弱 / 0 未知。
_STRENGTH_LEVELS = {4: "极强", 3: "强", 2: "中", 1: "弱", 0: "未知"}
_STRENGTH_SCORES = {4: 100, 3: 86, 2: 72, 1: 52, 0: 24}


def _amount_tier(amount: Decimal, policy: LinkPolicy) -> int:
    if amount >= policy.extreme_tao:
        return 4
    if amount >= policy.strong_tao:
        return 3
    if amount >= policy.medium_tao:
        return 2
    if amount > 0:
        return 1
    return 0


def _strength_for(
    *,
    amount: Decimal,
    tx_count: int,
    depth: int,
    policy: LinkPolicy,
    book_pair_tao: Decimal | None,
) -> tuple[str, int, str]:
    """把金额事实换成「极强 / 强 / 中 / 弱」。

    规则（全部可解释、可复算）：

    1. 累计金额落到哪一档就是哪一档：≥1000 极强、≥100 强、≥30 中、其余弱。
    2. 笔数加权：累计已达到「弱」以上且往来 ≥3 笔，升一档（极强封顶）。
    3. 间接关联（≥2 跳）降一档，最低到「弱」——两跳之外的地址不该和直接
       对手方同档。
    4. 没有直接金额可比时（例如只通过库关系排在一起），用所属两个地址库的
       历史累计规模补一个很小的加分，只影响同档内排序，不改变档位。
    """

    tier = _amount_tier(amount, policy)
    notes: list[str] = []
    if amount > 0:
        notes.append(f"累计往来 {amount.normalize():f} TAO")
    if tx_count:
        notes.append(f"{tx_count} 笔")
    if tier >= 1 and tx_count >= 3 and tier < 4:
        tier += 1
        notes.append("往来 3 笔以上升一档")
    if depth >= 2:
        tier = max(1, tier - 1)
        notes.append(f"{depth} 跳间接关联降一档")
    score = _STRENGTH_SCORES[tier]
    if book_pair_tao and book_pair_tao > 0:
        # 最多 +8 分：同档内「两个库之间本来就走得多」的地址排前面。
        score = min(score + 8, 100)
        notes.append(f"所属地址库间累计 {book_pair_tao.normalize():f} TAO")
    return _STRENGTH_LEVELS[tier], score, "；".join(notes) or "无可比金额"


def _result_categories(row: RelationResult) -> set[str]:
    categories = {str(row.library_category or "").upper()}
    categories.update(str(value).upper() for value in row.library_categories)
    return {value for value in categories if value}


def _is_exchange_result(row: RelationResult) -> bool:
    """结果行是不是「已确认 / 官方锁定」的交易所——排序时永远排最前。"""

    return "EXCHANGE" in _result_categories(row) and bool(
        row.exchange_confirmed or row.exchange_locked
    )


def _is_public_result(row: RelationResult) -> bool:
    """人工标记的公共地址：与交易所同性质，排在交易所之后、其它地址之前。"""

    return "PUBLIC" in _result_categories(row)


def _role_for_address(
    address: str,
    entry: LibraryEntry | None,
    activity: list[TransferRecord],
    balance_tao: Decimal | None = None,
    exchange_related: bool = False,
) -> tuple[str, str]:
    """Return a conservative public label from available address evidence.

    The current index contains transfer events, not a complete balance or
    subnet purchase ledger. Automatic labels therefore remain explicitly
    qualified and never claim ownership or identity.
    """

    if entry and _entry_has(entry, "EXCHANGE"):
        if entry.exchange_rejected:
            return "已人工否定交易所", "用户已人工否定该交易所候选，当前不会进入交易所过滤集合。"
        if entry.exchange_confirmed:
            label = entry.label.strip() or "交易所地址"
            return label, "地址库明确标记为交易所地址。"
        return "疑似交易所（待确认）", "交易所候选尚未人工确认，交易所过滤不可用。"
    if entry and _entry_has(entry, "VALIDATOR"):
        label = entry.label.strip() or "验证者地址"
        return label, "地址库明确标记为验证者地址。"
    if entry and _entry_has(entry, "PUBLIC"):
        label = entry.label.strip() or "公共地址"
        return label, "地址库人工标记为公共地址：此类地址会和大量不确定钱包往来，作为关联终点不再向下扩展。"

    if exchange_related:
        return (
            "疑似中转钱包（交易所关联）",
            "该地址与已确认的交易所公共地址存在直接转账；当前查询数据包含 Balances.Transfer 与子网 stake 转移，仍未覆盖质押、解质押和购买等完整子网交易语义。余额高低不影响此线索，但这不是全链历史中不存在子网交易的证明。",
        )

    if not activity:
        return "", ""
    counterparties = {
        (row.to_address if row.from_address == address else row.from_address)
        for row in activity
        if address in {row.from_address, row.to_address}
    }
    recent_activity, recent_counterparties, gross_tao = _recent_activity_for_exchange(
        address, activity
    )
    gross_tao = sum((row.amount_tao for row in recent_activity), Decimal("0"))
    transfer_count = len(recent_activity)
    # The current transfer-only index cannot prove current wallet balance or
    # subnet staking history. This is therefore a review hint, never a filter
    # decision. A confirmed EXCHANGE library entry is the only filterable one.
    if (
        balance_tao is not None
        and balance_tao >= SUSPECT_EXCHANGE_MIN_BALANCE_TAO
        and transfer_count >= SUSPECT_EXCHANGE_MIN_TRANSFERS
        and len(recent_counterparties) >= SUSPECT_EXCHANGE_MIN_COUNTERPARTIES
        and gross_tao >= SUSPECT_EXCHANGE_MIN_GROSS_TAO
    ):
        return (
            "疑似交易所（待确认）",
            f"近 {SUSPECT_EXCHANGE_LOOKBACK_DAYS} 天观察到 {transfer_count} 笔转账、{len(recent_counterparties)} 个对手方、可观察转账总额 {gross_tao} TAO，当前余额约 {balance_tao} TAO；子网质押/解质押事件尚未纳入自动核验，需人工确认。",
        )
    if (
        transfer_count >= SUSPECT_EXCHANGE_MIN_TRANSFERS
        and len(recent_counterparties) >= SUSPECT_EXCHANGE_MIN_COUNTERPARTIES
        and gross_tao >= SUSPECT_EXCHANGE_MIN_GROSS_TAO
        and balance_tao is not None
        and balance_tao < SUSPECT_EXCHANGE_MIN_BALANCE_TAO
    ):
        return (
            "排除交易所疑似（余额偏低）",
            f"近 {SUSPECT_EXCHANGE_LOOKBACK_DAYS} 天交易活动较高，但当前账户可用余额约 {balance_tao} TAO，低于 {SUSPECT_EXCHANGE_MIN_BALANCE_TAO} TAO 门槛；未标记为疑似交易所。",
        )
    if (
        transfer_count >= SUSPECT_EXCHANGE_MIN_TRANSFERS
        and len(recent_counterparties) >= SUSPECT_EXCHANGE_MIN_COUNTERPARTIES
        and gross_tao >= SUSPECT_EXCHANGE_MIN_GROSS_TAO
        and balance_tao is None
    ):
        return (
            "交易所判断待核余额",
            "转账活动符合高频候选条件，但无法取得当前账户余额，因此不会标记为疑似交易所。",
        )
    if len(activity) >= 2 and len(counterparties) >= 2:
        return (
            "疑似中转钱包",
            "当前数据范围内仅观察到转账或子网 stake 转移记录；尚未接入子网质押、解质押和购买事件，不能据此确认其没有这些记录。",
        )
    return "", ""


def _canonical_dedupe(transfers: Iterable[TransferRecord]) -> list[TransferRecord]:
    """Collapse exact repeated versions of a public transfer record."""

    seen: set[tuple[str, str, str, str, int]] = set()
    result: list[TransferRecord] = []
    for transfer in transfers:
        normalized = TransferRecord(
            tx_hash=transfer.tx_hash.strip(),
            block_number=transfer.block_number,
            from_address=transfer.from_address.strip(),
            to_address=transfer.to_address.strip(),
            amount_tao=transfer.amount_tao,
            block_hash=transfer.block_hash.strip(),
            timestamp=transfer.timestamp.strip(),
            edge_type=transfer.edge_type,
            netuid=transfer.netuid,
            hotkey=transfer.hotkey,
        )
        if normalized.tx_hash:
            identity = (
                normalized.tx_hash.lower(),
                normalized.from_address,
                normalized.to_address,
                str(normalized.amount_tao),
                normalized.block_number,
            )
            if identity in seen:
                continue
            seen.add(identity)
        result.append(normalized)
    return result


def discover(
    seed: str,
    transfers: Iterable[TransferRecord],
    *,
    library: Iterable[LibraryEntry] = (),
    max_depth: int = DEFAULT_RELATION_DEPTH,
    policy: LinkPolicy | None = None,
    data_cutoff_block: int | None = None,
    freshness: str = "UNKNOWN_EXPORT",
    warnings: Iterable[str] = (),
    excluded_addresses: Iterable[str] = (),
    removed_addresses: Iterable[str] = (),
    balances: dict[str, Decimal | None] | None = None,
    book_pair_stats: dict[tuple[str, str], Decimal] | None = None,
) -> ScanReport:
    """Traverse transfer edges at most ``max_depth`` hops from the seed.

    Traversal follows every non-system transfer so a low first hop is not
    silently dropped. The displayed graph only contains first-hop transfers
    and subsequent transfers above the edge amount threshold. Every relation
    must meet the aggregate threshold; low-amount direct transfers are counted
    but omitted from the reported graph.    """

    seed = seed.strip()
    if not seed:
        raise ValueError("seed must not be empty")
    if max_depth < 0 or max_depth > MAX_RELATION_DEPTH:
        raise ValueError(f"max_depth must be between 0 and {MAX_RELATION_DEPTH}")
    policy = policy or LinkPolicy()
    transfer_rows = _canonical_dedupe(transfers)
    excluded = {address.strip() for address in excluded_addresses if address.strip()}
    removed = {address.strip() for address in removed_addresses if address.strip()}
    removed_contact_notes: list[str] = []
    if removed:
        # 手动移除关联的延伸规则：与被移除地址发生过直接转账的其他地址
        # 一并视为「同一条线索」，在 BFS 之前就排除——这样通过它们中转的
        # 下游地址也会级联剪掉，而不是只剪路径恰好经过被移除地址的分支。
        pruned_contacts: set[str] = set()
        for transfer in transfer_rows:
            source = transfer.from_address.strip()
            target = transfer.to_address.strip()
            if source in removed and target not in removed and target != seed:
                pruned_contacts.add(target)
            elif target in removed and source not in removed and source != seed:
                pruned_contacts.add(source)
        excluded.update(pruned_contacts)
        if pruned_contacts:
            removed_contact_notes.append(
                f"已移除地址与 {len(pruned_contacts)} 个其他关联地址存在直接转账交集，"
                "这些地址及其下游线索也已一并移除。"
            )
    library_map: dict[str, LibraryEntry] = {}
    for entry in library:
        address = entry.address.strip()
        current = library_map.get(address)
        entry_system = _entry_categories(entry) & {"EXCHANGE", "VALIDATOR", "PUBLIC"}
        # 同一地址出现多条记录时，系统分类（交易所/验证者/公共地址）优先于
        # 用户分类，否则过滤公共地址会失效。
        if current is None or (
            entry_system
            and not (_entry_categories(current) & {"EXCHANGE", "VALIDATOR", "PUBLIC"})
        ):
            library_map[address] = LibraryEntry(
                address=address,
                label=entry.label,
                category=entry.category,
                note=entry.note,
                source=entry.source,
                exchange_confirmed=entry.exchange_confirmed,
                exchange_rejected=entry.exchange_rejected,
                categories=entry.categories,
                book=getattr(entry, "book", "") or "",
            )

    # 关联终点：已人工确认的交易所地址、验证者地址（人工标记或官方名单
    # 同步）、人工在标签/备注里写明「交易所」的地址、人工标记的公共地址
    # （PUBLIC），以及被分到「系统」分类的地址。它们仍会作为关联地址出现
    # 在结果里，但不再向下扩展其转账。
    # 验证者同理：热/冷钱包每天都在押注、解押、子网质押，不截止会把结果
    # 灌满噪音，也白白消耗 Taostats 请求额度。
    # 判定逻辑抽到 terminal_addresses()，历史结果校正复用同一份规则。
    terminal_exchange_addresses = terminal_addresses(library_map.values())

    if seed in excluded:
        return ScanReport(
            seed=seed,
            max_depth=max_depth,
            data_cutoff_block=data_cutoff_block,
            freshness=freshness,
            policy=policy,
            seed_is_in_library=seed in library_map,
            seed_library_matches=[library_map[seed]] if seed in library_map else [],
            warnings=list(dict.fromkeys([
                *warnings,
                "起始地址命中当前过滤地址库，已停止从该地址扩展。",
            ])),
        )

    graph: dict[str, list[TransferRecord]] = defaultdict(list)
    activity: dict[str, list[TransferRecord]] = defaultdict(list)
    for transfer in transfer_rows:
        graph[transfer.from_address].append(transfer)
        activity[transfer.from_address].append(transfer)
        activity[transfer.to_address].append(transfer)
        if transfer.to_address != transfer.from_address:
            graph[transfer.to_address].append(transfer)

    exchange_related_addresses: set[str] = set()
    for transfer in transfer_rows:
        if transfer.from_address in terminal_exchange_addresses:
            if transfer.to_address not in terminal_exchange_addresses:
                exchange_related_addresses.add(transfer.to_address)
        elif transfer.to_address in terminal_exchange_addresses:
            exchange_related_addresses.add(transfer.from_address)

    distances: dict[str, int] = {seed: 0}
    paths: dict[str, list[TransferRecord]] = {seed: []}
    queue: deque[str] = deque([seed])
    retained: dict[tuple[str, str, str, int], Edge] = {}
    missing_hash_rows = 0

    def retained_edge(transfer: TransferRecord, depth: int, neighbor: str, transitive: bool = False) -> Edge:
        return Edge(
            tx_hash=transfer.tx_hash,
            block_number=transfer.block_number,
            from_address=transfer.from_address,
            to_address=transfer.to_address,
            amount_tao=transfer.amount_tao,
            block_hash=transfer.block_hash,
            timestamp=transfer.timestamp,
            depth=depth,
            reason="transit_edge" if transitive else ("direct_seed" if depth == 1 else "qualified_edge"),
            related_address=neighbor,
            edge_type=transfer.edge_type,
            netuid=transfer.netuid,
            hotkey=transfer.hotkey,
        )

    while queue:
        current = queue.popleft()
        depth = distances[current]
        if depth >= max_depth:
            continue
        if current in terminal_exchange_addresses:
            continue
        for transfer in graph.get(current, []):
            if policy.exclude_system_accounts and (
                _is_system(transfer.from_address, policy.system_prefix)
                or _is_system(transfer.to_address, policy.system_prefix)
            ):
                continue
            if transfer.from_address == current:
                if not policy.include_outgoing:
                    continue
                neighbor = transfer.to_address
            elif transfer.to_address == current:
                if not policy.include_incoming:
                    continue
                neighbor = transfer.from_address
            else:
                continue
            if neighbor == current:
                continue
            if neighbor in excluded:
                continue
            material = depth == 0 or transfer.amount_tao >= policy.min_edge_tao
            # Small indirect transfers are exploration hints only; they do not
            # become retained graph edges and cannot introduce a new path layer.
            if not material and depth > 0:
                continue
            if not transfer.tx_hash:
                missing_hash_rows += 1
                qualifier = f"row:{missing_hash_rows}"
            else:
                qualifier = transfer.tx_hash
            key = (transfer.from_address, transfer.to_address, qualifier, transfer.block_number)
            if material and key not in retained:
                retained[key] = retained_edge(transfer, depth + 1, neighbor)
            if (
                neighbor not in distances
                and neighbor not in excluded
                and neighbor not in terminal_exchange_addresses
            ):
                distances[neighbor] = depth + 1
                paths[neighbor] = paths[current] + [transfer]
                queue.append(neighbor)
            elif neighbor in terminal_exchange_addresses and neighbor not in distances:
                distances[neighbor] = depth + 1
                paths[neighbor] = paths[current] + [transfer]

    # A low transfer can create a candidate but not carry a path. Every retained
    # edge is therefore above the threshold, unless it starts directly at seed.
    # Direct low transfers remain visible so they can be explicitly reviewed.
    frontier = [seed]
    visible: dict[str, int] = {seed: 0}
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        next_frontier: list[str] = []
        for current in frontier:
            for edge in retained.values():
                if edge.depth != depth:
                    continue
                if current not in {edge.from_address, edge.to_address}:
                    continue
                target = edge.to_address if edge.from_address == current else edge.from_address
                if target == seed or target in visible:
                    continue
                visible[target] = depth
                next_frontier.append(target)
        frontier = next_frontier

    edges = [
        edge
        for edge in retained.values()
        if edge.depth <= max_depth and (edge.depth == 1 or edge.amount_tao >= policy.min_edge_tao)
    ]
    by_relation: dict[str, list[Edge]] = defaultdict(list)
    for edge in edges:
        for endpoint in (edge.from_address, edge.to_address):
            if endpoint != seed:
                by_relation[endpoint].append(edge)

    related: list[RelationResult] = []
    pruned_through_terminal = 0
    for address, edge_rows in by_relation.items():
        total = sum((edge.amount_tao for edge in edge_rows), Decimal("0"))
        if total < policy.min_total_tao:
            continue
        if address not in visible:
            continue
        path = paths.get(address)
        if not path:
            continue
        # 关联终点（交易所 / 系统分类）不再向下扩展，所以正常不可能出现
        # 「路径中途经过终点」的地址；万一出现（例如终点是本次查询期间新标记的），
        # 这种地址只是交易所的下游噪音，直接剪掉，不当作线索展示。
        if any(
            hop.from_address in terminal_exchange_addresses
            or hop.to_address in terminal_exchange_addresses
            for hop in path[:-1]
        ):
            pruned_through_terminal += 1
            continue
        entry = library_map.get(address)
        address_role, role_reason = _role_for_address(
            address,
            entry,
            activity.get(address, []),
            (balances or {}).get(address),
            address in exchange_related_addresses,
        )
        unique_tx = {edge.tx_hash for edge in edge_rows if edge.tx_hash}
        incoming = [edge for edge in edge_rows if edge.to_address == address]
        outgoing = [edge for edge in edge_rows if edge.from_address == address]
        # 库间规模：两个地址分属不同地址库时，用两库历史累计规模做同档内排序。
        pair_tao = None
        seed_book = (library_map.get(seed).book if library_map.get(seed) else "") or ""
        address_book = (entry.book if entry is not None else "") or ""
        if book_pair_stats and seed_book and address_book:
            pair_tao = book_pair_stats.get(tuple(sorted([seed_book, address_book])))
        strength, strength_score, strength_reason = _strength_for(
            amount=total,
            tx_count=len(unique_tx) or len(edge_rows),
            depth=visible.get(address, len(path)),
            policy=policy,
            book_pair_tao=pair_tao,
        )
        # 起始地址到该地址的逐笔路径：BFS 走过的每一跳原始转账记录。
        # 每一跳额外给出「按起始地址方向」的 path_from → path_to：转账本身可能是
        # 对方转入，直接照抄 from/to 会让路径看起来从别的地址开始。
        path_transfers = _oriented_path(seed, path)
        related.append(
            RelationResult(
                address=address,
                depth=visible.get(address, len(path)),
                incoming_tao=sum((edge.amount_tao for edge in incoming), Decimal("0")),
                outgoing_tao=sum((edge.amount_tao for edge in outgoing), Decimal("0")),
                total_tao=total,
                incoming_count=len(incoming),
                outgoing_count=len(outgoing),
                tx_count=len(unique_tx) or len(edge_rows),
                first_block=min(edge.block_number for edge in edge_rows),
                last_block=max(edge.block_number for edge in edge_rows),
                balance_tao=(balances or {}).get(address),
                match=entry is not None,
                library_label=entry.label if entry else "",
                library_category=entry.category if entry else "",
                library_note=entry.note if entry else "",
                library_source=entry.source if entry else "",
                library_categories=entry.categories if entry else (),
                address_role=address_role,
                role_reason=role_reason,
                exchange_confirmed=entry.exchange_confirmed if entry else False,
                exchange_rejected=entry.exchange_rejected if entry else False,
                exchange_locked=bool(
                    entry
                    and str(entry.source or "").startswith(("taostats:", "builtin:"))
                    and _entry_has(entry, "EXCHANGE")
                ),
                stake_transfer_count=sum(
                    1 for edge in edge_rows if edge.edge_type == "stake_transfer"
                ),
                strength=strength,
                strength_score=strength_score,
                strength_reason=strength_reason,
                path_transfers=path_transfers,
            )
        )
    # 排序：交易所最前，公共地址其次（同属「汇聚型」地址），
    # 然后按关联强度（分数高的在前），再按跳数与金额。
    related.sort(
        key=lambda row: (
            0 if _is_exchange_result(row) else (1 if _is_public_result(row) else 2),
            -row.strength_score,
            row.depth,
            -row.total_tao,
            row.address,
        )
    )
    seed_entry = library_map.get(seed)
    final_warnings = list(warnings)
    final_warnings.extend(removed_contact_notes)
    visible_terminal_exchanges = terminal_exchange_addresses & set(visible)
    if visible_terminal_exchanges:
        final_warnings.append(
            f"已识别 {len(visible_terminal_exchanges)} 个交易所地址（人工确认，或标签/备注中写明「交易所」）；"
            "这些地址作为关联终点，不再向下扩展其转账。"
        )
    if pruned_through_terminal:
        final_warnings.append(
            f"已剪除 {pruned_through_terminal} 个只经由交易所/系统地址连通的地址；"
            "这些地址属于交易所的下游噪音，不作为线索展示。"
        )
    if missing_hash_rows:
        final_warnings.append(
            f"{missing_hash_rows} matching rows have no tx_hash; exact deduplication was limited."
        )
    if not transfer_rows:
        final_warnings.append("No transfer records supplied; no relations can be discovered.")
    if any(not row.tx_hash for row in edges):
        final_warnings.append("Some displayed edges have no tx_hash and are not independently auditable.")
    stake_edges = [edge for edge in edges if edge.edge_type == "stake_transfer"]
    if stake_edges:
        final_warnings.append(
            f"本次纳入 {len(stake_edges)} 条子网 stake 转移边（SubtensorModule.StakeTransferred）；"
            "金额按事件的 TAO 等值计价，边类型与子网 ID 在导出数据中单独标识。"
        )
    return ScanReport(
        seed=seed,
        max_depth=max_depth,
        data_cutoff_block=data_cutoff_block,
        freshness=freshness,
        policy=policy,
        edges=sorted(
            edges,
            key=lambda e: (e.depth, e.block_number, e.tx_hash, e.from_address),
        ),
        related=related,
        library_matches=[row for row in related if row.match],
        seed_is_in_library=seed_entry is not None,
        seed_library_matches=[seed_entry] if seed_entry else [],
        warnings=list(dict.fromkeys(final_warnings)),
    )


def relation_edges(report: ScanReport, address: str) -> list[Edge]:
    return [
        edge
        for edge in report.edges
        if address in {edge.from_address, edge.to_address}
    ]
