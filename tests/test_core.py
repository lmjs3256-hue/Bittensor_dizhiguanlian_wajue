import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from app.collector import extract_balance_transfers
from app.db import Database
from app.service import _parse_upload
from app.relation import discover
from app.relation_config import Edge, LibraryEntry, LinkPolicy, TransferRecord


class FakeScale:
    def __init__(self, value):
        self.value = value


def test_extracts_only_balance_transfer_and_resolves_hash():
    events = [
        FakeScale({
            "phase": {"extrinsic_index": 0},
            "event": {"module_id": "Balances", "event_id": "Withdraw", "attributes": {"who": "A", "amount": 100}},
        }),
        FakeScale({
            "phase": {"extrinsic_index": 0},
            "event": {
                "module_id": "Balances",
                "event_id": "Transfer",
                "attributes": {"from": "A", "to": "B", "amount": 900000000},
            },
        }),
        FakeScale({
            "phase": {"extrinsic_index": 0},
            "event": {"module_id": "Balances", "event_id": "Deposit", "attributes": {"who": "B", "amount": 900000000}},
        }),
    ]
    extrinsics = [FakeScale({"extrinsic_hash": "0xabc"})]
    rows = extract_balance_transfers(
        events, block_number=100, block_hash="0xblock", extrinsics=extrinsics
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["from_address"] == "A"
    assert row["to_address"] == "B"
    assert row["amount_tao"] == Decimal("0.9")
    assert row["tx_hash"] == "0xabc"


def test_extract_accepts_tuple_attributes():
    item = FakeScale({
        "event": {
            "module_id": "Balances",
            "event_id": "Transfer",
            "attributes": ["A", "B", 1000000000],
        }
    })
    rows = extract_balance_transfers([item], block_number=1, block_hash="0x")
    assert rows[0]["amount_rao"] == 1000000000
    assert rows[0]["tx_hash"] == ""


def test_upload_csv_generates_library_records():
    rows = _parse_upload(
        "known.csv",
        "address,label,category,note\n5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt,Team,SEED,alpha\n5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg,Risky,BLACKLIST,\n",
    )
    assert len(rows) == 2
    assert rows[1]["category"] == "BLACKLIST"


def test_three_hop_search_and_library_match():
    records = []
    names = ["S", "A", "B", "C", "D"]
    for index, (source, target) in enumerate(zip(names, names[1:])):
        records.append(
            TransferRecord(
                f"0x{index}", 100 + index, source, target, Decimal("1")
            )
        )
    report = discover(
        "S",
        records,
        max_depth=3,
        library=[LibraryEntry("B", "Label B", "WATCH", "", "test")],
    )
    assert [row.address for row in report.related] == ["A", "B", "C"]
    assert report.library_matches[0].address == "B"
    assert report.library_matches[0].depth == 2


def test_small_indirect_transfer_does_not_become_a_graph_edge():
    records = [
        TransferRecord("0x01", 1, "S", "A", Decimal("1")),
        TransferRecord("0x02", 2, "A", "B", Decimal("0.01")),
        TransferRecord("0x03", 3, "B", "C", Decimal("100")),
    ]
    report = discover("S", records, max_depth=3, policy=LinkPolicy(Decimal("0.1"), Decimal("0.1")))
    assert [row.address for row in report.related] == ["A"]
    assert not any(edge.tx_hash == "0x03" for edge in report.edges)


def test_excluded_address_is_not_returned_or_used_as_a_transit_hop():
    records = [
        TransferRecord("0x01", 1, "S", "A", Decimal("1")),
        TransferRecord("0x02", 2, "S", "EXCHANGE", Decimal("2")),
        TransferRecord("0x03", 3, "EXCHANGE", "B", Decimal("3")),
    ]

    report = discover(
        "S",
        records,
        max_depth=3,
        excluded_addresses={"EXCHANGE"},
    )

    assert [row.address for row in report.related] == ["A"]
    assert "EXCHANGE" not in {edge.from_address for edge in report.edges}
    assert "EXCHANGE" not in {edge.to_address for edge in report.edges}


def test_excluded_seed_returns_no_relations():
    report = discover(
        "S",
        [TransferRecord("0x01", 1, "S", "A", Decimal("1"))],
        max_depth=3,
        excluded_addresses={"S"},
    )

    assert report.related == []
    assert report.edges == []
    assert any("起始地址命中" in warning for warning in report.warnings)


def test_known_exchange_gets_explicit_role_label():
    report = discover(
        "S",
        [TransferRecord("0x01", 1, "S", "EXCHANGE", Decimal("10"))],
        max_depth=1,
        library=[LibraryEntry("EXCHANGE", "OKX交易所", "EXCHANGE")],
    )

    assert report.related[0].address_role == "OKX交易所"
    assert "地址库明确标记" in report.related[0].role_reason


def test_confirmed_exchange_is_terminal_and_direct_neighbor_is_intermediary():
    report = discover(
        "S",
        [
            TransferRecord("0x01", 1, "S", "PERSONAL", Decimal("1")),
            TransferRecord("0x02", 2, "PERSONAL", "EXCHANGE", Decimal("0.1")),
            TransferRecord("0x03", 3, "EXCHANGE", "BEYOND", Decimal("100")),
        ],
        max_depth=3,
        policy=LinkPolicy(Decimal("0.1"), Decimal("0.1")),
        library=[LibraryEntry("EXCHANGE", "OKX交易所", "EXCHANGE")],
    )

    rows = {row.address: row for row in report.related}
    assert set(rows) == {"PERSONAL", "EXCHANGE"}
    assert rows["EXCHANGE"].address_role == "OKX交易所"
    assert rows["PERSONAL"].address_role == "疑似中转钱包（交易所关联）"
    assert "余额高低不影响此线索" in rows["PERSONAL"].role_reason


def test_exchange_related_intermediary_needs_only_one_transfer():
    report = discover(
        "S",
        [
            TransferRecord("0x01", 1, "S", "PERSONAL", Decimal("0.1")),
            TransferRecord("0x02", 2, "PERSONAL", "EXCHANGE", Decimal("0.1")),
        ],
        max_depth=2,
        policy=LinkPolicy(Decimal("1"), Decimal("0.1")),
        library=[LibraryEntry("EXCHANGE", "OKX交易所", "EXCHANGE")],
    )

    row = next(item for item in report.related if item.address == "PERSONAL")
    assert row.address_role == "疑似中转钱包（交易所关联）"


def test_related_result_carries_path_transfer_details():
    """2 级关联要能展示从起始地址到该地址的逐笔转账明细。"""

    report = discover(
        "S",
        [
            TransferRecord("0x01", 100, "S", "A", Decimal("2"), timestamp="2026-11-01T10:00:00Z"),
            TransferRecord("0x02", 200, "A", "B", Decimal("3"), timestamp="2026-11-02T11:30:00Z"),
        ],
        max_depth=3,
    )

    rows = {row.address: row for row in report.related}
    assert [row.address for row in report.related] == ["A", "B"]
    path_a = rows["A"].path_transfers
    assert len(path_a) == 1
    assert path_a[0]["from_address"] == "S"
    assert path_a[0]["to_address"] == "A"
    assert path_a[0]["amount_tao"] == "2"
    assert path_a[0]["timestamp"] == "2026-11-01T10:00:00Z"
    assert path_a[0]["block_number"] == 100

    path_b = rows["B"].path_transfers
    assert len(path_b) == 2
    assert [(hop["from_address"], hop["to_address"]) for hop in path_b] == [("S", "A"), ("A", "B")]
    assert path_b[1]["amount_tao"] == "3"


def test_noted_exchange_address_is_terminal():
    """备注写明「交易所」的地址：保留在结果里，但不再向下扩展。"""

    report = discover(
        "S",
        [
            TransferRecord("0x01", 1, "S", "HOTWALLET", Decimal("1")),
            TransferRecord("0x02", 2, "HOTWALLET", "BEYOND", Decimal("100")),
        ],
        max_depth=3,
        library=[
            LibraryEntry(
                "HOTWALLET", "热钱包", "其他", "人工确认：某交易所充值地址", "manual",
                exchange_confirmed=False,
            )
        ],
    )

    rows = {row.address for row in report.related}
    assert "HOTWALLET" in rows
    assert "BEYOND" not in rows
    assert any("交易所" in warning and "关联终点" in warning for warning in report.warnings)


def test_noted_exchange_in_label_is_terminal_but_rejected_is_not():
    """标签写「交易所」同样生效；被人工否定的地址不剪枝。"""

    records = [
        TransferRecord("0x01", 1, "S", "LABELLED", Decimal("1")),
        TransferRecord("0x02", 2, "LABELLED", "BEYOND1", Decimal("100")),
        TransferRecord("0x03", 3, "S", "REJECTED", Decimal("1")),
        TransferRecord("0x04", 4, "REJECTED", "BEYOND2", Decimal("100")),
    ]
    report = discover(
        "S",
        records,
        max_depth=3,
        library=[
            LibraryEntry("LABELLED", "某交易所直充", "其他", "", "manual", exchange_confirmed=False),
            LibraryEntry(
                "REJECTED", "候选", "EXCHANGE", "疑似交易所", "manual",
                exchange_confirmed=False, exchange_rejected=True,
            ),
        ],
    )

    rows = {row.address for row in report.related}
    assert "LABELLED" in rows
    assert "BEYOND1" not in rows
    assert "REJECTED" in rows
    assert "BEYOND2" in rows


def test_default_policy_starts_at_one_tao_and_reports_seed_library():
    report = discover(
        "S",
        [],
        library=[LibraryEntry("S", "DSV", "WATCH")],
    )

    assert LinkPolicy().min_edge_tao == Decimal("1")
    assert LinkPolicy().min_total_tao == Decimal("1")
    assert report.seed_is_in_library is True
    assert report.seed_library_matches[0].label == "DSV"


def test_relation_result_carries_current_balance_when_available():
    report = discover(
        "S",
        [TransferRecord("0x01", 1, "S", "A", Decimal("1.25"))],
        balances={"A": Decimal("12.3456")},
    )

    row = next(item for item in report.related if item.address == "A")
    assert row.balance_tao == Decimal("12.3456")


def test_api_usage_counts_are_persisted_by_calendar_period():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "usage.db")
        try:
            database.initialize()
            database.record_api_request("/v1/transfers", status_code=200, success=True)
            usage = database.api_usage_summary(monthly_limit=10000, per_minute_limit=5)
            assert usage["today"] == 1
            assert usage["month"] == 1
            assert usage["total"] == 1
            assert usage["monthly_remaining"] == 9999
        finally:
            database.engine.dispose()
