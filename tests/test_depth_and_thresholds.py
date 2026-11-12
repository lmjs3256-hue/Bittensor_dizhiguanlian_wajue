# -*- coding: utf-8 -*-
"""层级上限放到 10 级 + 阈值本地保存。

两条需求原文：
1. 「地址关联层级提高到 10 级，默认层级仍然为 3 级……层级提升所有规则都和
   前面的一样。」——层级只决定 BFS 向下走多少跳；单边门槛、累计门槛、
   交易所/公共地址截止、BFS 最短路径这些规则在每一级都一样。
2. 「单边最低和累计最低 TAO 增加本地保存按钮，让挖掘层级数较高时我会把
   单边最低 TAO 提升。」——阈值要能落盘，下次打开页面自动填回。
"""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import settings_store
from app.db import Database
from app.jobs import RelationJobManager, address_cap_for_depth
from app.relation import discover
from app.relation_config import (
    DEFAULT_RELATION_DEPTH,
    MAX_RELATION_DEPTH,
    LinkPolicy,
    TransferRecord,
)
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """阈值写到临时 settings.json，绝不碰用户真实的 data/settings.json。"""

    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "settings.json"))
    yield


def chain_records(hops: int):
    """造一条 ``hops`` 跳的转账链，每跳 10 TAO。"""

    records = []
    for index in range(hops):
        records.append(
            TransferRecord(
                f"tx-{index}",
                100 + index,
                f"hop-{index}",
                f"hop-{index + 1}",
                Decimal("10"),
            )
        )
    return records


# -- 1. 层级上限 -----------------------------------------------------------
def test_depth_constants_are_10_and_3():
    assert MAX_RELATION_DEPTH == 10
    assert DEFAULT_RELATION_DEPTH == 3


@pytest.mark.parametrize("depth", [4, 5, 6, 7, 8, 9, 10])
def test_discover_walks_deeper_than_three(depth):
    """4～10 级都能走通，且每一级只多出「那一层」的地址。"""

    records = chain_records(depth)
    report = discover(
        "hop-0", records, max_depth=depth, policy=LinkPolicy(Decimal("1"), Decimal("1"))
    )

    deepest = max(row.depth for row in report.related)
    assert deepest == depth
    # 一条链就是每层一个地址，总数应等于跳数
    assert len(report.related) == depth
    assert report.max_depth == depth
    # 最深那个地址的路径明细必须完整：depth 跳就 depth 笔
    deepest_row = next(row for row in report.related if row.depth == depth)
    assert len(deepest_row.path_transfers) == depth
    # 路径必须从起始地址起步、到该地址收尾（深层也不能错位）
    assert deepest_row.path_transfers[0]["path_from"] == "hop-0"
    assert deepest_row.path_transfers[-1]["path_to"] == f"hop-{depth}"


def test_discover_rejects_depth_above_ten():
    with pytest.raises(ValueError):
        discover("hop-0", chain_records(3), max_depth=MAX_RELATION_DEPTH + 1)


def test_deeper_levels_use_the_same_threshold_rules():
    """层级提到 6 级后，规则完全一样：小额透明边被过滤、累计不达标不出结果。"""

    records = chain_records(6)
    # 第 4 跳插一笔 0.01 TAO 的小额转账，低于单边门槛，不应成为保留边
    records.append(
        TransferRecord("tiny", 300, "hop-3", "tiny-leaf", Decimal("0.01"))
    )
    report = discover(
        "hop-0",
        records,
        max_depth=6,
        policy=LinkPolicy(Decimal("1"), Decimal("1")),
    )

    addresses = {row.address for row in report.related}
    assert "tiny-leaf" not in addresses, "深层的小额边同样要被单边门槛过滤"
    assert max(row.depth for row in report.related) == 6


