from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.event_monitor.address_monitor import address_event_record, mask_address
from app.event_monitor.chain import ChainCollector, _decode_identity_v3, _identity_to_profile
from app.event_monitor.github import (
    GitHubBacklogError, GitHubClient, GitHubCollector, GitHubError,
    _commit_change_brief, _commit_progress_points, parse_github_repository,
)
from app.event_monitor.rules import load_rules, load_simulation_signals
from app.event_monitor.server import AppContext, EventHub, MonitorHTTPServer, WorkerStatus
from app.event_monitor.storage import Store


def make_event(event_id: str = "a" * 64, *, height: int = 10, netuid: int = 55) -> dict:
    return {
        "event_id": event_id,
        "network": "finney",
        "netuid": netuid,
        "event_type": "身份信息变更",
        "event_name": "SubtensorModule.SubnetIdentitySet",
        "source": "chain",
        "source_url": None,
        "event_at": f"2026-11-11T00:0{height % 10}:00.000Z",
        "summary": "身份信息正在核验",
        "details": {"rule_id": "subnet_identity_set"},
        "raw_data": {"attributes": {"netuid": netuid}},
        "block_number": height,
        "block_hash": f"0x{height:064x}",
        "event_index": 1,
        "runtime_spec_version": 488,
        "chain_status": "observed",
        "enrichment_status": "pending",
        "confidence": "high",
    }


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "events.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_old_watched_address_schema_gains_manual_note_column(self) -> None:
        path = Path(self.temp.name) / "old-schema.db"
        db = sqlite3.connect(path)
        try:
            db.execute("""
                CREATE TABLE watched_addresses (
                    address_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    network TEXT NOT NULL,
                    address TEXT NOT NULL,
                    label TEXT NOT NULL,
                    min_transfer_tao REAL NOT NULL DEFAULT 0,
                    library_book TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(network, address)
                )
            """)
            db.execute(
                "INSERT INTO watched_addresses(network,address,label,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("finney", "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234", "旧备注", "now", "now"),
            )
            db.commit()
        finally:
            db.close()
        migrated = Store(path)
        items = migrated.list_watched_addresses("finney")
        self.assertEqual(items[0]["label"], "旧备注")
        self.assertEqual(items[0]["manual_note"], "")
        db = sqlite3.connect(path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(watched_addresses)")}
        finally:
            db.close()
        self.assertIn("manual_note", columns)

    def test_watched_address_manual_note_is_separate_and_editable(self) -> None:
        item = self.store.add_watched_address(
            "finney",
            "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "SN53 项目方",
            library_book="SN53 NIOME",
            manual_note="回购、官方",
        )
        self.assertEqual(item["label"], "SN53 项目方")
        self.assertEqual(item["manual_note"], "回购、官方")
        updated = self.store.update_watched_address(item["address_id"], manual_note="出货")
        self.assertEqual(updated["label"], "SN53 项目方")
        self.assertEqual(updated["manual_note"], "出货")

    def test_event_is_idempotent_and_updates_same_record(self) -> None:
        event = make_event()
        first, created = self.store.create_event(event)
        self.assertTrue(created)
        duplicate, created_again = self.store.create_event(event)
        self.assertFalse(created_again)
        self.assertEqual(first["event_id"], duplicate["event_id"])

        updated = self.store.update_event(
            event["event_id"],
            "enriched",
            {
                "summary": "SN55 名称变更：旧名 → 新名",
                "enrichment_status": "complete",
                "details": {"identity_changes": {"subnet_name": {"before": "旧名", "after": "新名"}}},
            },
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated["summary"], "SN55 名称变更：旧名 → 新名")
        self.assertEqual(updated["update_count"], 1)
        self.assertEqual(len(self.store.list_events({})), 1)
        self.assertEqual([item["change_type"] for item in self.store.event_history(event["event_id"])],
                         ["discovered", "enriched"])

    def test_existing_event_summaries_are_backfilled_with_subnet_prefix(self) -> None:
        path = Path(self.temp.name) / "summary-backfill.db"
        store = Store(path)
        chain_event = make_event("1" * 64, netuid=None)
        chain_event.update({
            "event_type": "子网移除",
            "event_name": "SubtensorModule.NetworkRemoved",
            "summary": "子网已从网络中移除",
            "details": {"rule_id": "network_removed"},
            "raw_data": {
                "module": "SubtensorModule",
                "event_name": "NetworkRemoved",
                "attributes": {},
                "value": {"attributes": 55},
            },
        })
        store.create_event(chain_event)
        stake_event = make_event("2" * 64, netuid=78)
        stake_event.update({
            "event_type": "解质押",
            "event_name": "SubtensorModule.StakeRemoved",
            "source": "address",
            "summary": "团队钱包 · 5HCF…gDHh · SN78 卖出 1.00 TAO",
        })
        store.create_event(stake_event)
        transfer_event = make_event("3" * 64, netuid=None)
        transfer_event.update({
            "event_type": "普通转账",
            "event_name": "Balances.Transfer",
            "source": "address",
            "summary": "SN不适用 · TAO 转账 · 5HCF…gDHh → 5FHn…94ty · 1 TAO",
        })
        store.create_event(transfer_event)

        reopened = Store(path)
        repaired_chain = reopened.get_event(chain_event["event_id"])
        self.assertEqual(repaired_chain["netuid"], 55)
        self.assertEqual(repaired_chain["summary"], "SN55 · 子网已从网络中移除")
        self.assertEqual(
            reopened.get_event(stake_event["event_id"])["summary"],
            "SN78 · 团队钱包 · 5HCF…gDHh · 卖出 1.00 TAO",
        )
        self.assertEqual(
            reopened.get_event(transfer_event["event_id"])["summary"],
            "TAO 转账 · 5HCF…gDHh → 5FHn…94ty · 1 TAO",
        )

    def test_block_replay_and_reorg_retraction(self) -> None:
        event_11 = make_event("b" * 64, height=11)
        raw = {
            "raw_id": "finney:0xblock11:1",
            "event_index": 1,
            "module": "SubtensorModule",
            "event_name": "SubnetIdentitySet",
            "raw": {"height": 11},
        }
        first = self.store.process_chain_block(
            "finney", 11, "0x" + "1" * 64, "0x" + "0" * 64, 488, [raw], [event_11]
        )
        second = self.store.process_chain_block(
            "finney", 11, "0x" + "1" * 64, "0x" + "0" * 64, 488, [raw], [event_11]
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

        retracted = self.store.retract_after("finney", 10)
        self.assertEqual(len(retracted), 1)
        stored = self.store.get_event(event_11["event_id"])
        self.assertEqual(stored["chain_status"], "retracted")
        self.assertIn("链重组更正", stored["summary"])
        self.assertEqual(self.store.get_block("finney", 11), None)
        self.assertIn("retracted", [
            item["change_type"] for item in self.store.event_history(event_11["event_id"])
        ])

    def test_subnet_history_is_ascending(self) -> None:
        self.store.create_event(make_event("c" * 64, height=12, netuid=7))
        self.store.create_event(make_event("d" * 64, height=10, netuid=7))
        rows = self.store.list_events({"network": "finney", "netuid": 7}, ascending=True)
        self.assertEqual([row["block_number"] for row in rows], [10, 12])

    def test_runtime_metadata_is_versioned(self) -> None:
        self.assertFalse(self.store.has_runtime_metadata("finney", 488))
        self.store.save_runtime_metadata(
            "finney", 488, "0x" + "a" * 64, "hash-488", "0x1234"
        )
        self.assertTrue(self.store.has_runtime_metadata("finney", 488))
        self.assertFalse(self.store.has_runtime_metadata("test", 488))
        self.assertEqual(self.store.list_runtime_metadata("finney")[0]["metadata_hex_length"], 6)
        self.assertEqual(self.store.get_runtime_metadata("finney", 488)["metadata_hex"], "0x1234")

    def test_stale_chain_repository_is_deactivated(self) -> None:
        chain_repo = self.store.upsert_chain_repository({
            "network": "finney", "netuid": 55, "owner": "acme", "name": "subnet",
            "html_url": "https://github.com/acme/subnet",
        })
        manual = self.store.add_repository({
            "network": "finney", "netuid": 56, "owner": "acme", "name": "manual",
            "html_url": "https://github.com/acme/manual",
            "relation_source": "manual_review", "reviewed_by": "tester",
        })
        self.assertEqual(self.store.reconcile_chain_repositories("finney", set()), 1)
        rows = {row["repository_id"]: row for row in self.store.list_repositories()}
        self.assertEqual(rows[chain_repo["repository_id"]]["active"], 0)
        self.assertEqual(rows[manual["repository_id"]]["active"], 1)

    def test_netuid_repository_move_deactivates_previous_mapping(self) -> None:
        first = self.store.upsert_chain_repository({
            "network": "finney", "netuid": 55, "owner": "old", "name": "repo",
            "html_url": "https://github.com/old/repo",
        })
        second = self.store.upsert_chain_repository({
            "network": "finney", "netuid": 55, "owner": "new", "name": "repo",
            "html_url": "https://github.com/new/repo",
        })
        self.store.deactivate_chain_repositories_for_netuid(
            "finney", 55, second["repository_id"]
        )
        rows = {row["repository_id"]: row for row in self.store.list_repositories()}
        self.assertEqual(rows[first["repository_id"]]["active"], 0)
        self.assertEqual(rows[second["repository_id"]]["active"], 1)

    def test_alert_family_can_be_resolved_together(self) -> None:
        self.store.add_alert("github:acme/repo", "warning", "timeout")
        self.store.add_alert("github", "warning", "legacy")
        self.store.resolve_alert_family("github:")
        self.assertEqual([item["component"] for item in self.store.list_alerts()], ["github"])

    def test_logs_are_persisted_and_filterable(self) -> None:
        self.store.add_log("INFO", "app.chain", "collector connected")
        self.store.add_log("WARNING", "app.github", "rate limited")
        self.assertEqual(self.store.list_logs(level="WARNING")[0]["component"], "app.github")
        self.assertEqual(
            self.store.list_logs(component="app.chain")[0]["message"],
            "collector connected",
        )

    def test_trading_state_is_upserted(self) -> None:
        self.store.set_trading_state(
            network="finney", netuid=99, enabled=True, registration_block=9447220,
            block_hash="0x" + "a" * 64, block_number=9477119,
        )
        self.store.set_trading_state(
            network="finney", netuid=99, enabled=False, registration_block=None,
            block_hash="0x" + "b" * 64, block_number=9477120,
        )
        state = self.store.list_trading_states("finney")[0]
        self.assertFalse(state["enabled"])
        self.assertEqual(state["registration_block"], 9447220)

    def test_subnet_profile_auto_sync_preserves_manual_override(self) -> None:
        self.store.upsert_subnet_profile_auto(
            "finney", 99,
            {"name": "Chain Name", "website_url": "https://example.org", "x_url": ""},
            "0x" + "a" * 64,
        )
        edited = self.store.save_subnet_profile_override(
            "finney", 99, {"name": "Reviewed Name"}, "tester",
        )
        self.assertEqual(edited["name"], "Reviewed Name")
        self.assertEqual(edited["website_url"], "https://example.org")
        self.assertEqual(edited["field_sources"]["name"], "manual")

    def test_profile_archive_is_preserved_and_new_registration_starts_blank(self) -> None:
        self.store.upsert_subnet_profile_auto(
            "finney", 77,
            {"name": "Old Project", "github_url": "https://github.com/old/project"},
            "0x" + "a" * 64,
        )
        self.store.save_subnet_profile_override(
            "finney", 77, {"website_url": "https://old.example"}, "tester",
        )
        self.assertTrue(self.store.archive_subnet_profile(
            "finney", 77, "network_removed", archive_key="event:removed",
        ))
        current = self.store.get_subnet_profile("finney", 77)
        self.assertFalse(current["active"])
        self.assertNotIn("name", current)
        archived = self.store.list_archived_subnet_profiles("finney")
        self.assertEqual(archived[0]["name"], "Old Project")
        self.assertEqual(archived[0]["github_url"], "https://github.com/old/project")
        self.assertEqual(archived[0]["archive_reason"], "network_removed")

        self.store.reset_subnet_profile_for_registration(
            "finney", 77, archive_key="event:registered",
        )
        recreated = self.store.get_subnet_profile("finney", 77)
        self.assertTrue(recreated["active"])
        self.assertNotIn("name", recreated)
        self.assertNotIn("github_url", recreated)
        self.assertEqual(recreated["overrides"], {})

    def test_reconcile_does_not_restore_archived_values(self) -> None:
        self.store.upsert_subnet_profile_auto(
            "finney", 88, {"name": "Previous SN88"}, "0x" + "a" * 64,
        )
        self.store.reconcile_subnet_profiles("finney", set())
        self.assertFalse(self.store.get_subnet_profile("finney", 88)["active"])
        self.store.reconcile_subnet_profiles("finney", {88})
        recreated = self.store.get_subnet_profile("finney", 88)
        self.assertTrue(recreated["active"])
        self.assertNotIn("name", recreated)
        self.assertEqual(self.store.list_archived_subnet_profiles("finney")[0]["name"], "Previous SN88")

    def test_monitoring_selection_filters_github_queue_and_priority_reenables(self) -> None:
        self.store.upsert_subnet_profile_auto(
            "finney", 55, {"name": "NIOME"}, "0x" + "a" * 64,
        )
        self.store.add_repository({
            "network": "finney", "netuid": 55, "owner": "acme", "name": "subnet",
            "html_url": "https://github.com/acme/subnet",
            "relation_source": "chain_identity", "reviewed_by": "chain",
        })
        self.store.set_subnet_monitoring("finney", [55], False)
        self.assertEqual(self.store.list_polling_repositories("finney"), [])
        self.store.set_subnet_priority("finney", [55], 2)
        rows = self.store.list_polling_repositories("finney")
        self.assertEqual(rows[0]["netuid"], 55)
        self.assertEqual(rows[0]["priority_level"], 2)


class ScaleValue:
    def __init__(self, value):
        self.value = value


class ChainShapeTests(unittest.TestCase):
    def test_substrate_interface_event_shape_is_normalized(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        collector = ChainCollector(
            url="unused", network="finney",
            store=object(), rules=rules, publish=lambda *args: None,
            status=lambda *args: None,
        )
        event = ScaleValue({
            "event_index": 5,
            "module_id": "SubtensorModule",
            "event_id": "SubnetIdentitySet",
            "attributes": {"netuid": 55},
            "phase": "ApplyExtrinsic",
        })
        raw = collector._raw_record(event, "0x" + "a" * 64, 99, 5)
        self.assertEqual(raw["module"], "SubtensorModule")
        self.assertEqual(raw["event_name"], "SubnetIdentitySet")
        self.assertEqual(raw["raw"]["attributes"], {"netuid": 55})
        normalized = collector._event_record(raw, "0x" + "a" * 64, 99, 488,
                                             "2026-11-11T00:00:00.000Z")
        self.assertEqual(normalized["netuid"], 55)
        self.assertEqual(normalized["event_type"], "身份信息变更")

    def test_nested_substrate_event_id_is_normalized(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        collector = ChainCollector(
            url="unused", network="finney",
            store=object(), rules=rules, publish=lambda *args: None,
            status=lambda *args: None,
        )
        attributes = [
            "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "5HotKeyABCDEFGHIJKLMNOPQRSTUVWXYZ5678",
            1_250_000_000,
            2_500_000_000,
            78,
            10,
        ]
        event = ScaleValue({
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": 7,
            "event": {
                "event_index": "0202",
                "module_id": "SubtensorModule",
                "event_id": "StakeAdded",
                "attributes": attributes,
            },
            "event_index": 2,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": attributes,
        })
        raw = collector._raw_record(event, "0x" + "b" * 64, 100, 2)
        self.assertEqual(raw["module"], "SubtensorModule")
        self.assertEqual(raw["event_name"], "StakeAdded")
        address_event = address_event_record(
            raw,
            network="finney",
            watched_addresses=[{
                "address": "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
            }],
            block_hash="0x" + "b" * 64,
            height=100,
            spec_version=488,
            event_at="2026-11-11T00:00:00.000Z",
        )
        self.assertIsNotNone(address_event)
        self.assertEqual(address_event["netuid"], 78)
        self.assertEqual(address_event["event_type"], "质押")

    def test_catchup_progress_refreshes_chain_heartbeat(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        statuses = []

        class StoreStub:
            def get_cursor(self, _name):
                return {"height": 42, "spec_version": 491}

        collector = ChainCollector(
            url="unused", network="finney", store=StoreStub(), rules=rules,
            publish=lambda *_args: None,
            status=lambda *args: statuses.append(args),
        )
        try:
            collector._report_progress(target=100, processed=3)
        finally:
            collector.close()

        assert statuses[-1][0:2] == ("chain", "connected")
        details = statuses[-1][2]
        assert details["best_height"] == 100
        assert details["last_processed"] == 42
        assert details["lag_blocks"] == 58
        assert details["syncing"] is True
        assert details["updated_at"]

    def test_balance_transfer_is_suppressed_inside_matching_stake_extrinsic(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        collector = ChainCollector(
            url="unused", network="finney",
            store=object(), rules=rules, publish=lambda *args: None,
            status=lambda *args: None,
        )
        coldkey = "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        hotkey = "5HotKeyABCDEFGHIJKLMNOPQRSTUVWXYZ5678"
        transfer = ScaleValue({
            "event_index": 5,
            "module_id": "Balances",
            "event_id": "Transfer",
            "attributes": {
                "from": coldkey,
                "to": hotkey,
                "amount": 20_000_000_000,
            },
            "extrinsic_idx": 9,
            "phase": "ApplyExtrinsic",
        })
        stake_added = ScaleValue({
            "event_index": 7,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [coldkey, hotkey, 20_000_000_000, 40_000_000_000, 53, 1],
            "extrinsic_idx": 9,
            "phase": "ApplyExtrinsic",
        })
        raws, mapped = collector._map_block_events(
            [transfer, stake_added],
            watched_addresses=[{"address": coldkey, "label": "团队钱包"}],
            disabled_netuids=set(),
            block_hash="0x" + "c" * 64,
            height=101,
            spec_version=490,
            event_at="2026-11-11T00:00:00.000Z",
        )
        self.assertEqual([item["event_name"] for item in mapped], ["SubtensorModule.StakeAdded"])
        self.assertNotIn(
            "Balances.Transfer",
            [item["event_name"] for item in raws],
        )
        self.assertEqual(mapped[0]["netuid"], 53)
        self.assertTrue(mapped[0]["summary"].startswith("SN53 · "))
        self.assertIn("买入 20.00 TAO", mapped[0]["summary"])

    def test_scalar_substrate_params_fall_back_to_value_attributes(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        collector = ChainCollector(
            url="unused", network="finney",
            store=object(), rules=rules, publish=lambda *args: None,
            status=lambda *args: None,
        )

        class ScalarParamEvent:
            params = 17
            value = {
                "event_index": 2,
                "module_id": "SubtensorModule",
                "event_id": "SubnetIdentitySet",
                "attributes": {"netuid": 55},
            }
            event_index = 2
            phase = "ApplyExtrinsic"
            extrinsic_idx = 4

        raw = collector._raw_record(
            ScalarParamEvent(), "0x" + "d" * 64, 102, 2,
        )
        self.assertEqual(raw["module"], "SubtensorModule")
        self.assertEqual(raw["event_name"], "SubnetIdentitySet")
        self.assertEqual(raw["raw"]["attributes"], {"netuid": 55})
        self.assertEqual(raw["raw"]["value"]["attributes"], {"netuid": 55})

    def test_scalar_event_attribute_preserves_netuid_and_prefix(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        collector = ChainCollector(
            url="unused", network="finney",
            store=object(), rules=rules, publish=lambda *args: None,
            status=lambda *args: None,
        )

        class ScalarNetworkRemoved:
            params = 35
            value = {
                "event_index": 7,
                "module_id": "SubtensorModule",
                "event_id": "NetworkRemoved",
                "attributes": 35,
            }
            event_index = 7
            phase = "ApplyExtrinsic"
            extrinsic_idx = 2

        raw = collector._raw_record(
            ScalarNetworkRemoved(), "0x" + "e" * 64, 103, 7,
        )
        self.assertEqual(raw["raw"]["attributes"], {"0": 35})
        normalized = collector._event_record(
            raw, "0x" + "e" * 64, 103, 490,
            "2026-11-13T00:00:00.000Z",
        )
        self.assertEqual(normalized["netuid"], 35)
        self.assertTrue(normalized["summary"].startswith("SN35 "))
        self.assertNotEqual(normalized["summary"], "子网已从网络中移除")

    def test_all_chain_rule_summaries_start_with_subnet_marker(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        collector = ChainCollector(
            url="unused", network="finney",
            store=object(), rules=rules, publish=lambda *args: None,
            status=lambda *args: None,
        )
        for rule in rules.values():
            indexes = [
                value for value in rule.get("field_indices", {}).values()
                if isinstance(value, int)
            ]
            size = max(indexes, default=0) + 1
            values = [0] * size
            netuid_index = rule.get("netuid_index")
            if isinstance(netuid_index, int):
                values[netuid_index] = 17
            raw = {
                "raw_id": f"finney:test-{rule['id']}:0",
                "event_index": 0,
                "module": rule["module"],
                "event_name": rule["event_name"],
                "raw": {
                    "attributes": {str(index): value for index, value in enumerate(values)},
                    "value": {
                        "attributes": {str(index): value for index, value in enumerate(values)},
                    },
                },
            }
            normalized = collector._event_record(
                raw, "0x" + "f" * 64, 104, 490,
                "2026-11-13T00:00:00.000Z",
            )
            self.assertIsNotNone(normalized, rule["id"])
            self.assertRegex(normalized["summary"], r"^SN(?:\d|待定)")
            if isinstance(netuid_index, int):
                self.assertEqual(normalized["netuid"], 17, rule["id"])
                self.assertTrue(normalized["summary"].startswith("SN17 "), rule["id"])

    def test_discarded_state_fast_forwards_to_live_head(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "events.db")
            store.process_chain_block(
                "finney", 10, "0x" + "1" * 64, "0x" + "2" * 64,
                490, [], [],
            )
            statuses = []
            collector = ChainCollector(
                url="unused", network="finney", store=store, rules={},
                publish=lambda *args: None,
                status=lambda component, state, details: statuses.append(
                    (component, state, details)
                ),
            )
            collector.hash_at = lambda height: "0x" + "3" * 64
            collector.rpc = lambda method, params=None: {
                "parentHash": "0x" + "4" * 64
            }
            collector.version_at = lambda block_hash: 491
            snapshot = collector._recover_to_live_head(
                20, 10, RuntimeError("State already discarded for 0xold"),
            )
            cursor = store.get_cursor("chain:finney")
            self.assertEqual(cursor["height"], 20)
            self.assertTrue(snapshot["recovered"])
            self.assertEqual(snapshot["skipped_blocks"], 10)
            self.assertEqual(statuses[-1][0], "chain")
            self.assertEqual(statuses[-1][1], "connected")

    def test_scale_identity_decoder(self) -> None:
        fields = ["SN55", "https://github.com/acme/subnet", "", "", "", "", "", ""]

        def compact(value: str) -> bytes:
            raw = value.encode()
            assert len(raw) < 64
            return bytes([len(raw) << 2]) + raw

        value = "0x" + b"".join(compact(item) for item in fields).hex()
        decoded = _decode_identity_v3(value)
        self.assertEqual(decoded["subnet_name"], "SN55")
        self.assertEqual(decoded["github_repo"], "https://github.com/acme/subnet")

    def test_identity_profile_extracts_links(self) -> None:
        profile = _identity_to_profile({
            "subnet_name": "Example",
            "github_repo": "https://github.com/acme/subnet",
            "subnet_url": "https://example.org",
            "discord": "https://discord.gg/example",
            "description": "Example subnet",
            "logo_url": "https://example.org/logo.png",
            "additional": "Docs: https://docs.example.org | X: @example_ai",
        })
        self.assertEqual(profile["name"], "Example")
        self.assertEqual(profile["github_url"], "https://github.com/acme/subnet")
        self.assertEqual(profile["x_url"], "https://x.com/example_ai")

    def test_trading_false_to_true_pushes_one_event(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(__file__).resolve().parent.parent
            store = Store(Path(folder) / "events.db")
            rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
            published = []
            collector = ChainCollector(
                url="unused", network="finney", store=store, rules=rules,
                publish=lambda event, kind: published.append((event, kind)),
                status=lambda *args: None,
            )
            store.set_trading_state(
                network="finney", netuid=99, enabled=False, registration_block=9447220,
                block_hash="0x" + "a" * 64, block_number=100,
            )
            changed = ([{
                "netuid": 99, "enabled": True, "registration_block": 9447220,
                "block_hash": "0x" + "b" * 64,
            }], 101)
            with patch("app.event_monitor.chain._scan_trading_states_http", return_value=changed):
                collector._sync_trading_background(None)
                collector._sync_trading_background(None)
            events = store.list_events({"network": "finney", "netuid": 99})
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "交易开放")
            self.assertIn("已开放交易", events[0]["summary"])
            self.assertEqual(len(published), 1)
            self.assertEqual(published[0][1], "discovered")
            collector.close()

    def test_address_event_uses_trade_fields_direction_and_manual_note_tags(self) -> None:
        address = "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        watched = [{
            "address": address,
            "label": "SN53 项目方",
            "library_book": "SN53 NIOME",
            "manual_note": "回购、官方",
        }]

        def make_raw(event_name: str, amount: int, netuid: int = 53) -> dict:
            if event_name == "Transfer":
                attributes = {
                    "from": address,
                    "to": "5OtherKeyABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "amount": amount,
                }
            else:
                attributes = [address, "5HotKeyABCDEFGHIJKLMNOPQRSTUVWXYZ5678",
                              amount, amount * 2, netuid, 0]
            return {
                "raw_id": f"finney:test:{event_name}:{amount}",
                "event_index": 1,
                "module": "Balances" if event_name == "Transfer" else "SubtensorModule",
                "event_name": event_name,
                "raw": {"attributes": attributes},
            }

        common = {
            "network": "finney",
            "watched_addresses": watched,
            "block_hash": "0x" + "a" * 64,
            "height": 100,
            "spec_version": 491,
            "event_at": "2026-11-18T00:00:00.000Z",
        }
        buy = address_event_record(make_raw("StakeAdded", 20_000_000_000), **common)
        sell = address_event_record(make_raw("StakeRemoved", 5_000_000_000), **common)
        transfer = address_event_record(make_raw("Transfer", 2_000_000_000), **common)

        self.assertEqual(buy["summary"], "SN53 · 买入 20.00 TAO · 5Col…1234")
        self.assertEqual(buy["details"]["address_action"], "buy")
        self.assertEqual(buy["details"]["manual_note_tags"], ["【回购】", "【官方】"])
        self.assertEqual(
            buy["details"]["display_tags"],
            ["【SN53 项目方】", "【回购】", "【官方】"],
        )
        self.assertIn("卖出 5.00 TAO", sell["summary"])
        self.assertEqual(sell["details"]["address_action"], "sell")
        self.assertEqual(transfer["summary"], "全链 · 2 TAO · 5Col…1234")
        self.assertEqual(transfer["details"]["address_action"], "transfer")

    def test_project_owner_transfer_gets_prominent_source_suffix(self) -> None:
        coldkey = "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        raw = {
            "raw_id": "project-owner-transfer",
            "event_index": 1,
            "module": "Balances",
            "event_name": "Transfer",
            "raw": {
                "attributes": {
                    "from": coldkey,
                    "to": "5OtherKeyABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "amount": 1_000_000_000,
                }
            },
        }
        event = address_event_record(
            raw,
            network="finney",
            watched_addresses=[{
                "address": coldkey,
                "label": "SN46 项目方",
                "library_book": "SN46项目方相关",
            }],
            block_hash="0x" + "a" * 64,
            height=100,
            spec_version=491,
            event_at="2026-11-18T00:00:00.000Z",
        )
        self.assertEqual(event["summary"], "全链 · 1 TAO · 5Col…1234")
        self.assertIn("【SN46 项目方】", event["details"]["display_tags"])
        self.assertEqual(event["details"]["project_owner_tags"], ["SN46项目方"])

    def test_project_owner_token_transfer_gets_source_suffix(self) -> None:
        coldkey = "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        raw = {
            "raw_id": "project-owner-token-transfer",
            "event_index": 2,
            "module": "SubtensorModule",
            "event_name": "StakeTransferred",
            "raw": {
                "attributes": [
                    coldkey,
                    "5DestinationABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "5HotKeyABCDEFGHIJKLMNOPQRSTUVWXYZ5678",
                    46,
                    78,
                    1_000_000_000,
                ],
            },
        }
        event = address_event_record(
            raw,
            network="finney",
            watched_addresses=[{
                "address": coldkey,
                "label": "SN46 项目方",
                "library_book": "SN46项目方相关",
            }],
            block_hash="0x" + "d" * 64,
            height=103,
            spec_version=491,
            event_at="2026-11-18T00:00:00.000Z",
        )
        self.assertEqual(event["summary"], "SN46→SN78 · 1 TAO 等值 · 5Col…1234")
        self.assertIn("【SN46 项目方】", event["details"]["display_tags"])
        self.assertEqual(event["details"]["project_owner_tags"], ["SN46项目方"])

    def test_non_project_owner_keeps_original_subnet_prefix_only(self) -> None:
        coldkey = "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        raw = {
            "raw_id": "normal-wallet-stake",
            "event_index": 1,
            "module": "SubtensorModule",
            "event_name": "StakeAdded",
            "raw": {
                "attributes": [coldkey, "5HotKeyABCDEFGHIJKLMNOPQRSTUVWXYZ5678",
                               1_000_000_000, 2_000_000_000, 78, 0],
            },
        }
        event = address_event_record(
            raw,
            network="finney",
            watched_addresses=[{
                "address": coldkey,
                "label": "普通观察地址",
                "library_book": "普通地址库",
            }],
            block_hash="0x" + "b" * 64,
            height=101,
            spec_version=491,
            event_at="2026-11-18T00:00:00.000Z",
        )
        self.assertTrue(event["summary"].startswith("SN78 · "))
        self.assertNotIn("项目方）", event["summary"])
        self.assertEqual(event["details"]["project_owner_tags"], [])

    def test_project_owner_coldkey_swap_chain_event_gets_source_suffix(self) -> None:
        root = Path(__file__).resolve().parent.parent
        rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
        collector = ChainCollector(
            url="unused", network="finney", store=object(), rules=rules,
            publish=lambda *args: None, status=lambda *args: None,
        )
        coldkey = "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        raw_event = ScaleValue({
            "event_index": 9,
            "module_id": "SubtensorModule",
            "event_id": "ColdkeySwapped",
            "attributes": [coldkey, "5NewColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ"],
            "phase": "ApplyExtrinsic",
        })
        raws, mapped = collector._map_block_events(
            [raw_event],
            watched_addresses=[{
                "address": coldkey,
                "label": "SN46 项目方",
                "library_book": "SN46项目方相关",
            }],
            disabled_netuids=set(),
            block_hash="0x" + "c" * 64,
            height=102,
            spec_version=491,
            event_at="2026-11-18T00:00:00.000Z",
        )
        swap = next(item for item in mapped if item["event_name"].endswith("ColdkeySwapped"))
        self.assertTrue(swap["summary"].endswith("【SN46项目方】"))
        self.assertEqual(swap["details"]["project_owner_tags"], ["SN46项目方"])

    def test_address_monitor_masks_addresses_and_builds_concise_events(self) -> None:
        watched = [{
            "address": "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "label": "团队钱包",
        }]
        raw = {
            "raw_id": "finney:0xblock:1",
            "event_index": 1,
            "module": "SubtensorModule",
            "event_name": "StakeAdded",
            "raw": {
                "attributes": {
                    "coldkey": "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
                    "hotkey": "5HotKeyABCDEFGHIJKLMNOPQRSTUVWXYZ5678",
                    "tao_amount": 1_250_000_000,
                    "alpha_amount": 2_500_000_000,
                    "netuid": 0,
                },
            },
        }
        event = address_event_record(
            raw,
            network="finney",
            watched_addresses=watched,
            block_hash="0x" + "a" * 64,
            height=100,
            spec_version=488,
            event_at="2026-11-11T00:00:00.000Z",
        )
        self.assertEqual(event["source"], "address")
        self.assertEqual(event["event_type"], "零号子网质押")
        self.assertEqual(
            event["summary"],
            f"SN0 · 买入 1.25 TAO · {mask_address(watched[0]['address'])}",
        )
        self.assertIn(mask_address(watched[0]["address"]), event["summary"])
        self.assertNotIn("5HotKey", event["summary"])

    def test_address_mask_uses_first_and_last_four_characters(self) -> None:
        self.assertEqual(mask_address("1234567890abcdef"), "1234…cdef")

    def test_address_transfer_threshold_filters_only_small_transfers(self) -> None:
        watched = [{
            "address": "5ColdKeyABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "label": "团队钱包",
            "min_transfer_tao": 1,
        }]

        def transfer(amount_rao: int) -> dict:
            return {
                "raw_id": f"finney:0xblock:{amount_rao}",
                "event_index": 1,
                "module": "Balances",
                "event_name": "Transfer",
                "raw": {"attributes": {
                    "from": watched[0]["address"],
                    "to": "5OtherKeyABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "amount": amount_rao,
                }},
            }

        common = {
            "network": "finney",
            "watched_addresses": watched,
            "block_hash": "0x" + "a" * 64,
            "height": 100,
            "spec_version": 490,
            "event_at": "2026-11-11T00:00:00.000Z",
        }
        self.assertIsNone(address_event_record(transfer(500_000_000), **common))
        event = address_event_record(transfer(1_000_000_000), **common)
        self.assertIsNotNone(event)
        self.assertTrue(event["summary"].startswith("全链 · 1 TAO · "))
        self.assertNotIn("SN不适用", event["summary"])
        self.assertIn("1 TAO", event["summary"])

        stake = {
            "raw_id": "finney:0xblock:stake",
            "event_index": 2,
            "module": "SubtensorModule",
            "event_name": "StakeAdded",
            "raw": {"attributes": {
                "coldkey": watched[0]["address"],
                "hotkey": "5HotKeyABCDEFGHIJKLMNOPQRSTUVWXYZ5678",
                "tao_amount": 500_000_000,
                "alpha_amount": 500_000_000,
                "netuid": 0,
            }},
        }
        self.assertIsNotNone(address_event_record(stake, **common))

    def test_coldkey_swap_is_decoded_and_linked_to_subnet(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(__file__).resolve().parent.parent
            store = Store(Path(folder) / "events.db")
            rules, _ = load_rules(root / "app" / "event_monitor" / "config" / "event_rules.json")
            collector = ChainCollector(
                url="unused", network="finney", store=store, rules=rules,
                publish=lambda *args: None, status=lambda *args: None,
            )
            raw = {
                "raw_id": "finney:0xblock:coldkey",
                "event_index": 2,
                "module": "SubtensorModule",
                "event_name": "ColdkeySwapped",
                "raw": {
                    "height": 100,
                    "block_hash": "0x" + "a" * 64,
                    "module": "SubtensorModule",
                    "event_name": "ColdkeySwapped",
                    "attributes": ["old-address", "new-address"],
                },
            }
            event = collector._event_record(
                raw, "0x" + "a" * 64, 100, 488, "2026-11-11T00:00:00.000Z"
            )
            self.assertEqual(event["event_type"], "冷键交换")
            self.assertEqual(event["details"]["fields"]["new_coldkey"], "new-address")
            self.assertEqual(event["enrichment_status"], "pending")

            class Value:
                def __init__(self, value):
                    self.value = value
                    self.ss58_address = str(value)

            class FakeSubstrate:
                def query_map(self, **kwargs):
                    return [[Value(78), Value("new-address")], [Value(99), Value("other")]]

            enriched = collector.enrich_coldkey_swap(
                event, event["block_hash"], FakeSubstrate()
            )
            self.assertEqual(enriched["netuid"], 78)
            self.assertIn("SN78 子网冷键交换", enriched["summary"])
            self.assertEqual(enriched["details"]["affected_subnets"], [78])


class FakeGitHubClient:
    token = "test-token"

    def __init__(self) -> None:
        self.commit_calls = 0
        self.release_calls = 0
        self.old = {
            "sha": "old-sha",
            "html_url": "https://github.com/acme/subnet/commit/old-sha",
            "commit": {
                "author": {"date": "2026-11-11T00:00:00Z", "name": "Dev"},
                "message": "old commit",
            },
        }
        self.new = {
            "sha": "new-sha",
            "html_url": "https://github.com/acme/subnet/commit/new-sha",
            "commit": {
                "author": {"date": "2026-11-11T01:00:00Z", "name": "Dev"},
                "message": "ship update",
            },
        }

    def get(self, path: str):
        if path == "/repos/acme/subnet":
            return {"default_branch": "main"}
        if path.startswith("/repos/acme/subnet/commits?"):
            self.commit_calls += 1
            return [self.old] if self.commit_calls == 1 else [self.new, self.old]
        if path == "/repos/acme/subnet/commits/new-sha":
            return {
                "stats": {"additions": 42, "deletions": 8, "total": 50},
                "files": [
                    {"filename": "README.md"},
                    {"filename": "docs/update.md"},
                ],
            }
        if path.startswith("/repos/acme/subnet/releases?"):
            self.release_calls += 1
            if self.release_calls == 1:
                return []
            return [{
                "id": 9001,
                "tag_name": "v1.0.0",
                "name": "v1.0.0",
                "html_url": "https://github.com/acme/subnet/releases/tag/v1.0.0",
                "published_at": "2026-11-11T02:00:00Z",
                "draft": False,
                "prerelease": False,
            }]
        raise AssertionError(path)


class GitHubTests(unittest.TestCase):
    def test_parse_repository(self) -> None:
        self.assertEqual(parse_github_repository("https://github.com/acme/subnet.git"),
                         ("acme", "subnet"))
        self.assertEqual(parse_github_repository("acme/subnet"), ("acme", "subnet"))
        self.assertIsNone(parse_github_repository("https://example.com/acme/subnet"))
        self.assertIsNone(parse_github_repository("https://github.com/orgs/Acme"))

    def test_baseline_then_new_commit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "events.db")
            repo = store.add_repository({
                "network": "finney", "netuid": 55, "owner": "acme", "name": "subnet",
                "html_url": "https://github.com/acme/subnet",
                "relation_source": "manual_review", "reviewed_by": "tester",
            })
            client = FakeGitHubClient()
            published = []
            collector = GitHubCollector(
                store=store, publish=lambda event, kind: published.append((event, kind)),
                status=lambda *args: None, client=client,
            )
            self.assertEqual(collector.poll_repository(repo), [])
            repo = next(item for item in store.list_repositories() if item["owner"] == "acme")
            events = collector.poll_repository(repo)
            self.assertEqual(len(events), 2)
            commit_event = next(
                item for item in events
                if item["event_name"] == "GitHub.DefaultBranchCommit"
            )
            self.assertIn("GitHub 文档更新", commit_event["summary"])
            self.assertNotIn("+42/-8", commit_event["summary"])
            self.assertIn("评级：中", commit_event["summary"])
            self.assertEqual(commit_event["details"]["files_changed"], 2)
            self.assertEqual(commit_event["details"]["update_rating"], "中")
            self.assertTrue(any("v1.0.0" in item["summary"] for item in events))
            self.assertEqual(len(published), 2)

    def test_commit_summary_describes_technical_change_not_line_count(self) -> None:
        brief = _commit_change_brief(
            "fix(auction): the audit backlog leaves out records waiting for their draw (#491)",
            [
                "reliquary/validator/corpus_auditor.py",
                "tests/unit/test_corpus_stake_rule.py",
            ],
            "问题修复",
        )
        self.assertEqual(
            brief,
            "修复：审计积压统计、等待抽签的记录",
        )
        points = _commit_progress_points(
            "fix(auction): the audit backlog leaves out records waiting for their draw",
            [
                {
                    "filename": "reliquary/validator/corpus_auditor.py",
                    "patch": "+def should_count_sample():\n+    return False",
                },
                {
                    "filename": "tests/unit/test_corpus_stake_rule.py",
                    "patch": "+def test_waiting_record_is_excluded():",
                },
            ],
            "问题修复",
        )
        self.assertIn("本次重点：审计积压统计、等待抽签的记录。", points)
        self.assertTrue(any("代码改动" in point for point in points))
        self.assertTrue(any("should_count_sample" in point for point in points))
        self.assertTrue(any("测试变化" in point for point in points))

    def test_github_token_update_changes_runtime_mode(self) -> None:
        class TokenClient:
            token = None

        collector = GitHubCollector(
            store=object(), publish=lambda *args: None,
            status=lambda *args: None, client=TokenClient(),
        )
        collector.update_token("test-token-value")
        self.assertEqual(collector.client.token, "test-token-value")
        self.assertEqual(collector.max_repositories_per_poll, 30)
        collector.update_token("")
        self.assertIsNone(collector.client.token)
        self.assertEqual(collector.max_repositories_per_poll, 1)

    def test_github_client_captures_rate_limit_headers(self) -> None:
        client = GitHubClient(token="test")
        client._capture_rate_limit({
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4210",
            "X-RateLimit-Used": "790",
            "X-RateLimit-Reset": "1793512800",
            "X-RateLimit-Resource": "core",
        })
        self.assertEqual(client.rate_limit["limit"], 5000)
        self.assertEqual(client.rate_limit["remaining"], 4210)
        self.assertEqual(client.rate_limit["used"], 790)
        self.assertEqual(client.rate_limit["resource"], "core")
        self.assertTrue(client.rate_limit["reset_at"].endswith("Z"))

    def test_partial_failures_do_not_mark_entire_github_worker_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "events.db")
            for netuid in (1, 2):
                store.add_repository({
                    "network": "finney", "netuid": netuid,
                    "owner": "acme", "name": f"subnet-{netuid}",
                    "html_url": f"https://github.com/acme/subnet-{netuid}",
                    "relation_source": "chain_identity", "reviewed_by": "chain",
                })
            collector = GitHubCollector(
                store=store, publish=lambda *args: None,
                status=lambda *args: None,
                client=FakeGitHubClient(), max_repositories_per_poll=2, max_workers=2,
            )

            def poll(repo):
                if repo["netuid"] == 2:
                    raise GitHubError("GitHub API 暂时不可用：连接超时，已自动重试")
                return []

            with patch.object(collector, "poll_repository", side_effect=poll):
                result = collector.poll_once()
            self.assertEqual(result["state"], "partial")
            self.assertEqual(result["successes"], 1)
            self.assertEqual(result["failures"], 1)

    def test_github_backlog_resets_cursor_to_latest_commit(self) -> None:
        class LatestClient:
            token = "test-token"

            def get(self, path: str):
                self.assert_path = path
                return [{"sha": "latest-sha"}]

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "events.db")
            repo = store.add_repository({
                "network": "finney", "netuid": 3, "owner": "acme", "name": "large",
                "html_url": "https://github.com/acme/large",
                "relation_source": "chain_identity", "reviewed_by": "chain",
            })
            store.set_repo_baseline(repo["repository_id"], "main", "old-sha", "release-1")
            repo = next(item for item in store.list_repositories() if item["name"] == "large")
            collector = GitHubCollector(
                store=store, publish=lambda *args: None,
                status=lambda *args: None, client=LatestClient(),
            )
            with patch.object(
                collector, "_commits", side_effect=GitHubBacklogError("backlog")
            ):
                self.assertEqual(collector.poll_repository(repo), [])
            refreshed = next(item for item in store.list_repositories() if item["name"] == "large")
            self.assertEqual(refreshed["commit_cursor"], "latest-sha")
            self.assertEqual(refreshed["release_cursor"], "release-1")

    def test_rate_limit_is_reported_as_limited_not_degraded(self) -> None:
        class LimitedClient:
            token = None

            def get(self, path: str):
                raise GitHubError("GitHub API rate limit reached")

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "events.db")
            store.add_repository({
                "network": "finney", "netuid": 99, "owner": "acme", "name": "subnet",
                "html_url": "https://github.com/acme/subnet",
                "relation_source": "chain_identity", "reviewed_by": "chain",
            })
            statuses = []
            collector = GitHubCollector(
                store=store, publish=lambda *args: None,
                status=lambda component, state, details: statuses.append(
                    (component, state, details)
                ),
                client=LimitedClient(),
            )
            result = collector.poll_once()
            self.assertEqual(result["state"], "limited")
            self.assertTrue(result["rate_limited"])
            self.assertEqual(result["failures"], 0)
            self.assertEqual(result["rate_limited_repository"], "acme/subnet")
            self.assertEqual(statuses[-1][1], "limited")


