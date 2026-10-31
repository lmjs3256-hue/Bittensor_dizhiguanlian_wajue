"""Configuration and deterministic association rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class LinkPolicy:
    """Controls which transfers become graph edges.

    Direct seed transfers are always retained. This prevents a small first hop
    from hiding a later large transfer that would otherwise have linked it to
    the seed. A reported relation also requires one direct edge or the sum of
    all retained indirect edges to meet ``min_total_tao``.
    """

    min_edge_tao: Decimal = Decimal("0.1")
    min_total_tao: Decimal = Decimal("0.1")
    include_incoming: bool = True
    include_outgoing: bool = True
    exclude_system_accounts: bool = True
    system_prefix: str = "5EYCAe5"

    def __post_init__(self) -> None:
        if self.min_edge_tao < 0:
            raise ValueError("min_edge_tao must be >= 0")
        if self.min_total_tao < 0:
            raise ValueError("min_total_tao must be >= 0")
        if not self.include_incoming and not self.include_outgoing:
            raise ValueError("at least one transfer direction must be enabled")
        if not self.system_prefix:
            raise ValueError("system_prefix must not be empty")

    @classmethod
    def from_dict(cls, values: dict[str, Any] | None) -> "LinkPolicy":
        values = values or {}
        try:
            return cls(
                min_edge_tao=Decimal(str(values.get("min_edge_tao", "0.1"))),
                min_total_tao=Decimal(str(values.get("min_total_tao", "0.1"))),
                include_incoming=bool(values.get("include_incoming", True)),
                include_outgoing=bool(values.get("include_outgoing", True)),
                exclude_system_accounts=bool(
                    values.get("exclude_system_accounts", True)
                ),
                system_prefix=str(values.get("system_prefix", "5EYCAe5")),
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
    match: bool = False
    library_label: str = ""
    library_category: str = ""
    library_note: str = ""
    library_source: str = ""
    address_role: str = ""
    role_reason: str = ""


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
    warnings: list[str] = field(default_factory=list)
