"""选择地址库「保存设置」：全选/一键清除真正落库并保持清空（1.0.30）。

用户确认口径：
- 「一键清除」清空勾选后必须点「保存设置」才移出全部监控地址；
- 清空后监控名单锁定，项目方生命周期恢复 / 默认 seed / 手动新增都不得把地址加回；
- 事件流不再显示地址类转账、交易（历史也不显示，记录本身不删）；
- 全选后一级勾选框要显示已勾选，保存后解锁并按勾选重建名单。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.main as main_module
from app.event_monitor.default_watch import seed_default_subnet_watch
from app.event_monitor.storage import Store

ADDRESS = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
ADDRESS_TWO = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"


def _event(event_id: str, source: str) -> dict:
    return {
        "event_id": event_id,
        "network": "finney",
        "netuid": 55,
        "event_type": "身份信息变更",
        "event_name": "SubtensorModule.SubnetIdentitySet",
        "source": source,
        "source_url": None,
        "event_at": "2026-11-26T00:00:00.000Z",
        "summary": "测试事件",
        "details": {},
        "raw_data": {},
        "block_number": 10,
        "block_hash": "0x" + "0" * 64,
        "event_index": 1,
        "runtime_spec_version": 488,
        "chain_status": "observed",
        "enrichment_status": "pending",
        "confidence": "high",
    }


class TestReplaceWatchedAddresses:
    def test_empty_replacement_clears_list_and_locks(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "events.db")
        store.add_watched_address("finney", ADDRESS, "A", library_book="SN1 A")
        store.add_watched_address("finney", ADDRESS_TWO, "B", library_book="SN1 B")

        result = store.replace_watched_addresses("finney", [], lock=True)

        assert result == {"network": "finney", "created": 0, "removed": 2, "locked": True}
        assert store.list_watched_addresses("finney") == []
        assert store.address_list_locked() is True
        with store.connect() as db:
            excluded = {
                row["address"]
                for row in db.execute(
                    "SELECT address FROM watched_address_exclusions WHERE network='finney'"
                ).fetchall()
            }
        assert excluded == {ADDRESS, ADDRESS_TWO}, "清空时每个地址都要写排除表"

    def test_lock_blocks_every_readd_path_even_with_clear_exclusion(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "events.db")
        store.add_watched_address("finney", ADDRESS, "A", library_book="SN1 A")
        store.replace_watched_addresses("finney", [], lock=True)

        with pytest.raises(ValueError, match="清空状态"):
            store.add_watched_address("finney", ADDRESS, "A", clear_exclusion=True)
        assert store.list_watched_addresses("finney") == [], "锁定后 clear_exclusion 也不能加回"

        store.set_address_list_lock(False)
        store.add_watched_address("finney", ADDRESS, "A", clear_exclusion=True)
        assert len(store.list_watched_addresses("finney")) == 1, "解锁后保存设置应能恢复监控"

    def test_full_replacement_unlocks_and_rebuilds(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "events.db")
        store.add_watched_address("finney", ADDRESS, "A", library_book="SN1 A")
        store.replace_watched_addresses("finney", [], lock=True)

        result = store.replace_watched_addresses(
            "finney",
            [{"address": ADDRESS_TWO, "label": "B", "library_book": "SN1 B", "address_role": "project_owner"}],
            lock=False,
        )

        assert result["created"] == 1
        assert result["removed"] == 0
        assert result["locked"] is False
        rows = store.list_watched_addresses("finney")
        assert [row["address"] for row in rows] == [ADDRESS_TWO]
        assert rows[0]["address_role"] == "project_owner"
        assert store.address_list_locked() is False
        with store.connect() as db:
            excluded = {
                row["address"]
                for row in db.execute(
                    "SELECT address FROM watched_address_exclusions WHERE network='finney'"
                ).fetchall()
            }
        assert ADDRESS in excluded and ADDRESS_TWO not in excluded


class TestHistoricalAddressEvents:
    def test_lock_hides_address_events_without_deleting_them(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "events.db")
        store.create_event(_event("a" * 64, "address"))
        store.create_event(_event("b" * 64, "chain"))
        assert store.count_events({}) == 2

        store.set_address_list_lock(True)
        assert store.count_events({}) == 1
        assert [event["source"] for event in store.list_events({})] == ["chain"]

        store.set_address_list_lock(False)
        assert store.count_events({}) == 2, "解锁后历史地址事件仍应可见，记录未被删除"


class TestReaddPathsRespectLock:
    def test_subnet_owner_restore_skipped_when_locked(self, monkeypatch) -> None:
        class LockedStore:
            def address_list_locked(self) -> bool:
                return True

        context = type("C", (), {"store": LockedStore()})()
        monitor = type("M", (), {"running": True, "context": context})()
        monkeypatch.setattr(main_module, "EVENT_MONITOR", monitor)

        assert main_module._ensure_subnet_owner_watched(1) == 0

    def test_default_seed_short_circuits_when_locked(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "events.db")
        store.set_address_list_lock(True)
        result = seed_default_subnet_watch(
            store, [{"book": "SN1 Apex", "address": ADDRESS, "source": "bittensor:SubtensorModule.SubnetOwner"}]
        )
        assert result["seeded"] is False
        assert result["reason"] == "address_list_locked"
        assert store.list_watched_addresses("finney") == []