def test_transfer_only_wallet_gets_conservative_role_label():
    report = discover(
        "S",
        [
            TransferRecord("0x01", 1, "S", "A", Decimal("1")),
            TransferRecord("0x02", 2, "A", "B", Decimal("1")),
        ],
        max_depth=2,
    )

    row = next(item for item in report.related if item.address == "A")
    assert row.address_role == "疑似中转钱包"
    assert "尚未接入子网质押、解质押和购买事件" in row.role_reason


def test_high_volume_wallet_gets_conservative_exchange_role_label():
    timestamp = datetime.now(timezone.utc).isoformat()
    records = [TransferRecord("0x00", 1, "S", "A", Decimal("100"), timestamp=timestamp)]
    records.extend(
        TransferRecord(
            f"0x{index:02x}",
            index + 1,
            "A",
            f"C{index}",
            Decimal("100"),
            timestamp=timestamp,
        )
        for index in range(1, 21)
    )
    report = discover("S", records, max_depth=1, balances={"A": Decimal("200")})

    row = next(item for item in report.related if item.address == "A")
    assert row.address_role == "疑似交易所（待确认）"


def test_low_balance_high_volume_wallet_is_not_exchange_suspect():
    timestamp = datetime.now(timezone.utc).isoformat()
    records = [TransferRecord("0x00", 1, "S", "A", Decimal("100"), timestamp=timestamp)]
    records.extend(
        TransferRecord(
            f"0x{index:02x}", index + 1, "A", f"C{index}", Decimal("100"), timestamp=timestamp
        )
        for index in range(1, 21)
    )
    report = discover("S", records, max_depth=1, balances={"A": Decimal("0.00001")})
    row = next(item for item in report.related if item.address == "A")
    assert row.address_role == "排除交易所疑似（余额偏低）"


