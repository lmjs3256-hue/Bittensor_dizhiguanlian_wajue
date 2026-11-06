# -*- coding: utf-8 -*-
"""人工确认交易所/验证者/公共地址后，回头校正历史查询结果。"""

from decimal import Decimal

import pytest

import app.service as service_module
from app.db import AutoImportLog, Database, WatchCandidate
from app.service import LinkService, prune_archive_payload


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
HUB = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
MIDDLE = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
LEAF = "5C9yEy27yLNG5BDMxVwS8RyGBneZB1ouShazFhGZVP8thK5z"
OTHER = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.fixture(autouse=True)
def no_balance_rpc(monkeypatch):
    monkeypatch.setattr(service_module, "query_account_balances", lambda *_a, **_k: {})


@pytest.fixture
def svc(tmp_path):
    database = Database(tmp_path / "reconcile.db")
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
        "reason": "qualified_edge",
    }


def build_payload() -> dict:
    """SEED → HUB → MIDDLE → LEAF，另有一条 SEED → OTHER。"""

    return {
        "seed": SEED,
        "max_depth": 3,
        "data_source": "local",
        "related": [
            {"address": HUB, "depth": 1, "path_transfers": [_hop(SEED, HUB, 100)]},
            {
                "address": MIDDLE,
                "depth": 2,
                "path_transfers": [_hop(SEED, HUB, 100), _hop(HUB, MIDDLE, 101)],
            },
            {
                "address": LEAF,
                "depth": 3,
                "path_transfers": [
                    _hop(SEED, HUB, 100),
                    _hop(HUB, MIDDLE, 101),
                    _hop(MIDDLE, LEAF, 102),
                ],
            },
            {"address": OTHER, "depth": 1, "path_transfers": [_hop(SEED, OTHER, 103)]},
        ],
        "edges": [
            _edge(SEED, HUB, 1, 100),
            _edge(HUB, MIDDLE, 2, 101),
            _edge(MIDDLE, LEAF, 3, 102),
            _edge(SEED, OTHER, 1, 103),
        ],
        "library_matches": [],
        "warnings": [],
    }


def save_archive(service) -> int:
    return service.archive_relation(build_payload(), kind="job")


def archive_payload(service, archive_id) -> dict:
    return service.relation_history_detail(archive_id)["result"]


def test_prune_keeps_terminal_drops_its_downstream():
    payload, stats = prune_archive_payload(build_payload(), {HUB: ("PUBLIC",)})
    addresses = [item["address"] for item in payload["related"]]
    assert addresses == [HUB, OTHER]
    assert stats["related_removed"] == 2
    assert stats["blocking"] == {"公共地址": 2}
    assert payload["correction"]["removed_related"] == 2
    # 边表是真实发生的转账记录，也是可达性判断依据，剪枝时保持原样。
    assert len(payload["edges"]) == 4


def test_prune_is_idempotent():
    payload, _ = prune_archive_payload(build_payload(), {HUB: ("EXCHANGE",)})
    again, stats = prune_archive_payload(payload, {HUB: ("EXCHANGE",)})
    assert stats["related_removed"] == 0
    assert [item["address"] for item in again["related"]] == [HUB, OTHER]


def test_prune_without_edges_uses_path_hops():
    payload = build_payload()
    payload.pop("edges")
    pruned, stats = prune_archive_payload(payload, {HUB: ("VALIDATOR",)})
    assert [item["address"] for item in pruned["related"]] == [HUB, OTHER]
    assert stats["related_removed"] == 2


def _no_path_payload(related, edges) -> dict:
    """模拟老记录：关联条目没有 path_transfers，只剩边表可依据。"""

    return {
        "seed": SEED,
        "related": [{"address": address, "depth": 1} for address in related],
        "edges": [_edge(source, target, 1, 100 + index) for index, (source, target) in enumerate(edges)],
        "warnings": [],
    }