def test_deeper_levels_stop_at_terminal_addresses():
    """深层同样在交易所/系统地址处截止，不会顺着公共地址继续扩散。"""

    from app.relation_config import LibraryEntry

    records = chain_records(6)
    records.append(
        TransferRecord("beyond", 400, "hop-3", "behind-exchange", Decimal("50"))
    )
    library = [
        LibraryEntry(
            address="hop-3",
            label="某交易所",
            category="EXCHANGE",
            categories=("EXCHANGE",),
            exchange_confirmed=True,
        )
    ]
    report = discover(
        "hop-0",
        records,
        library=library,
        max_depth=6,
        policy=LinkPolicy(Decimal("1"), Decimal("1")),
    )

    addresses = {row.address for row in report.related}
    assert "hop-3" in addresses, "交易所本身仍是关联地址"
    assert "behind-exchange" not in addresses, "不应该穿过交易所继续向下扩展"


def test_api_accepts_depth_ten_and_rejects_eleven(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "depth10.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                # 非法 SS58 会在进入扫描前被挡下（422），
                # 这里只关心层级校验本身不拦 10 级。
                ok = client.post(
                    "/api/relations",
                    json={
                        "seed": SEED,
                        "depth": 10,
                        "min_edge_tao": "1",
                        "min_total_tao": "1",
                        "data_source": "local",
                    },
                )
                assert ok.status_code == 200
                assert ok.json()["max_depth"] == 10

                too_deep = client.post(
                    "/api/relations",
                    json={"seed": SEED, "depth": 11, "data_source": "local"},
                )
                assert too_deep.status_code == 422
        finally:
            database.engine.dispose()


def test_job_input_defaults_to_three_and_accepts_ten():
    assert main_module.RelationJobInput(seed=SEED).depth == 3
    assert main_module.RelationJobInput(seed=SEED, depth=10).depth == 10
    with pytest.raises(Exception):
        main_module.RelationJobInput(seed=SEED, depth=11)


def test_address_cap_grows_with_depth():
    """层级放到 4 级以上时，地址护栏也要放宽，否则第 4 层没走完就被截停。"""

    assert address_cap_for_depth(3) == 150
    assert address_cap_for_depth(10) == 500
    # 单调不减
    caps = [address_cap_for_depth(depth) for depth in range(1, 11)]
    assert caps == sorted(caps)


def test_job_archives_depth_and_address_cap(monkeypatch):
    """归档结果里要带 depth 和实际生效的地址护栏。

    事后翻「查询记录」时，只有看到 max_addresses 才能区分
    「被 150 个地址卡住」和「层级本来就只有 3 级」。
    """

    def fake_transfer(address, page):
        from app.taostats import TransferPage

        return TransferPage([], None)

    import app.service as service_module

    monkeypatch.setattr(service_module, "get_transfer_page", fake_transfer)
    monkeypatch.setattr(
        service_module,
        "get_stake_transfer_page",
        lambda *_a, **_k: __import__("app.taostats", fromlist=["TransferPage"]).TransferPage([], None),
    )

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "archive-cap.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        try:
            manager = RelationJobManager(round_interval=600, max_rounds=2)
            job = manager.create(
                service, seed=SEED, depth=8, min_edge_tao="1", min_total_tao="1"
            )
            payload = manager.payload(job)
            assert payload["max_addresses"] == address_cap_for_depth(8) == 400
            archived = (job.result or {}).get("job") or {}
            # 队列立刻清空时作业当场收尾，归档里就有这两个字段
            assert archived.get("depth") == 8
            assert archived.get("max_addresses") == 400
        finally:
            database.engine.dispose()


# -- 2. 阈值本地保存 -------------------------------------------------------
def test_thresholds_default_to_one():
    # 0.9.8 起返回值多一把「1 级关联最低 TAO」，出厂值 0 = 不限金额。
    assert settings_store.load_scan_thresholds() == {
        "min_edge_tao": "1",
        "min_total_tao": "1",
        "min_direct_tao": "0",
    }


def test_save_and_load_thresholds_roundtrip():
    saved = settings_store.save_scan_thresholds("10", "50")
    assert saved == {
        "min_edge_tao": "10",
        "min_total_tao": "50",
        "min_direct_tao": "0",
    }
    assert settings_store.load_scan_thresholds() == {
        "min_edge_tao": "10",
        "min_total_tao": "50",
        "min_direct_tao": "0",
    }


def test_thresholds_are_normalised():
    """10.00 写成 10、0.50 写成 0.5，避免回填时出现一堆尾零。"""

    settings_store.save_scan_thresholds(" 10.00 ", "0.50")
    values = settings_store.load_scan_thresholds()
    assert values["min_edge_tao"] == "10"
    assert values["min_total_tao"] == "0.5"


def test_invalid_threshold_keeps_previous_value():
    """误输入不该把已保存的设置冲掉（改成 1/1 更糟）。"""

    settings_store.save_scan_thresholds("10", "50")
    kept = settings_store.save_scan_thresholds("abc", "-3")
    assert kept == {
        "min_edge_tao": "10",
        "min_total_tao": "50",
        "min_direct_tao": "0",
    }
    assert settings_store.load_scan_thresholds() == kept


def test_thresholds_survive_broken_file(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(broken))

    assert settings_store.load_scan_thresholds() == {
        "min_edge_tao": "1",
        "min_total_tao": "1",
        "min_direct_tao": "0",
    }
    assert settings_store.save_scan_thresholds("7", "30") == {
        "min_edge_tao": "7",
        "min_total_tao": "30",
        "min_direct_tao": "0",
    }
    assert settings_store.load_scan_thresholds()["min_edge_tao"] == "7"


def test_thresholds_do_not_clobber_api_key():
    """阈值和 API key 在同一个 settings.json 里，互相不能覆盖。"""

    settings_store.save_api_key("key-abcdefghij")
    settings_store.save_scan_thresholds("12", "60")

    assert settings_store.load_api_key() == "key-abcdefghij"
    assert settings_store.load_scan_thresholds()["min_edge_tao"] == "12"


def test_threshold_endpoints(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "threshold.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                read = client.get("/api/settings/scan-thresholds")
                assert read.status_code == 200
                assert read.json() == {
                    "min_edge_tao": "1",
                    "min_total_tao": "1",
                    "min_direct_tao": "0",
                }

                written = client.post(
                    "/api/settings/scan-thresholds",
                    json={
                        "min_edge_tao": "20",
                        "min_total_tao": "100",
                        "min_direct_tao": "2",
                    },
                )
                assert written.status_code == 200
                assert written.json() == {
                    "min_edge_tao": "20",
                    "min_total_tao": "100",
                    "min_direct_tao": "2",
                }

                # 再读一次必须是刚存的值（真的落盘了）
                again = client.get("/api/settings/scan-thresholds")
                assert again.json()["min_total_tao"] == "100"
        finally:
            database.engine.dispose()


def test_overview_carries_depth_and_saved_thresholds(monkeypatch):
    """页面初始化只发一次 /api/overview，层级上限和阈值都随它带回。"""

    settings_store.save_scan_thresholds("15", "80")
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "overview.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        try:
            with TestClient(main_module.app) as client:
                payload = client.get("/api/overview").json()
                assert payload["max_depth"] == 10
                assert payload["default_depth"] == 3
                assert payload["scan_thresholds"] == {
                    "min_edge_tao": "15",
                    "min_total_tao": "80",
                    "min_direct_tao": "0",
                }
        finally:
            database.engine.dispose()


# -- 3. 前端静态资源 -------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_depth_selects_are_generated_from_backend_limits():
    """页面上不再写死 1/2/3，三处层级下拉都由 applyDepthOptions 生成。"""

    assert "function applyDepthOptions" in JS
    assert "applyDepthOptions(data.max_depth, data.default_depth)" in JS
    # 最大层级下拉本身是空的（选项由 JS 填），不能残留写死的 3 级 selected
    assert '<select id="depth" aria-label="最大关联层级"></select>' in HTML
    assert '<option value="3" selected>3 级</option>' not in HTML
    # 另外三处层级下拉要带 data-depth-options 标记
    assert HTML.count("data-depth-options") == 3


def test_threshold_save_button_exists_and_is_wired():
    assert 'id="save-thresholds"' in HTML
    assert "$(\"#save-thresholds\").addEventListener(\"click\", saveScanThresholds)" in JS
    assert "/api/settings/scan-thresholds" in JS
    # 输入变化要把按钮标成「待保存」，用户才知道当前值没落盘
    assert "function syncThresholdSaveState" in JS