def test_missing_balance_does_not_automatically_label_exchange():
    timestamp = datetime.now(timezone.utc).isoformat()
    records = [TransferRecord("0x00", 1, "S", "A", Decimal("100"), timestamp=timestamp)]
    records.extend(
        TransferRecord(
            f"0x{index:02x}", index + 1, "A", f"C{index}", Decimal("100"), timestamp=timestamp
        )
        for index in range(1, 21)
    )
    report = discover("S", records, max_depth=1)
    row = next(item for item in report.related if item.address == "A")
    assert row.address_role == "交易所判断待核余额"


def test_path_transfers_are_oriented_from_scan_seed():
    """路径明细必须从发起扫描的地址起步：对方转入的那一跳要反向展示。"""

    report = discover(
        "S",
        [
            TransferRecord("0x01", 100, "N1", "S", Decimal("2")),
            TransferRecord("0x02", 200, "S", "N2", Decimal("3")),
        ],
        max_depth=2,
        policy=LinkPolicy(Decimal("0.1"), Decimal("0.1")),
    )

    rows = {row.address: row for row in report.related}
    hop_n1 = rows["N1"].path_transfers[0]
    assert (hop_n1["from_address"], hop_n1["to_address"]) == ("N1", "S")  # 链上原始方向
    assert (hop_n1["path_from"], hop_n1["path_to"]) == ("S", "N1")  # 展示方向
    assert hop_n1["direction"] == "in"

    hop_n2 = rows["N2"].path_transfers[0]
    assert (hop_n2["path_from"], hop_n2["path_to"]) == ("S", "N2")
    assert hop_n2["direction"] == "out"