def test_prune_without_paths_drops_terminal_downstream():
    """回归：终点必须入队，否则「标记终点下游」那段是死代码，一条都剪不掉。"""

    payload = _no_path_payload(
        [HUB, MIDDLE],
        [(SEED, HUB), (HUB, MIDDLE)],
    )
    pruned, stats = prune_archive_payload(payload, {HUB: ("EXCHANGE",)})
    assert [item["address"] for item in pruned["related"]] == [HUB]
    assert stats["related_removed"] == 1
    assert stats["unverified"] == 0
    assert stats["blocking"] == {"交易所": 1}


def test_prune_without_paths_drops_whole_downstream_chain():
    """终点下游的整条链都要删，不只是第一层。"""

    payload = _no_path_payload(
        [HUB, MIDDLE, LEAF],
        [(SEED, HUB), (HUB, MIDDLE), (MIDDLE, LEAF)],
    )
    pruned, stats = prune_archive_payload(payload, {HUB: ("PUBLIC",)})
    assert [item["address"] for item in pruned["related"]] == [HUB]
    assert stats["related_removed"] == 2
    assert stats["unverified"] == 0


def test_prune_without_paths_keeps_reachable_side_branch():
    """删的只限终点下游：绕开终点也能到的一律保留。"""

    payload = _no_path_payload(
        [HUB, MIDDLE, OTHER],
        [(SEED, HUB), (HUB, MIDDLE), (SEED, OTHER), (OTHER, MIDDLE)],
    )
    pruned, stats = prune_archive_payload(payload, {HUB: ("EXCHANGE",)})
    # MIDDLE 经 OTHER 也能到达，属于正常线索；只有 HUB 的下游才剪。
    assert sorted(item["address"] for item in pruned["related"]) == sorted([HUB, MIDDLE, OTHER])
    assert stats["related_removed"] == 0
    assert stats["unverified"] == 0


def test_prune_counts_unverified_when_graph_does_not_connect():
    """边表没把这个地址连到起点时，仍然按「无证据不删」处理。"""

    payload = _no_path_payload(
        [HUB, MIDDLE, OTHER],
        [(SEED, HUB), (MIDDLE, OTHER)],
    )
    pruned, stats = prune_archive_payload(payload, {HUB: ("EXCHANGE",)})
    assert stats["related_removed"] == 0
    assert stats["unverified"] == 2
    assert len(pruned["related"]) == 3


def test_marking_public_corrects_existing_archive(svc):
    _database, service = svc
    service.import_rows([{"address": HUB, "category": "群友"}], "manual")
    archive_id = save_archive(service)
    assert len(archive_payload(service, archive_id)["related"]) == 4

    result = service.decide_public_address(HUB, True)

    assert result["correction"]["archives"]["changed"] == 1
    assert result["correction"]["archives"]["related_removed"] == 2
    payload = archive_payload(service, archive_id)
    assert [item["address"] for item in payload["related"]] == [HUB, OTHER]
    assert service.relation_history()["items"][0]["related_count"] == 2


def test_confirm_exchange_corrects_existing_archive(svc):
    _database, service = svc
    service.import_rows([{"address": HUB, "category": "EXCHANGE"}], "manual")
    archive_id = save_archive(service)

    result = service.decide_exchange_address(HUB, "confirm")

    assert result["correction"]["archives"]["related_removed"] == 2
    payload = archive_payload(service, archive_id)
    assert [item["address"] for item in payload["related"]] == [HUB, OTHER]


def test_exchange_note_corrects_existing_archive(svc):
    """备注里写明「交易所」也算关联终点（老数据的常见写法）。"""

    _database, service = svc
    service.import_rows([{"address": HUB, "category": "群友"}], "manual")
    archive_id = save_archive(service)
    row_id = service.library()[0]["id"]

    result = service.update_library_note(row_id, "某交易所归集地址")

    assert result["correction"]["archives"]["related_removed"] == 2
    payload = archive_payload(service, archive_id)
    assert [item["address"] for item in payload["related"]] == [HUB, OTHER]


