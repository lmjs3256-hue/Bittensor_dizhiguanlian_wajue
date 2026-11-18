from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import app.main as main_module
from app.event_monitor.chain import ChainCollector
from app.event_monitor.rules import load_rules


def test_removed_event_archives_library_and_reconciles_watch(monkeypatch):
    archive = Mock(return_value={
        "netuid": 109,
        "name": "Old Project",
        "cleared_addresses": 4,
    })
    reconcile = Mock(return_value={"removed": 4, "added": 0})
    monkeypatch.setattr(main_module.SERVICE, "archive_subnet_owner_library", archive)
    monkeypatch.setattr(main_module, "_subnet_has_watched_addresses", lambda _netuid: True)
    monkeypatch.setattr(main_module, "_sync_event_monitor_library_state", reconcile)
    main_module._SUBNET_LIFECYCLE_WATCH_STATE.clear()

    result = main_module._process_subnet_lifecycle_event({
        "rule_id": "network_removed",
        "netuid": 109,
        "event_id": "a" * 64,
    })

    archive.assert_called_once_with(109, reason="network_removed_event")
    reconcile.assert_called_once_with()
    assert result["event"] == "network_removed"
    assert result["result"]["cleared_addresses"] == 4
    assert main_module._SUBNET_LIFECYCLE_WATCH_STATE[109] is True


def test_registered_event_syncs_owner_reconciles_and_restores_watch(monkeypatch):
    sync = Mock(return_value={"inserted": 1, "updated": 0, "restored": 0})
    reconcile = Mock(return_value={"removed": 1, "added": 1})
    ensure = Mock(return_value=1)
    monkeypatch.setattr(main_module.SERVICE, "sync_subnet_owner_addresses", sync)
    monkeypatch.setattr(main_module, "_sync_event_monitor_library_state", reconcile)
    monkeypatch.setattr(main_module, "_ensure_subnet_owner_watched", ensure)
    main_module._SUBNET_LIFECYCLE_WATCH_STATE.clear()
    main_module._SUBNET_LIFECYCLE_WATCH_STATE[109] = True

    result = main_module._process_subnet_lifecycle_event({
        "rule_id": "network_registered",
        "netuid": 109,
        "event_id": "b" * 64,
    })

    sync.assert_called_once_with()
    reconcile.assert_called_once_with()
    ensure.assert_called_once_with(109)
    assert result["result"]["watch_restored"] == 1
    assert 109 not in main_module._SUBNET_LIFECYCLE_WATCH_STATE


def test_chain_persist_block_invokes_lifecycle_callback():
    store = Mock()
    event = {
        "event_id": "c" * 64,
        "netuid": 109,
        "details": {"rule_id": "network_registered"},
        "enrichment_status": "complete",
    }
    store.process_chain_block.return_value = [event]
    callback = Mock()
    collector = ChainCollector(
        url="unused",
        network="finney",
        store=store,
        rules={},
        publish=lambda *_args: None,
        status=lambda *_args: None,
        subnet_lifecycle_callback=callback,
    )
    collector.start_repository_sync = Mock()
    try:
        collector._persist_block({
            "height": 100,
            "block_hash": "0x" + "d" * 64,
            "parent_hash": "0x" + "c" * 64,
            "spec_version": 491,
            "raws": [],
            "events": [],
        })
    finally:
        collector.close()

    callback.assert_called_once()
    assert callback.call_args.args[0] == "network_registered"
    assert callback.call_args.args[1] == 109
    assert callback.call_args.args[2]["event_id"] == "c" * 64

def test_lifecycle_event_is_not_suppressed_by_disabled_netuid():
    root = Path(__file__).resolve().parents[1]
    rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
    collector = ChainCollector(
        url="unused", network="finney", store=object(), rules=rules,
        publish=lambda *_args: None, status=lambda *_args: None,
    )
    event = {
        "event_index": 1,
        "module_id": "SubtensorModule",
        "event_id": "NetworkRemoved",
        "attributes": [109],
        "phase": "ApplyExtrinsic",
    }
    try:
        _raws, mapped = collector._map_block_events(
            [event],
            watched_addresses=[],
            disabled_netuids={109},
            block_hash="0x" + "e" * 64,
            height=103,
            spec_version=491,
            event_at="2026-11-19T00:00:00.000Z",
        )
    finally:
        collector.close()
    assert any(
        item["event_name"] == "SubtensorModule.NetworkRemoved"
        and item["details"]["rule_id"] == "network_removed"
        for item in mapped
    )