def test_confirmed_exchange_prunes_its_downstream_addresses():
    """公共地址/未录入的交易所人工确认后，刷新应把只经由它连通的下游地址移除。"""

    records = [
        TransferRecord("0x01", 100, "S", "A", Decimal("2")),
        TransferRecord("0x02", 110, "S", "EX", Decimal("2")),
        TransferRecord("0x03", 200, "EX", "X", Decimal("3")),
        TransferRecord("0x04", 210, "EX", "Y", Decimal("3")),
    ]
    library = [
        LibraryEntry(
            address="EX",
            label="人工确认交易所",
            category="EXCHANGE",
            note="",
            source="manual:exchange-confirmation",
            exchange_confirmed=True,
            exchange_rejected=False,
            categories=("EXCHANGE",),
        )
    ]

    report = discover(
        "S",
        records,
        max_depth=3,
        policy=LinkPolicy(Decimal("0.1"), Decimal("0.1")),
        library=library,
    )

    assert {row.address for row in report.related} == {"A", "EX"}
    assert any("交易所地址" in warning and "关联终点" in warning for warning in report.warnings)


def test_downstream_kept_when_it_has_another_path():
    """下游地址若还能通过别的路径连回起始地址，就不算交易所噪音，必须保留。"""

    records = [
        TransferRecord("0x01", 100, "S", "EX", Decimal("2")),
        TransferRecord("0x02", 110, "EX", "Z", Decimal("3")),
        TransferRecord("0x03", 120, "S", "Z", Decimal("2")),
    ]
    library = [
        LibraryEntry(
            address="EX",
            label="人工确认交易所",
            category="EXCHANGE",
            note="",
            source="manual",
            exchange_confirmed=True,
            exchange_rejected=False,
            categories=("EXCHANGE",),
        )
    ]

    report = discover(
        "S",
        records,
        max_depth=3,
        policy=LinkPolicy(Decimal("0.1"), Decimal("0.1")),
        library=library,
    )

    rows = {row.address: row for row in report.related}
    assert "Z" in rows
    assert rows["Z"].path_transfers[-1]["path_to"] == "Z"
