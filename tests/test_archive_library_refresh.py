# -*- coding: utf-8 -*-
"""标记公共地址后，历史结果里的「库归属」必须跟着变。

用户反馈（0.9.1）：一个地址本来在 ``const`` 库里，在结果页把它标成公共地址后，
地址库里的 ``book`` 已经清空、``label`` 已经是「公共地址」，但打开历史结果，
那一行仍显示库名「const」、仍挂着「标为公共地址」按钮——看起来就像标记动作
根本没把它从 const 移出去。

根因：剪枝（``prune_archive_payload``）只删终点下游，**保留下来的条目原封不动
带着生成当时的 ``library_*`` 快照**，不会跟着库变化。
"""

import pytest

import app.service as service_module
from app.db import Database
from app.relation_config import LibraryEntry
from app.service import LinkService, refresh_archive_library_fields

SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
HUB = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
LEAF = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"
OTHER = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


@pytest.fixture
def svc(tmp_path):
    database = Database(tmp_path / "archive_refresh.db")
    database.initialize()
    yield database, LinkService(database, "wss://example.invalid")
    database.engine.dispose()


def _hop(source, target, block):
    return {
        "tx_hash": f"tx-{source[:6]}-{target[:6]}-{block}",
        "block_number": block,
        "from_address": source,
        "to_address": target,
        "amount_tao": "5",
        "timestamp": "2026-11-01T00:00:00.000Z",
        "path_from": source,
        "path_to": target,
        "direction": "out",
    }


def _edge(source, target, depth, block):
    return {
        "tx_hash": f"tx-{source[:6]}-{target[:6]}-{block}",
        "block_number": block,
        "from_address": source,
        "to_address": target,
        "amount_tao": "5",
        "depth": depth,
    }


def build_payload() -> dict:
    """SEED → HUB → LEAF，另有一条 SEED → OTHER；HUB 那条带着 const 库快照。"""

    return {
        "seed": SEED,
        "max_depth": 2,
        "data_source": "local",
        "related": [
            {
                "address": HUB,
                "depth": 1,
                # 历史快照：当时它在 const 库，身份还是"疑似中转钱包"。
                "match": True,
                "library_label": "const",
                "library_category": "大V",
                "library_note": "由关联查询 X 新建并导入",
                "library_source": "bittensor-address-library-2026-11-10.json",
                "library_categories": ["大V"],
                "address_role": "疑似中转钱包（交易所关联）",
                "role_reason": "该地址与已确认的交易所公共地址存在直接转账。",
                "path_transfers": [_hop(SEED, HUB, 100)],
            },
            {
                "address": LEAF,
                "depth": 2,
                "path_transfers": [_hop(SEED, HUB, 100), _hop(HUB, LEAF, 101)],
            },
            {
                "address": OTHER,
                "depth": 1,
                "path_transfers": [_hop(SEED, OTHER, 103)],
            },
        ],
        "edges": [
            _edge(SEED, HUB, 1, 100),
            _edge(HUB, LEAF, 2, 101),
            _edge(SEED, OTHER, 1, 103),
        ],
        "library_matches": [],
        "warnings": [],
    }


def _row(service, archive_id, address):
    payload = service.relation_history_detail(archive_id)["result"]
    return next(item for item in payload["related"] if item["address"] == address)


def test_marking_public_drops_stale_book_name_from_history(svc):
    """核心回归：标成公共地址后，历史结果里不能再出现 const。"""

    _database, service = svc
    service.import_rows(
        [
            {
                "address": HUB,
                "category": "大V",
                "label": "const",
                "book": "const",
                "note": "由关联查询 X 新建并导入",
            }
        ],
        "bittensor-address-library.json",
    )
    archive_id = service.archive_relation(build_payload(), kind="job")

    result = service.decide_public_address(HUB, True)

    assert result["correction"]["archives"]["changed"] == 1
    row = _row(service, archive_id, HUB)
    assert row["library_label"] == "公共地址"
    assert "PUBLIC" in row["library_categories"]
    assert row["library_label"] != "const"
    assert row["address_role"] == "公共地址"
    assert "const" not in row["role_reason"]
    # 下游照旧剪掉，其它线索不受影响。
    assert _row(service, archive_id, OTHER)["address"] == OTHER
    payload = service.relation_history_detail(archive_id)["result"]
    assert [item["address"] for item in payload["related"]] == [HUB, OTHER]


