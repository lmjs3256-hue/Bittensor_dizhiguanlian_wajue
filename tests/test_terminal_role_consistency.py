"""关联终点判定的四个缺陷（用户实测报障后补的回归）。

**用户现象**：把 `5CF6Ruq…` 标成公共地址后，刷新结果里仍然出现**穿过它**的
三级下游地址 `5HCT4A…`。路径是：

    5HinyU…(seed) → 5Cf5… → 5CF6Ruq…(公共地址) → 5HCT4A…(不该出现)

排查出四个独立缺陷，都会导致「明明标了公共地址，它的下游还在」：

1. **改分类会把 PUBLIC 冲掉**（`update_library_categories` /
   `update_group_categories` / 组内添加）：都用 `encode_categories(values)`
   整体覆盖，标好的角色被静默抹掉。
2. **公共地址/验证者只看分类，不看标签备注**：交易所会从标签/备注推断，
   公共地址和验证者不会。于是「标签写着公共地址、分类是别的」的地址不算终点。
3. **整组改分类没排除 PUBLIC**：只跳过 EXCHANGE/VALIDATOR。
4. **`人工否定交易所` 被当终点**：它的分类仍是 EXCHANGE，标签里也有「交易所」
   三字，忘了 `exchange_rejected` 这个否定结论。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import app.service as service_module
from app.db import AddressLibrary, Database
from app.relation import terminal_roles
from app.relation_config import LibraryEntry
from app.service import LinkService

SEED = "5HinyU1VsBUu51mSdeWCm8FUkuVGXCJMQj2QMpTSYtoqW1fn"
PUB = "5CF6Ruq6u7DoF6TYxyaFzEuviWi7tjvJtz19XgPS2b7XDCFM"
MIDDLE = "5Cf5Ea2BbLJ6mReeBQ6NRx8juYb3DaUQmzDfyq2jij6num2v"
DOWNSTREAM = "5HCT4AarReToT1BKyLtJXJfSLs4zRS7dENnZ7iysqrqxXyV7"


def _entry(**kw) -> LibraryEntry:
    base = {"address": "5Test", "label": "", "category": "其他", "categories": ("其他",)}
    base.update(kw)
    return LibraryEntry(**base)


# -- 缺陷 2：标签/备注要能推断出公共地址与验证者 -----------------------------
def test_label_public_address_counts_as_terminal():
    """标签写「公共地址」但分类是「大V」——仍必须算终点。

    这是用户那个地址的真实形态：被手动加进名叫「公共地址」的组，
    组分类沿用「大V」，于是 categories 里没有 PUBLIC。
    """

    roles = terminal_roles([_entry(label="公共地址", category="大V", categories=("大V",))])
    assert roles["5Test"] == ("PUBLIC",)


def test_note_public_address_counts_as_terminal():
    roles = terminal_roles([_entry(note="这是公共地址，会和大量钱包往来")])
    assert roles["5Test"] == ("PUBLIC",)


def test_label_validator_counts_as_terminal():
    roles = terminal_roles([_entry(label="验证者地址", category="大V", categories=("大V",))])
    assert roles["5Test"] == ("VALIDATOR",)


def test_negated_public_address_is_not_terminal():
    """写了「已否定 / 不是」，说明结论是"不算"，不能当终点。"""

    roles = terminal_roles([_entry(label="公共地址", note="已否定，不是公共地址")])
    assert "5Test" not in roles


def test_plain_project_address_is_not_terminal():
    """「SN51项目方」这种普通标签不能被误判成系统角色。"""

    roles = terminal_roles(
        [_entry(label="SN51项目方", category="项目方", categories=("项目方",))]
    )
    assert "5Test" not in roles


def test_rejected_exchange_is_not_terminal():
    """人工否定交易所：分类仍是 EXCHANGE、标签也含「交易所」，但不能当终点。"""

    roles = terminal_roles(
        [
            _entry(
                label="人工否定交易所",
                category="EXCHANGE",
                categories=("EXCHANGE",),
                exchange_rejected=True,
            )
        ]
    )
    assert "5Test" not in roles


def test_confirmed_exchange_is_terminal():
    roles = terminal_roles(
        [
            _entry(
                label="人工确认交易所",
                category="EXCHANGE",
                categories=("EXCHANGE",),
                exchange_confirmed=True,
            )
        ]
    )
    assert roles["5Test"] == ("EXCHANGE",)


# -- 缺陷 1/3：改分类不能冲掉系统角色 ---------------------------------------
@pytest.fixture()
def service(tmp_path):
    database = Database(tmp_path / "roles.db")
    database.initialize()
    yield LinkService(database, "wss://example.invalid")
    database.engine.dispose()


def _add(database, address, **kw) -> int:
    with database.session() as db:
        row = AddressLibrary(
            address=address,
            label=kw.pop("label", ""),
            category=kw.pop("category", "其他"),
            categories=kw.pop("categories", '["其他"]'),
            note=kw.pop("note", ""),
            source=kw.pop("source", "manual"),
            book=kw.pop("book", ""),
            **kw,
        )
        db.add(row)
        db.flush()
        return row.id


def test_edit_category_keeps_public_role(service):
    """单条改分类不能把 PUBLIC 抹掉（以前是整体覆盖）。"""

    row_id = _add(
        service.database, PUB,
        label="公共地址", category="项目方", categories='["项目方","PUBLIC"]',
    )
    service.update_library_categories(row_id, ["大V"])

    with service.database.session() as db:
        row = db.get(AddressLibrary, row_id)
        assert "PUBLIC" in service_module.row_categories(row)
        assert "大V" in service_module.row_categories(row)


def test_group_category_edit_skips_public(service):
    """整组改分类时，公共地址要被跳过（以前只跳过 EXCHANGE/VALIDATOR）。"""

    _add(service.database, PUB, label="公共地址", category="PUBLIC",
         categories='["PUBLIC"]', book="某库")
    _add(service.database, MIDDLE, label="某库", category="项目方",
         categories='["项目方"]', book="某库")

    result = service.update_group_categories(["大V"], book="某库")
    assert result["skipped"] >= 1

    with service.database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=PUB).one()
        assert "PUBLIC" in service_module.row_categories(row)


def test_group_with_only_role_addresses_is_rejected(service):
    """整组都是系统角色时直接拒绝，而不是把它们静默降级成普通地址。"""

    _add(service.database, PUB, label="库2", category="PUBLIC",
         categories='["PUBLIC"]', book="库2")

    with pytest.raises(Exception):
        service.update_group_categories(["群友"], book="库2")

    with service.database.session() as db:
        row = db.query(AddressLibrary).filter_by(address=PUB).one()
        assert "PUBLIC" in service_module.row_categories(row)


# -- 端到端：标记后历史结果的剪枝 -------------------------------------------
def test_prune_removes_downstream_through_public_address():
    """用户原始场景：标记公共地址后，穿过它的三级下游必须被剪掉。

    实测这条记录：标记前 210 条，标记后只剩 51 条，`5HCT4A…` 被剪掉。
    """

    payload = {
        "seed": SEED,
        "related": [
            {"address": MIDDLE, "depth": 1,
             "path_transfers": [{"from_address": SEED, "to_address": MIDDLE}]},
            {"address": PUB, "depth": 2,
             "path_transfers": [
                 {"from_address": SEED, "to_address": MIDDLE},
                 {"from_address": MIDDLE, "to_address": PUB},
             ]},
            {"address": DOWNSTREAM, "depth": 3,
             "path_transfers": [
                 {"from_address": SEED, "to_address": MIDDLE},
                 {"from_address": MIDDLE, "to_address": PUB},
                 {"from_address": PUB, "to_address": DOWNSTREAM},
             ]},
        ],
        "edges": [
            {"from_address": SEED, "to_address": MIDDLE},
            {"from_address": MIDDLE, "to_address": PUB},
            {"from_address": PUB, "to_address": DOWNSTREAM},
        ],
    }
    terminal = terminal_roles(
        [_entry(address=PUB, label="公共地址", category="PUBLIC", categories=("PUBLIC",))]
    )

    pruned, stats = service_module.prune_archive_payload(payload, terminal)
    kept = {item["address"] for item in pruned["related"]}

    # 终点本身保留（它是有价值的关联地址），它的下游剪掉
    assert PUB in kept
    assert DOWNSTREAM not in kept
    assert stats["related_removed"] == 1


def test_prune_keeps_normal_addresses():
    """没有穿过终点的地址一条都不能删——宁可漏剪也不能误伤。"""

    payload = {
        "seed": SEED,
        "related": [
            {"address": MIDDLE, "depth": 1,
             "path_transfers": [{"from_address": SEED, "to_address": MIDDLE}]},
        ],
        "edges": [{"from_address": SEED, "to_address": MIDDLE}],
    }
    terminal = terminal_roles(
        [_entry(address=PUB, label="公共地址", category="PUBLIC", categories=("PUBLIC",))]
    )
    pruned, stats = service_module.prune_archive_payload(payload, terminal)
    assert stats["related_removed"] == 0
    assert {item["address"] for item in pruned["related"]} == {MIDDLE}
