"""内置固定名单：人工确认过的交易所 / 验证者 / 公共地址。

这些地址不是「同步来的候选」，而是已经人工核实、随版本一起发布的结论：

- 交易所：会和大量不确定钱包往来，查到即作为关联终点；判定固定，不能改判。
- 验证者：热钱包/冷钱包会和押注、解押、子网质押产生海量流水，查到即终止，
  否则一次查询会被验证者的转账噪音灌满。
- 公共地址：中转站、发币池、公共充值地址，同交易所性质。

规则：

- 内置记录用 ``builtin:<分类>`` 作为 source，界面显示「内置固定」；
- 每次启动幂等写入（``ensure_builtin_addresses``），只补不删：用户改过的
  名称、备注保留，已有主分类保留，只把系统分类追加进多分类；
- 内置记录不能删除、不能改分类、不能改判交易所，避免「固定」被误操作冲掉。

新增名单只需要在下面三个元组里加一行，升级后自动进库。
"""

from __future__ import annotations

from dataclasses import dataclass

BUILTIN_SOURCE_PREFIX = "builtin:"


@dataclass(frozen=True)
class BuiltinAddress:
    """一条内置地址：地址 + 系统分类 + 展示名。"""

    address: str
    category: str
    label: str

    @property
    def source(self) -> str:
        return f"{BUILTIN_SOURCE_PREFIX}{self.category.lower()}"


# 人工确认的交易所地址（用户提供，2026-11 核实）。
BUILTIN_EXCHANGE_ADDRESSES: tuple[tuple[str, str], ...] = (
    ("5FqqXKb9zonSNKbZhEuHYjCXnmPbX9tdzMCU2gx8gir8Z8a5", "人工确认交易所"),
    ("5DunDrFV6BPbzsgLZUtMQtCq7w9fcUEbM59BUwx5E7N9Bn5E", "人工确认交易所"),
    ("5EiXej3AwjKqb9mjQAf29JG5HJf9Dwtt8CjvDqA8biprWTiN", "人工确认交易所"),
    ("5FV99mBYw3tYrMTXXe52PDa69an1AE19aTrpnB4kzxW73yUj", "人工确认交易所"),
    ("5Gr1Ab7LSV4eT6RdWmVeoxnTpDBWAZFLXHHrcaWNDdjK22pp", "人工确认交易所"),
)

# 人工确认的验证者地址。
BUILTIN_VALIDATOR_ADDRESSES: tuple[tuple[str, str], ...] = (
    ("5HK5tp6t2S59DywmHRWPBVJeJ86T61KjurYqeooqj8sREpeN", "人工确认验证者"),
)

# 人工确认的公共地址（中转、发币池、公共充值地址等）。
BUILTIN_PUBLIC_ADDRESSES: tuple[tuple[str, str], ...] = ()


def is_builtin_source(source: str | None) -> bool:
    """来源是否为内置固定名单（这类记录不允许删除、改分类、改判）。"""

    return str(source or "").strip().startswith(BUILTIN_SOURCE_PREFIX)


def builtin_entries() -> tuple[BuiltinAddress, ...]:
    """内置名单全量条目（交易所 / 验证者 / 公共地址）。"""

    entries: list[BuiltinAddress] = []
    for addresses, category in (
        (BUILTIN_EXCHANGE_ADDRESSES, "EXCHANGE"),
        (BUILTIN_VALIDATOR_ADDRESSES, "VALIDATOR"),
        (BUILTIN_PUBLIC_ADDRESSES, "PUBLIC"),
    ):
        for address, label in addresses:
            entries.append(BuiltinAddress(address=address.strip(), category=category, label=label))
    return tuple(entries)