def test_refresh_alone_is_persisted_without_pruning(svc):
    """只有库归属变了、没有可剪的下游时，也必须落库。"""

    _database, service = svc
    service.import_rows(
        [{"address": OTHER, "category": "PUBLIC", "label": "公共地址"}], "manual"
    )
    archive_id = service.archive_relation(
        {
            "seed": SEED,
            "max_depth": 1,
            "related": [
                {
                    "address": OTHER,
                    "depth": 1,
                    "match": True,
                    "library_label": "const",
                    "library_category": "大V",
                    "library_categories": ["大V"],
                    "path_transfers": [_hop(SEED, OTHER, 103)],
                }
            ],
            "edges": [_edge(SEED, OTHER, 1, 103)],
        },
        kind="job",
    )

    report = service.reconcile_history(addresses=[OTHER], watch=False)

    assert report["archives"]["related_removed"] == 0
    assert report["archives"]["library_refreshed"] == 1
    assert report["archives"]["changed"] == 1
    assert report["details"][0]["refreshed"] == 1
    assert _row(service, archive_id, OTHER)["library_label"] == "公共地址"


def test_refresh_is_idempotent(svc):
    _database, service = svc
    service.import_rows(
        [{"address": HUB, "category": "PUBLIC", "label": "公共地址"}], "manual"
    )
    service.archive_relation(build_payload(), kind="job")

    first = service.reconcile_history(addresses=[HUB], watch=False)
    second = service.reconcile_history(addresses=[HUB], watch=False)

    assert first["archives"]["library_refreshed"] >= 1
    assert second["archives"]["library_refreshed"] == 0
    assert second["archives"]["changed"] == 0


def test_missing_library_row_clears_stale_fields():
    """地址已经不在库里时，旧库名要清掉，不能留一个不存在的归属。"""

    payload = {
        "seed": SEED,
        "related": [
            {
                "address": HUB,
                "depth": 1,
                "match": True,
                "library_label": "const",
                "library_category": "大V",
                "library_categories": ["大V"],
                "address_role": "疑似中转钱包（交易所关联）",
            }
        ],
    }

    refreshed, count = refresh_archive_library_fields(payload, {})

    assert count == 1
    row = refreshed["related"][0]
    assert row["match"] is False
    assert row["library_label"] == ""
    assert row["library_categories"] == []
    # 没有库证据时不覆盖基于转账事实的身份判断。
    assert row["address_role"] == "疑似中转钱包（交易所关联）"


def test_plain_address_keeps_role_text():
    """库里只是普通分类的地址：刷新库字段，但不动转账事实得出的身份。"""

    payload = {
        "seed": SEED,
        "related": [
            {
                "address": OTHER,
                "depth": 1,
                "match": True,
                "library_label": "const",
                "library_category": "大V",
                "library_categories": ["大V"],
                "address_role": "疑似中转钱包（交易所关联）",
            }
        ],
    }
    entries = {
        OTHER: LibraryEntry(
            address=OTHER,
            label="const",
            category="大V",
            categories=("大V",),
            book="const",
            # 库里的普通地址不是已确认交易所；直接构造的默认值是 True，
            # 这里显式写清楚，避免把「默认值」当成库事实。
            exchange_confirmed=False,
        )
    }

    refreshed, count = refresh_archive_library_fields(payload, entries)

    assert count == 0
    assert refreshed["related"][0]["address_role"] == "疑似中转钱包（交易所关联）"
