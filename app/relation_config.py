"""Configuration and deterministic association rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


# 关联层级上限与默认值：默认仍按 3 级扫（结果最干净、请求最少），
# 需要顺藤摸瓜时最高可以放到 10 级。规则（单边门槛、累计门槛、
# 交易所/公共地址截止、BFS 最短路径）在每一级都一样，层级只决定向下走多少跳。
MAX_RELATION_DEPTH = 10
DEFAULT_RELATION_DEPTH = 3


@dataclass(frozen=True)
class LinkPolicy:
    """Controls which transfers become graph edges.

    ``min_edge_tao`` applies to every hop after the first one;
    ``min_direct_tao`` applies to the first hop (seed -> neighbour). The latter
    defaults to ``0`` so existing scans keep their historical behaviour; raising
    it stops a dust transfer from manufacturing a first-level association.
    A reported relation also requires the sum of all retained edges for that
    address to meet ``min_total_tao``.
    """

    min_edge_tao: Decimal = Decimal("1")
    min_total_tao: Decimal = Decimal("1")
    include_incoming: bool = True
    include_outgoing: bool = True
    exclude_system_accounts: bool = True
    system_prefix: str = "5EYCAe5"
    # 关联强度分档阈值（累计金额，TAO）：≥extreme 极强，≥strong 强，
    # ≥medium 中，其余弱；间接关联（≥2 跳）再降一档。
    medium_tao: Decimal = Decimal("30")
    strong_tao: Decimal = Decimal("100")
    extreme_tao: Decimal = Decimal("1000")
    # 1 级关联（起始地址直接往来的那一笔转账）最低金额，0 = 不限（历史行为）。
    # 故意放在字段末尾：历史代码里有 LinkPolicy(Decimal("1"), Decimal("0.1"))
    # 这种位置参数写法，插在中间会静默改掉 min_total_tao。
    min_direct_tao: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.min_edge_tao < 0:
            raise ValueError("min_edge_tao must be >= 0")
        if self.min_total_tao < 0:
            raise ValueError("min_total_tao must be >= 0")
        if self.min_direct_tao < 0:
            raise ValueError("min_direct_tao must be >= 0")
        if not self.include_incoming and not self.include_outgoing:
            raise ValueError("at least one transfer direction must be enabled")
        if not self.system_prefix:
            raise ValueError("system_prefix must not be empty")
        if not (0 <= self.medium_tao <= self.strong_tao <= self.extreme_tao):
            raise ValueError("强度阈值必须满足 0 <= medium <= strong <= extreme")

    def edge_floor(self, depth: int) -> Decimal:
        """这一跳转账必须达到的最低金额，BFS 与 Taostats 爬取共用同一口径。

        ``depth`` 是**当前节点**距起始地址的跳数：0 表示正从起始地址往外走
        （产生的是 1 级关联边），>=1 表示已经在中转地址上（2 级及更深）。
        两边都调这个方法，避免只改一处造成「爬到的记录」和「算出来的关联」
        对不上。
        """

        return self.min_direct_tao if depth <= 0 else self.min_edge_tao

    @classmethod
    def from_dict(cls, values: dict[str, Any] | None) -> "LinkPolicy":
        values = values or {}
        try:
            return cls(
                min_edge_tao=Decimal(str(values.get("min_edge_tao", "1"))),
                min_total_tao=Decimal(str(values.get("min_total_tao", "1"))),
                include_incoming=bool(values.get("include_incoming", True)),
                include_outgoing=bool(values.get("include_outgoing", True)),
                exclude_system_accounts=bool(
                    values.get("exclude_system_accounts", True)
                ),
                system_prefix=str(values.get("system_prefix", "5EYCAe5")),
                medium_tao=Decimal(str(values.get("medium_tao", "30"))),
                strong_tao=Decimal(str(values.get("strong_tao", "100"))),
                extreme_tao=Decimal(str(values.get("extreme_tao", "1000"))),
                min_direct_tao=Decimal(str(values.get("min_direct_tao", "0"))),
            )
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid link policy: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AddressRecord:
    address: str
    label: str = ""
    category: str = "UNKNOWN"
    note: str = ""


@dataclass(frozen=True)
class TransferRecord:
    tx_hash: str
    block_number: int
    from_address: str
    to_address: str
    amount_tao: Decimal
    block_hash: str = ""
    timestamp: str = ""
    edge_type: str = "tao_transfer"
    netuid: int | None = None
    hotkey: str = ""


@dataclass
class Edge:
    tx_hash: str
    block_number: int
    from_address: str
    to_address: str
    amount_tao: Decimal
    block_hash: str = ""
    timestamp: str = ""
    depth: int = 1
    reason: str = ""
    related_address: str = ""
    edge_type: str = "tao_transfer"
    netuid: int | None = None
    hotkey: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["amount_tao"] = str(self.amount_tao)
        return result


@dataclass(frozen=True)
class LibraryEntry:
    address: str
    label: str = ""
    category: str = ""
    note: str = ""
    source: str = ""
    # Directly constructed entries represent an already curated library row.
    # Database-backed entries pass the persisted confirmation flag explicitly.
    exchange_confirmed: bool = True
    exchange_rejected: bool = False
    # 同一个地址可以属于多个分类；系统分类（含「系统」）据此判断关联终点。
    categories: tuple[str, ...] = ()
    # 所属地址库名称，用于「无具体金额时按库间交易规模排序」。
    book: str = ""


@dataclass
class RelationResult:
    address: str
    depth: int
    incoming_tao: Decimal
    outgoing_tao: Decimal
    total_tao: Decimal
    incoming_count: int
    outgoing_count: int
    tx_count: int
    first_block: int
    last_block: int
    balance_tao: Decimal | None = None
    match: bool = False
    library_label: str = ""
    library_category: str = ""
    library_note: str = ""
    library_source: str = ""
    library_categories: tuple[str, ...] = ()
    address_role: str = ""
    role_reason: str = ""
    exchange_confirmed: bool = False
    exchange_rejected: bool = False
    # 官方交易所名单同步来的地址：判定固定，前端不给改判入口。
    exchange_locked: bool = False
    stake_transfer_count: int = 0
    # 关联强度：极强 / 强 / 中 / 弱（按累计金额分档，间接降一档）；
    # score 是同档内的排序分，reason 说明这条结论是怎么来的。
    strength: str = ""
    strength_score: int = 0
    strength_reason: str = ""
    # 从起始地址到该关联地址的逐笔转账路径（BFS 最短跳数路径），
    # 每项含 tx_hash、时间、金额，以及按起始地址方向整理好的
    # path_from → path_to / direction（out=转出、in=转入），供前端「路径明细」展示。
    path_transfers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ScanReport:
    seed: str
    max_depth: int
    data_cutoff_block: int | None
    freshness: str
    policy: LinkPolicy
    edges: list[Edge] = field(default_factory=list)
    related: list[RelationResult] = field(default_factory=list)
    library_matches: list[RelationResult] = field(default_factory=list)
    seed_is_in_library: bool = False
    seed_library_matches: list[LibraryEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
