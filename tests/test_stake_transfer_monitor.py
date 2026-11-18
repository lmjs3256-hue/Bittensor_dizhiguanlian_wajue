from __future__ import annotations

import sqlite3

from app.event_monitor.address_monitor import address_event_record


OWNER = "5FUfruyVDoDsCty1Sh7tCmHVdpoC3XV1nUZYjhcpfe31Defk"
DESTINATION = "5G41MdZMCD5jSAkNRaGFBLt8L4HWNAbcveBTxiSRynRzEABv"
HOTKEY = "5DUB7kNLvvx8Dj7D8tn54N1C7Xok6GodNPQE2WECCaL9Wgpr"
BLOCK_HASH = "0xdc26b577a5463d963976a2ecfe71f8fe274980797b011df2e59c8f79503f59fb"


def _raw_stake_removed() -> dict:
    return {
        "raw_id": "finney:" + BLOCK_HASH + ":7",
        "module": "SubtensorModule",
        "event_name": "StakeRemoved",
        "raw": {
            "height": 9534717,
            "block_hash": BLOCK_HASH,
            "extrinsic_idx": 12,
            "attributes": {
                "0": OWNER,
                "1": HOTKEY,
                "2": 4671597050,
                "3": 1059145730702,
                "4": 54,
                "5": 0,
            },
        },
    }


def _watched_owner() -> list[dict]:
    return [{
        "address_id": 99,
        "address": OWNER,
        "label": "SN54 项目方",
        "library_book": "SN54 Yanez",
        "manual_note": "",
        "address_role": "project_owner",
        "min_transfer_tao": 0,
        "created_at": "2026-11-18T00:00:00Z",
    }]


def _create_transfers_table(path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE transfers (
                from_address TEXT, to_address TEXT, amount_rao INTEGER,
                amount_tao TEXT, netuid INTEGER, hotkey TEXT, tx_hash TEXT,
                block_hash TEXT, edge_type TEXT, event_index INTEGER
            )
        """)


def test_stake_removed_is_rendered_as_transfer_with_direction(tmp_path, monkeypatch):
    db_path = tmp_path / "wallet_link.db"
    _create_transfers_table(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                OWNER, DESTINATION, 4671597050, "4.67159705", 54, HOTKEY,
                "0x26750d002bdbbd5c1237e51d4e46eb7a10bcf46692e87b82058795afc3e063a1",
                BLOCK_HASH, "stake_transfer", 7,
            ),
        )
    monkeypatch.setenv("WALLET_LINK_DB", str(db_path))

    event = address_event_record(
        _raw_stake_removed(),
        network="finney",
        watched_addresses=_watched_owner(),
        block_hash=BLOCK_HASH,
        height=9534717,
        spec_version=491,
        event_at="2026-11-18T16:45:48Z",
    )

    assert event is not None
    assert event["event_type"] == "代币转移"
    assert event["event_name"] == "SubtensorModule.StakeTransferred"
    assert event["details"]["origin_coldkey"] == OWNER
    assert event["details"]["destination_coldkey"] == DESTINATION
    assert "转移 4.67 TAO" in event["summary"]
    assert "5FUf…Defk → 5G41…EABv" in event["summary"]


def test_stake_removed_without_transfer_row_remains_unstake(tmp_path, monkeypatch):
    db_path = tmp_path / "wallet_link.db"
    _create_transfers_table(db_path)
    monkeypatch.setenv("WALLET_LINK_DB", str(db_path))

    event = address_event_record(
        _raw_stake_removed(),
        network="finney",
        watched_addresses=_watched_owner(),
        block_hash=BLOCK_HASH,
        height=9534717,
        spec_version=491,
        event_at="2026-11-18T16:45:48Z",
    )

    assert event is not None
    assert event["event_type"] == "解质押"
    assert event["details"]["coldkey"] == OWNER
