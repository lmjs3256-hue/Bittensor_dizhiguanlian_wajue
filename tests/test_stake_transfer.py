"""Subnet stake-transfer edges (SubtensorModule.StakeTransferred)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from sqlalchemy import select, text

import app.service as service_module
import app.taostats as taostats
from app.collector import extract_stake_transfers
from app.db import Database, Transfer, to_relation_records
from app.relation import discover
from app.relation_config import LinkPolicy, TransferRecord
from app.service import LinkService, _stake_counterparty
from app.taostats import TransferPage, get_stake_transfer_page, reset_client_state_for_tests
from http_stub import as_http_get

A = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
B = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
HOTKEY = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"


class Scale:
    def __init__(self, value):
        self.value = value


def stake_event(attributes):
    return Scale(
        {
            "phase": {"extrinsic_index": 0},
            "event": {
                "module_id": "SubtensorModule",
                "event_id": "StakeTransferred",
                "attributes": attributes,
            },
        }
    )


@pytest.fixture(autouse=True)
def reset_state():
    reset_client_state_for_tests()
    yield
    reset_client_state_for_tests()


def test_extract_stake_transfers_positional_attributes():
    events = [
        Scale(
            {
                "event": {
                    "module_id": "Balances",
                    "event_id": "Transfer",
                    "attributes": {"from": A, "to": B, "amount": 1_000_000_000},
                }
            }
        ),
        stake_event((A, B, HOTKEY, 72, 0, 2_242_880_120)),
        stake_event((A, A, HOTKEY, 72, 0, 1_000_000_000)),
    ]
    rows = extract_stake_transfers(
        events,
        block_number=100,
        block_hash="0xblock",
        extrinsics=[Scale({"extrinsic_hash": "0xhash"})],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["from_address"] == A
    assert row["to_address"] == B
    assert row["amount_tao"] == Decimal("2.24288012")
    assert row["edge_type"] == "stake_transfer"
    assert row["netuid"] == 72
    assert row["hotkey"] == HOTKEY
    assert row["tx_hash"] == "0xhash"
    assert row["event_key"].startswith("stake:")


def test_extract_stake_transfers_accepts_dict_attributes():
    rows = extract_stake_transfers(
        [
            stake_event(
                {
                    "origin_coldkey": A,
                    "destination_coldkey": B,
                    "hotkey": HOTKEY,
                    "origin_netuid": 3,
                    "destination_netuid": 3,
                    "amount": 3_000_000_000,
                }
            )
        ],
        block_number=1,
        block_hash="0x",
    )
    assert len(rows) == 1
    assert rows[0]["amount_tao"] == Decimal("3")
    assert rows[0]["netuid"] == 3


def test_stake_transfer_edge_creates_labelled_relation():
    records = [
        TransferRecord(
            tx_hash="0x1",
            block_number=1,
            from_address=A,
            to_address=B,
            amount_tao=Decimal("5"),
            edge_type="stake_transfer",
            netuid=72,
            hotkey=HOTKEY,
        )
    ]
    report = discover(A, records, max_depth=3)
    assert report.edges[0].edge_type == "stake_transfer"
    assert report.edges[0].netuid == 72
    related_b = next(row for row in report.related if row.address == B)
    assert related_b.stake_transfer_count == 1
    assert any("子网 stake 转移边" in warning for warning in report.warnings)


def test_stake_transfer_respects_second_hop_amount_threshold():
    records = [
        TransferRecord(
            tx_hash="0x1", block_number=1, from_address=A, to_address=B,
            amount_tao=Decimal("0.01"), edge_type="stake_transfer", netuid=72,
        ),
        TransferRecord(
            tx_hash="0x2", block_number=2, from_address=B, to_address=HOTKEY,
            amount_tao=Decimal("0.01"), edge_type="stake_transfer", netuid=72,
        ),
    ]
    report = discover(
        A, records, max_depth=3,
        policy=LinkPolicy(min_edge_tao=Decimal("1"), min_total_tao=Decimal("1")),
    )
    assert all(row.address != HOTKEY for row in report.related)


def test_database_migrates_legacy_transfers_table():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE transfers ("
            "id INTEGER PRIMARY KEY, event_key VARCHAR(240), tx_hash VARCHAR(160), "
            "block_number INTEGER, block_hash VARCHAR(80), event_index INTEGER, "
            "from_address VARCHAR(64), to_address VARCHAR(64), amount_rao INTEGER, "
            "amount_tao NUMERIC NOT NULL, timestamp VARCHAR(32), created_at DATETIME)"
        )
        connection.commit()
        connection.close()

        database = Database(path)
        try:
            database.initialize()
            with database.engine.connect() as conn:
                columns = {
                    row[1] for row in conn.execute(text("PRAGMA table_info(transfers)"))
                }
            assert {"edge_type", "netuid", "hotkey"} <= columns

            inserted = database.ingest_transfers(
                [
                    {
                        "event_key": "stake:1:0:a:b:1000000000",
                        "tx_hash": "0x1",
                        "block_number": 1,
                        "block_hash": "0x",
                        "event_index": 0,
                        "from_address": A,
                        "to_address": B,
                        "amount_rao": 1_000_000_000,
                        "amount_tao": Decimal("1"),
                        "timestamp": "",
                        "edge_type": "stake_transfer",
                        "netuid": 72,
                        "hotkey": HOTKEY,
                    }
                ]
            )
            assert inserted == 1
            with database.session() as session:
                rows = to_relation_records(session.scalars(select(Transfer)).all())
            assert rows[0].edge_type == "stake_transfer"
            assert rows[0].netuid == 72
        finally:
            database.engine.dispose()


def test_stake_event_client_parses_response_and_caches(monkeypatch):
    monkeypatch.setenv("TAOSTATS_API_KEY", "test-secret")
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "100-1",
                        "coldkey": A,
                        "action": "unstake",
                        "amount": "2000000000",
                        "block_number": 100,
                        "is_transfer": True,
                        "netuid": 72,
                        "transfer_address": {"ss58": B},
                        "timestamp": "2026-11-04T00:00:00Z",
                    }
                ],
                "pagination": {
                    "next_page": None,
                    "per_page": 200,
                    "total_items": 1,
                    "total_pages": 1,
                },
            },
            request=httpx.Request("GET", taostats.STAKE_EVENTS_URL),
        )

    monkeypatch.setattr(taostats, "_http_get", as_http_get(fake_get))
    page = get_stake_transfer_page(A)
    cached = get_stake_transfer_page(A)

    assert page.rows[0]["amount"] == "2000000000"
    assert page.next_page is None
    assert page.truncated_by_window is False
    assert cached.from_cache is True
    assert len(calls) == 1
    assert calls[0][1]["params"]["is_transfer"] == "true"


def test_stake_counterparty_accepts_ss58_and_rejects_hex():
    assert _stake_counterparty({"transfer_address": B}) == B
    assert _stake_counterparty({"transfer_address": {"ss58": B}}) == B
    assert _stake_counterparty({"transfer_address": {"ss58": "0xdeadbeef"}}) == ""
    assert _stake_counterparty({}) == ""


def test_taostats_stake_legs_join_the_relation_graph(monkeypatch):
    monkeypatch.setattr(
        service_module, "query_account_balances", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        service_module,
        "get_transfer_page",
        lambda address, page: TransferPage([], None),
    )

    def fake_stake_page(address, page):
        if address != A:
            return TransferPage([], None)
        return TransferPage(
            [
                {
                    "id": "100-1",
                    "coldkey": A,
                    "action": "unstake",
                    "amount": "3000000000",
                    "block_number": 100,
                    "is_transfer": True,
                    "netuid": 72,
                    "transfer_address": {"ss58": B},
                    "extrinsic_id": "100-1",
                    "hotkey": HOTKEY,
                    "timestamp": "2026-11-04T00:00:00Z",
                }
            ],
            None,
        )

    monkeypatch.setattr(service_module, "get_stake_transfer_page", fake_stake_page)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "stake.db")
        try:
            database.initialize()
            result = LinkService(database, "wss://example.invalid").relations(
                seed=A,
                depth=3,
                min_edge_tao="0.1",
                min_total_tao="0.1",
                data_source="taostats",
            )
            assert result["stake_transfer_count"] == 1
            assert result["taostats_transfer_count"] == 0
            related_b = next(row for row in result["related"] if row["address"] == B)
            assert related_b["stake_transfer_count"] == 1
            assert result["edges"][0]["edge_type"] == "stake_transfer"
            assert result["api_requests"] >= 1
        finally:
            database.engine.dispose()
