import json
from decimal import Decimal

from app.collector import extract_balance_transfers
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
    assert "未发现子网购买记录" in row.role_reason


def test_high_volume_wallet_gets_conservative_exchange_role_label():
    records = [TransferRecord("0x00", 1, "S", "A", Decimal("15"))]
    records.extend(
        TransferRecord(f"0x{index:02x}", index + 1, "A", f"C{index}", Decimal("15"))
        for index in range(1, 11)
    )
    report = discover("S", records, max_depth=1)

    row = next(item for item in report.related if item.address == "A")
    assert row.address_role == "疑似交易所"
