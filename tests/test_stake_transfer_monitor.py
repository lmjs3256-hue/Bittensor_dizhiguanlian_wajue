from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import app.main as main_module
from app.event_monitor.address_monitor import (
    address_event_record,
    derived_stake_transfer_buy_raw,
)
from app.event_monitor.chain import ChainCollector
from app.event_monitor.rules import load_rules
from app.event_monitor.server import EventHub
from app.event_monitor.storage import Store


OWNER = "5FUfruyVDoDsCty1Sh7tCmHVdpoC3XV1nUZYjhcpfe31Defk"
DESTINATION = "5G41MdZMCD5jSAkNRaGFBLt8L4HWNAbcveBTxiSRynRzEABv"
HOTKEY = "5DUB7kNLvvx8Dj7D8tn54N1C7Xok6GodNPQE2WECCaL9Wgpr"
BLOCK_HASH = "0xdc26b577a5463d963976a2ecfe71f8fe274980797b011df2e59c8f79503f59fb"


def _raw_stake_removed() -> dict:
    return {
        "raw_id": "finney:" + BLOCK_HASH + ":7",
        "event_index": 7,
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


def _watched_destination() -> dict:
    return {
        "address_id": 100,
        "address": DESTINATION,
        "label": "yKQG",
        "library_book": "SN4 Targon",
        "manual_note": "",
        "address_role": "",
        "min_transfer_tao": 0,
        "created_at": "2026-11-18T00:00:00Z",
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


def _seed_transfer(db_path) -> None:
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


def test_stake_removed_stays_sell_and_derives_destination_buy(tmp_path, monkeypatch):
    db_path = tmp_path / "wallet_link.db"
    _seed_transfer(db_path)
    monkeypatch.setenv("WALLET_LINK_DB", str(db_path))

    source_event = address_event_record(
        _raw_stake_removed(),
        network="finney",
        watched_addresses=_watched_owner(),
        block_hash=BLOCK_HASH,
        height=9534717,
        spec_version=491,
        event_at="2026-11-18T16:45:48Z",
    )
    assert source_event is not None
    assert source_event["event_type"] == "解质押"
    assert source_event["event_name"] == "SubtensorModule.StakeRemoved"
    assert source_event["details"]["address_action"] == "sell"
    assert source_event["details"]["stake_transfer_to"] == DESTINATION
    assert source_event["summary"] == "SN54 · 4.67 TAO · 5FUf…Defk"

    derived_raw = derived_stake_transfer_buy_raw(_raw_stake_removed())
    assert derived_raw is not None
    buy_event = address_event_record(
        derived_raw,
        network="finney",
        watched_addresses=[*_watched_owner(), _watched_destination()],
        block_hash=BLOCK_HASH,
        height=9534717,
        spec_version=491,
        event_at="2026-11-18T16:45:48Z",
    )
    assert buy_event is not None
    assert buy_event["event_type"] == "质押"
    assert buy_event["event_name"] == "SubtensorModule.StakeAdded"
    assert buy_event["details"]["address_action"] == "buy"
    assert buy_event["details"]["coldkey"] == DESTINATION
    assert buy_event["details"]["primary_address"] == DESTINATION
    assert buy_event["details"]["stake_transfer_from"] == OWNER
    assert buy_event["summary"] == "SN54 · 4.67 TAO · 5G41…EABv"
    assert buy_event["event_id"] != source_event["event_id"]


def test_chain_mapping_emits_sell_and_buy_for_one_stake_removed(tmp_path, monkeypatch):
    db_path = tmp_path / "wallet_link.db"
    _seed_transfer(db_path)
    monkeypatch.setenv("WALLET_LINK_DB", str(db_path))
    root = Path(__file__).resolve().parents[1]
    rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
    collector = ChainCollector(
        url="unused", network="finney", store=object(), rules=rules,
        publish=lambda *args: None, status=lambda *args: None,
    )
    raw_event = {
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": 6,
        "event_index": 7,
        "module_id": "SubtensorModule",
        "event_id": "StakeRemoved",
        "attributes": [OWNER, HOTKEY, 4671597050, 1059145730702, 54, 0],
    }
    try:
        raws, mapped = collector._map_block_events(
            [raw_event],
            watched_addresses=[*_watched_owner(), _watched_destination()],
            disabled_netuids=set(),
            block_hash=BLOCK_HASH,
            height=9534717,
            spec_version=491,
            event_at="2026-11-18T16:45:48Z",
        )
    finally:
        collector.close()
    address_events = [item for item in mapped if item["source"] == "address"]
    assert len(address_events) == 2
    sell = next(item for item in address_events if item["details"]["address_action"] == "sell")
    buy = next(item for item in address_events if item["details"]["address_action"] == "buy")
    assert sell["summary"] == "SN54 · 4.67 TAO · 5FUf…Defk"
    assert buy["summary"] == "SN54 · 4.67 TAO · 5G41…EABv"
    assert len(raws) == 1


def test_chain_mapping_uses_extrinsic_destination_before_local_index(tmp_path, monkeypatch):
    missing_wallet = tmp_path / "missing-wallet.db"
    monkeypatch.setenv("WALLET_LINK_DB", str(missing_wallet))
    root = Path(__file__).resolve().parents[1]
    rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
    collector = ChainCollector(
        url="unused", network="finney", store=object(), rules=rules,
        publish=lambda *args: None, status=lambda *args: None,
    )
    raw_event = {
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": 6,
        "event_index": 7,
        "module_id": "SubtensorModule",
        "event_id": "StakeRemoved",
        "attributes": [OWNER, HOTKEY, 4671597050, 1059145730702, 54, 0],
    }
    transfer_call = {
        "extrinsic_hash": "0x" + "f" * 64,
        "call": {
            "call_module": "Proxy",
            "call_function": "proxy",
            "call_args": [
                {"name": "real", "value": OWNER},
                {
                    "name": "call",
                    "value": {
                        "call_module": "SubtensorModule",
                        "call_function": "transfer_stake",
                        "call_args": [
                            {"name": "destination_coldkey", "value": DESTINATION},
                            {"name": "hotkey", "value": HOTKEY},
                            {"name": "origin_netuid", "value": 54},
                            {"name": "destination_netuid", "value": 54},
                            {"name": "alpha_amount", "value": 1059145730702},
                        ],
                    },
                },
            ],
        },
    }
    try:
        _raws, mapped = collector._map_block_events(
            [raw_event],
            watched_addresses=[*_watched_owner(), _watched_destination()],
            disabled_netuids=set(),
            block_hash=BLOCK_HASH,
            height=9534717,
            spec_version=491,
            event_at="2026-11-18T16:45:48Z",
            extrinsics=[None, None, None, None, None, None, transfer_call],
        )
    finally:
        collector.close()
    address_events = [item for item in mapped if item["source"] == "address"]
    assert len(address_events) == 2
    buy = next(item for item in address_events if item["details"]["address_action"] == "buy")
    assert buy["details"]["primary_address"] == DESTINATION
    assert buy["summary"] == "SN54 · 4.67 TAO · 5G41…EABv"


def test_reconcile_upgrades_existing_unstake_and_adds_destination_buy(tmp_path, monkeypatch):
    wallet_path = tmp_path / "wallet_link.db"
    monkeypatch.setenv("WALLET_LINK_DB", str(wallet_path))
    event_store = Store(tmp_path / "event_monitor.db")
    source_event = address_event_record(
        _raw_stake_removed(),
        network="finney",
        watched_addresses=_watched_owner(),
        block_hash=BLOCK_HASH,
        height=9534717,
        spec_version=491,
        event_at="2026-11-18T16:45:48Z",
    )
    assert source_event is not None
    event_store.create_event(source_event)
    event_store.add_watched_address(
        "finney", OWNER, "SN54 项目方", library_book="SN54 Yanez",
        address_role="project_owner",
    )
    event_store.add_watched_address(
        "finney", DESTINATION, "yKQG", library_book="SN54 Yanez",
    )
    _seed_transfer(wallet_path)
    context = SimpleNamespace(store=event_store, hub=EventHub())
    monkeypatch.setattr(
        main_module, "EVENT_MONITOR", SimpleNamespace(running=True, context=context),
    )

    updated = main_module._reconcile_stake_transfer_events()

    assert updated == 2
    repaired_source = event_store.get_event(source_event["event_id"])
    assert repaired_source["event_type"] == "解质押"
    assert repaired_source["details"]["address_action"] == "sell"
    assert repaired_source["details"]["stake_transfer_to"] == DESTINATION
    address_events = event_store.list_events(
        {"network": "finney", "source": "address"}, limit=20,
    )
    assert len(address_events) == 2
    buy = next(event for event in address_events if event["details"].get("address_action") == "buy")
    assert buy["event_type"] == "质押"
    assert buy["summary"] == "SN54 · 4.67 TAO · 5G41…EABv"


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