class ServerTests(unittest.TestCase):
    def test_static_and_json_routes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(__file__).resolve().parent.parent
            store = Store(Path(folder) / "events.db")
            store.create_event(make_event())
            store.save_runtime_metadata(
                "finney", 488, "0x" + "a" * 64, "metadata-hash", "0x1234"
            )
            store.add_log("INFO", "app.server", "server ready")
            store.set_trading_state(
                network="finney", netuid=99, enabled=True, registration_block=9447220,
                block_hash="0x" + "b" * 64, block_number=9477119,
            )
            store.upsert_subnet_profile_auto(
                "finney", 99,
                {"name": "Chain Name", "website_url": "https://example.org"},
                "0x" + "b" * 64,
            )
            store.upsert_subnet_profile_auto(
                "finney", 100, {"name": "Archived Name", "github_url": "https://github.com/old/archive"},
                "0x" + "c" * 64,
            )
            store.archive_subnet_profile(
                "finney", 100, "network_removed", archive_key="test:removed",
            )
            context = AppContext(
                store=store, hub=EventHub(), status=WorkerStatus(),
                static_dir=root / "app" / "event_monitor" / "static", product_info={"product": {"version": "0.2.0"}},
                simulation_signals=load_simulation_signals(
                    root / "app" / "event_monitor" / "config" / "event_rules.json"
                ),
            )
            context.request_profile_sync = lambda block_hash: True
            token_updates = []
            context.github_token_update = token_updates.append
            context.env_path = Path(folder) / ".env"
            server = MonitorHTTPServer(("127.0.0.1", 0), context)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events", timeout=5) as response:
                    payload = json.loads(response.read())
                self.assertEqual(len(payload["events"]), 1)
                self.assertEqual(payload["total"], 1)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/events?limit=12&offset=0&netuid=55&q=%E8%BA%AB%E4%BB%BD",
                    timeout=5,
                ) as response:
                    filtered = json.loads(response.read())
                self.assertEqual(filtered["total"], 1)
                self.assertEqual(filtered["events"][0]["netuid"], 55)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    html = response.read().decode()
                self.assertIn("Wallet Link 事件监控", html)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/floating", timeout=5
                ) as response:
                    floating = response.read().decode()
                self.assertIn("Wallet Link 事件监控", floating)
                address_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/addresses",
                    data=json.dumps({
                        "address": "5HCFWvRqzSHWRPecN7q8J6c7aKQnrCZTMHstPv39xL1wgDHh",
                        "label": "Browser Test",
                        "library_book": "SN46项目方相关",
                        "min_transfer_tao": "0.5",
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(address_request, timeout=5) as response:
                    watched = json.loads(response.read())["address"]
                self.assertEqual(watched["masked"], "5HCF…gDHh")
                self.assertEqual(watched["min_transfer_tao"], 0.5)
                update_address = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/addresses/{watched['address_id']}",
                    data=json.dumps({"min_transfer_tao": "2.25"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with urllib.request.urlopen(update_address, timeout=5) as response:
                    updated_watched = json.loads(response.read())["address"]
                self.assertEqual(updated_watched["min_transfer_tao"], 2.25)
                batch_address_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/addresses",
                    data=json.dumps({
                        "addresses": [
                            "5HCFWvRqzSHWRPecN7q8J6c7aKQnrCZTMHstPv39xL1wgDHh",
                            "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
                        ],
                        "label": "Batch Test",
                        "min_transfer_tao": "1.5",
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(batch_address_request, timeout=5) as response:
                    batch_result = json.loads(response.read())
                self.assertEqual(batch_result["created"], 1)
                self.assertEqual(batch_result["skipped"], 1)
                self.assertEqual(batch_result["addresses"][0]["min_transfer_tao"], 1.5)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/addresses", timeout=5
                ) as response:
                    address_list = json.loads(response.read())
                self.assertEqual(len(address_list["addresses"]), 2)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/simulation/signals", timeout=5
                ) as response:
                    signals = json.loads(response.read())
                self.assertTrue(signals["enabled"])
                self.assertTrue(any(
                    signal["id"] == "coldkey_swapped"
                    for signal in signals["signals"]
                ))
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/simulation/events",
                    data=json.dumps({
                        "netuid": 78,
                        "signal_id": "coldkey_swapped",
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    simulated = json.loads(response.read())
                self.assertEqual(simulated["event"]["event_type"], "冷键交换")
                self.assertIn("SN78 子网冷键交换", simulated["event"]["summary"])
                delete_simulation = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/simulation/events",
                    method="DELETE",
                )
                with urllib.request.urlopen(delete_simulation, timeout=5) as response:
                    deleted = json.loads(response.read())
                self.assertEqual(deleted, {"status": "deleted", "deleted": 1})
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/events?source=simulation&limit=1",
                    timeout=5,
                ) as response:
                    remaining_simulations = json.loads(response.read())
                self.assertEqual(remaining_simulations["total"], 0)
                address_simulation = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/simulation/events",
                    data=json.dumps({
                        "netuid": 78,
                        "signal_id": "address_stake_removed",
                        "address_id": watched["address_id"],
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(address_simulation, timeout=5) as response:
                    simulated_address = json.loads(response.read())["event"]
                self.assertEqual(simulated_address["source"], "address")
                self.assertEqual(simulated_address["confidence"], "simulated")
                self.assertEqual(simulated_address["netuid"], 78)
                self.assertEqual(simulated_address["event_type"], "解质押")
                self.assertEqual(
                    simulated_address["summary"],
                    f"SN78 · 卖出 1.00 TAO · {watched['masked']}",
                )
                self.assertEqual(
                    simulated_address["details"]["project_owner_tags"],
                    ["SN46项目方"],
                )
                self.assertIn(watched["masked"], simulated_address["summary"])
                delete_address_simulation = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/simulation/events",
                    method="DELETE",
                )
                with urllib.request.urlopen(delete_address_simulation, timeout=5) as response:
                    deleted_address = json.loads(response.read())
                self.assertEqual(deleted_address["deleted"], 1)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/events?source=address&limit=1",
                    timeout=5,
                ) as response:
                    remaining_address = json.loads(response.read())
                self.assertEqual(remaining_address["total"], 0)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/runtime-metadata", timeout=5
                ) as response:
                    metadata = json.loads(response.read())
                self.assertEqual(metadata["versions"][0]["spec_version"], 488)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/logs", timeout=5
                ) as response:
                    logs = json.loads(response.read())
                self.assertEqual(logs["logs"][0]["message"], "server ready")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/trading", timeout=5
                ) as response:
                    trading = json.loads(response.read())
                self.assertTrue(trading["subnets"][0]["enabled"])
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnet-profiles", timeout=5
                ) as response:
                    profiles = json.loads(response.read())
                self.assertEqual(profiles["profiles"][0]["name"], "Chain Name")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnet-profiles?include_archived=1",
                    timeout=5,
                ) as response:
                    profiles_with_archive = json.loads(response.read())
                self.assertEqual(
                    profiles_with_archive["archived_profiles"][0]["name"],
                    "Archived Name",
                )
                sync_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/subnet-profiles/sync",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(sync_request, timeout=5) as response:
                    sync_result = json.loads(response.read())
                self.assertTrue(sync_result["started"])
                self.assertEqual(sync_result["status"], "accepted")
                with patch.dict("os.environ", {"GITHUB_TOKEN": ""}, clear=False):
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/settings/github", timeout=5
                    ) as response:
                        github_anonymous = json.loads(response.read())
                    self.assertFalse(github_anonymous["configured"])
                    token_request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/settings/github",
                        data=json.dumps({"token": "test-token-value"}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="PUT",
                    )
                    with urllib.request.urlopen(token_request, timeout=5) as response:
                        github_authenticated = json.loads(response.read())
                    self.assertTrue(github_authenticated["configured"])
                    self.assertEqual(github_authenticated["github_batch_size"], 30)
                    self.assertEqual(token_updates[-1], "test-token-value")
                    self.assertIn(
                        "GITHUB_TOKEN=test-token-value",
                        context.env_path.read_text(encoding="utf-8"),
                    )
                    clear_request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/settings/github",
                        data=json.dumps({"token": ""}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="PUT",
                    )
                    with urllib.request.urlopen(clear_request, timeout=5) as response:
                        github_cleared = json.loads(response.read())
                    self.assertFalse(github_cleared["configured"])
                    self.assertEqual(github_cleared["github_batch_size"], 1)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnet-profiles/export?format=json",
                    timeout=5,
                ) as response:
                    exported = json.loads(response.read())
                self.assertEqual(exported["profiles"][0]["netuid"], 99)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnet-event-records", timeout=5
                ) as response:
                    record_index = json.loads(response.read())
                self.assertEqual(record_index["subnet_count"], 2)
                record_55 = next(item for item in record_index["records"] if item["netuid"] == 55)
                record_99 = next(item for item in record_index["records"] if item["netuid"] == 99)
                self.assertEqual(record_55["event_count"], 1)
                self.assertEqual(record_99["event_count"], 0)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnets/55/export?format=md", timeout=5
                ) as response:
                    markdown = response.read().decode("utf-8")
                self.assertIn("# SN55 事件更新记录", markdown)
                self.assertIn("身份信息正在核验", markdown)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnets/55/export?format=csv", timeout=5
                ) as response:
                    csv_text = response.read().decode("utf-8-sig")
                self.assertIn("event_time", csv_text)
                self.assertIn("身份信息正在核验", csv_text)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnets/55/export?format=html", timeout=5
                ) as response:
                    html_export = response.read().decode("utf-8")
                self.assertIn("SN55 事件更新记录", html_export)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/subnet-event-records/export?format=json",
                    timeout=5,
                ) as response:
                    archive_bytes = response.read()
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    names = set(archive.namelist())
                    self.assertIn("SN55.json", names)
                    self.assertIn("SN99.json", names)
                    self.assertIn("README.txt", names)
                    exported_subnet = json.loads(archive.read("SN55.json"))
                self.assertEqual(exported_subnet["event_count"], 1)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
