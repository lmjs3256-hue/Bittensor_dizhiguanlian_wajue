"""Deterministic breadth-first address relation traversal."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from .relation_config import (
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
            or entry.category == "VALIDATOR"
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

    if entry and entry.category == "EXCHANGE":
        if entry.exchange_rejected:
            return "已人工否定交易所", "用户已人工否定该交易所候选，当前不会进入交易所过滤集合。"
        if entry.exchange_confirmed:
            label = entry.label.strip() or "交易所地址"
            return label, "地址库明确标记为交易所地址。"
        return "疑似交易所（待确认）", "交易所候选尚未人工确认，交易所过滤不可用。"
    if entry and entry.category == "VALIDATOR":
        label = entry.label.strip() or "验证者地址"
        return label, "地址库明确标记为验证者地址。"

    if exchange_related:
        return (
            "疑似中转钱包（交易所关联）",
            "该地址与已确认的交易所公共地址存在直接转账；当前查询数据只包含 Balances.Transfer，未观察到子网交易语义。余额高低不影响此线索，但这不是全链历史中不存在子网交易的证明。",
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
            "当前数据范围内仅观察到转账记录；尚未接入子网质押、解质押和购买事件，不能据此确认其没有这些记录。",
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
    max_depth: int = 3,
    policy: LinkPolicy | None = None,
    data_cutoff_block: int | None = None,
    freshness: str = "UNKNOWN_EXPORT",
    warnings: Iterable[str] = (),
    excluded_addresses: Iterable[str] = (),
    balances: dict[str, Decimal | None] | None = None,
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
    if max_depth < 0 or max_depth > 3:
        raise ValueError("max_depth must be between 0 and 3")
    policy = policy or LinkPolicy()
    transfer_rows = _canonical_dedupe(transfers)
    excluded = {address.strip() for address in excluded_addresses if address.strip()}
    library_map: dict[str, LibraryEntry] = {}
    for entry in library:
        address = entry.address.strip()
        current = library_map.get(address)
        if current is None or entry.category == "BLACKLIST":
            library_map[address] = LibraryEntry(
                address=address,
                label=entry.label,
                category=entry.category,
                note=entry.note,
                source=entry.source,
                exchange_confirmed=entry.exchange_confirmed,
                exchange_rejected=entry.exchange_rejected,
            )

    terminal_exchange_addresses = {
        address
        for address, entry in library_map.items()
        if entry.category == "EXCHANGE" and entry.exchange_confirmed
    }

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
    for address, edge_rows in by_relation.items():
        total = sum((edge.amount_tao for edge in edge_rows), Decimal("0"))
        if total < policy.min_total_tao:
            continue
        if address not in visible:
            continue
        path = paths.get(address)
        if not path:
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
                match=entry is not None,
                library_label=entry.label if entry else "",
                library_category=entry.category if entry else "",
                library_note=entry.note if entry else "",
                library_source=entry.source if entry else "",
                address_role=address_role,
                role_reason=role_reason,
                exchange_confirmed=entry.exchange_confirmed if entry else False,
                exchange_rejected=entry.exchange_rejected if entry else False,
            )
        )
    related.sort(key=lambda row: (row.depth, -row.total_tao, row.address))
    seed_entry = library_map.get(seed)
    final_warnings = list(warnings)
    visible_terminal_exchanges = terminal_exchange_addresses & set(visible)
    if visible_terminal_exchanges:
        final_warnings.append(
            f"已识别 {len(visible_terminal_exchanges)} 个已确认交易所地址；交易所公共地址作为关联终点，不继续扩展其下游转账。"
        )
    if missing_hash_rows:
        final_warnings.append(
            f"{missing_hash_rows} matching rows have no tx_hash; exact deduplication was limited."
        )
    if not transfer_rows:
        final_warnings.append("No transfer records supplied; no relations can be discovered.")
    if any(not row.tx_hash for row in edges):
        final_warnings.append("Some displayed edges have no tx_hash and are not independently auditable.")
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