def test_reject_does_not_correct(svc):
    _database, service = svc
    service.import_rows([{"address": HUB, "category": "EXCHANGE"}], "manual")
    archive_id = save_archive(service)

    service.decide_exchange_address(HUB, "reject")

    assert len(archive_payload(service, archive_id)["related"]) == 4


def test_dry_run_reports_without_writing(svc):
    _database, service = svc
    # 直接建成验证者库记录，避开「标记后自动校正」，用来单独验证预演行为。
    service.import_rows([{"address": HUB, "category": "VALIDATOR"}], "manual")
    archive_id = save_archive(service)

    report = service.reconcile_history(dry_run=True, addresses=[HUB], watch=False)

    assert report["dry_run"] is True
    assert report["archives"]["changed"] == 1
    assert report["archives"]["related_removed"] == 2
    assert len(archive_payload(service, archive_id)["related"]) == 4

    applied = service.reconcile_history(addresses=[HUB], watch=False)
    assert applied["archives"]["related_removed"] == 2
    assert len(archive_payload(service, archive_id)["related"]) == 2


def test_address_filter_skips_unrelated_archives(svc):
    """自动触发时只重扫提到过该地址的快照，别的历史记录不碰。"""

    _database, service = svc
    service.import_rows([{"address": HUB, "category": "VALIDATOR"}], "manual")
    archive_id = save_archive(service)
    unrelated = service.archive_relation(
        {
            "seed": OTHER,
            "max_depth": 1,
            "related": [{"address": LEAF, "depth": 1, "path_transfers": []}],
            "edges": [],
        },
        kind="job",
    )

    report = service.reconcile_history(addresses=[HUB], watch=False)

    assert report["archives"]["scanned"] == 1
    assert report["archives"]["changed"] == 1
    assert [item["address"] for item in archive_payload(service, archive_id)["related"]] == [
        HUB,
        OTHER,
    ]
    assert archive_payload(service, unrelated)["related"][0]["address"] == LEAF


def test_reconcile_cleans_watch_products(svc):
    database, service = svc
    service.import_rows([{"address": HUB, "category": "群友"}], "manual")
    with database.session() as db:
        db.add(
            WatchCandidate(
                address=MIDDLE,
                book="群友",
                cumulative_tao=Decimal("120"),
                tx_count=4,
                trigger_address=HUB,
            )
        )
        db.add(
            AutoImportLog(
                address=LEAF,
                book="群友",
                trigger_address=HUB,
                amount_tao=Decimal("120"),
                reason="巡检累计达标",
            )
        )
        db.add(
            AutoImportLog(
                address=OTHER,
                book="群友",
                trigger_address=OTHER,
                amount_tao=Decimal("90"),
                reason="正常线索",
            )
        )

    service.decide_public_address(HUB, True)
    report = service.reconcile_history(addresses=[HUB], watch=True)

    assert report["watch"]["candidates_removed"] == 1
    assert report["watch"]["auto_imports_revoked"] == 1
    with database.session() as db:
        assert db.query(WatchCandidate).count() == 0
        active = db.query(AutoImportLog).filter(AutoImportLog.status == "active").all()
        assert [row.address for row in active] == [OTHER]
        revoked = db.query(AutoImportLog).filter(AutoImportLog.status == "revoked").one()
        assert "校正" in revoked.reason


def test_reconcile_report_lists_samples(svc):
    _database, service = svc
    service.import_rows([{"address": HUB, "category": "PUBLIC"}], "manual")
    save_archive(service)

    report = service.reconcile_history(addresses=[HUB], watch=False)

    assert set(report["samples"]) == {MIDDLE, LEAF}
    assert report["details"][0]["related_before"] == 4
    assert report["details"][0]["related_after"] == 2
    assert report["terminal"]["by_role"].get("公共地址") == 1
