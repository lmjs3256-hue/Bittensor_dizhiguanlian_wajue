# -*- coding: utf-8 -*-
"""1 级关联金额门槛（min_direct_tao）与三把金额尺的真实生效性。

用户问题原文：「当前扫描地址关联逻辑的 1 级关联是不是不限定金额？那如果有人
恶意转账进去蹭关联怎么处理？增加个条件 1 级关联仍然需要提供可以设置转账金额
门槛的窗口。顺便排查单边最低 TAO 和累计最低 TAO 有没有真实应用到地址扫描的
筛选逻辑上。」

0.9.7 及以前 relation.discover() 的 ``depth == 0`` / ``edge.depth == 1`` 与
crawl.py 的 ``current_depth > 0`` 三处都显式豁免第一跳，所以「单边最低 TAO」
从来管不到 1 级关联。本文件给新门槛和旧规则的边界都上锁。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.service as service_module
from app import settings_store
from app.crawl import CrawlState, TaostatsCrawl
from app.db import Database
from app.jobs import RelationJobManager
from app.relation import discover
from app.relation_config import LinkPolicy, TransferRecord
from app.service import LinkService
from app.taostats import TransferPage


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """阈值写到临时 settings.json，绝不碰用户真实的 data/settings.json。"""

    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "settings.json"))
    yield


def make_policy(*, direct="0", edge="1", total="0") -> LinkPolicy:
    """累计门槛默认 0：测 1 级门槛时不想让累计门槛把结论混掉。"""

    return LinkPolicy(
        min_edge_tao=Decimal(edge),
        min_total_tao=Decimal(total),
        min_direct_tao=Decimal(direct),
    )


def addresses(report) -> set[str]:
    return {row.address for row in report.related}


# -- 1. 规则本身 -----------------------------------------------------------
def test_default_direct_floor_is_zero_and_keeps_legacy_behaviour():
    """出厂值 0 = 不限金额；0.9.7 的行为必须原样保留。"""

    assert LinkPolicy().min_direct_tao == Decimal("0")
    report = discover(
        "S",
        [TransferRecord("dust", 1, "DUST", "S", Decimal("0.001"))],
        max_depth=1,
        policy=make_policy(),
    )
    assert "DUST" in addresses(report), "填 0 时灰尘直连仍算 1 级关联（旧行为）"
    assert any(edge.amount_tao == Decimal("0.001") for edge in report.edges)


def test_edge_floor_uses_direct_rule_only_on_the_first_hop():
    """BFS 与爬取共用的口径：depth=0 用 1 级门槛，其余用单边门槛。"""

    policy = make_policy(direct="3", edge="7")
    assert policy.edge_floor(0) == Decimal("3")
    assert policy.edge_floor(1) == Decimal("7")
    assert policy.edge_floor(5) == Decimal("7")


def test_from_dict_reads_direct_floor_and_rejects_negative():
    policy = LinkPolicy.from_dict({"min_direct_tao": "2.5"})
    assert policy.min_direct_tao == Decimal("2.5")
    assert LinkPolicy.from_dict({}).min_direct_tao == Decimal("0")
    with pytest.raises(ValueError):
        LinkPolicy.from_dict({"min_direct_tao": "-1"})
    with pytest.raises(ValueError):
        LinkPolicy.from_dict({"min_direct_tao": "abc"})


# -- 2. 防蹭关联：核心行为 --------------------------------------------------
def test_direct_floor_drops_dust_transfer_and_its_edge():
    """设 1 TAO 后，0.001 TAO 的直连既不出结果，也不留在关联图里。"""

    records = [
        TransferRecord("dust", 1, "DUST", "S", Decimal("0.001")),
        TransferRecord("real", 2, "S", "REAL", Decimal("10")),
    ]
    report = discover("S", records, max_depth=2, policy=make_policy(direct="1"))

    assert addresses(report) == {"REAL"}
    assert not any(edge.tx_hash == "dust" for edge in report.edges)
    assert all("dust" not in hop.get("tx_hash", "") for row in report.related
               for hop in row.path_transfers)


def test_cumulative_threshold_alone_does_not_stop_dust_bumping():
    """排查结论的可执行证据：默认 0 时，灰尘地址靠下游 100 TAO 撑过累计门槛。"""

    records = dust_with_big_downstream()
    report = discover("S", records, max_depth=3, policy=make_policy(total="1"))

    rows = {row.address: row for row in report.related}
    assert "DUST" in rows, "旧行为：灰尘直连确实会变成 1 级关联"
    assert rows["DUST"].depth == 1
    assert rows["DUST"].total_tao > Decimal("100"), (
        "累计金额被它作为中转点的下游边撑高了——这正是单靠累计门槛防不住蹭关联的原因"
    )


def dust_with_big_downstream():
    return [
        TransferRecord("dust", 1, "DUST", "S", Decimal("0.001")),
        TransferRecord("big", 2, "DUST", "BIG", Decimal("100")),
        TransferRecord("far", 3, "BIG", "FAR", Decimal("100")),
    ]


def test_direct_floor_cuts_dust_and_its_whole_downstream():
    """设门槛后，灰尘边不进 BFS，它的整条下游（BIG / FAR）一并消失。"""

    report = discover(
        "S", dust_with_big_downstream(), max_depth=3,
        policy=make_policy(direct="1", total="1"),
    )
    assert report.related == []
    assert report.edges == []


def test_direct_floor_does_not_change_the_deeper_hop_rule():
    """两把尺各管各的：1 级门槛为 0 时，深层仍只受单边门槛管。"""

    records = [
        TransferRecord("t1", 1, "S", "A", Decimal("0.001")),
        TransferRecord("t2", 2, "A", "B", Decimal("2")),
        TransferRecord("t3", 3, "A", "C", Decimal("10")),
    ]
    legacy = discover(
        "S", records, max_depth=3,
        policy=make_policy(direct="0", edge="5"),
    )
    assert addresses(legacy) == {"A", "C"}, "第 1 跳 0.001 放行，第 2 跳 2 TAO 被单边门槛挡掉"

    guarded = discover(
        "S", records, max_depth=3,
        policy=make_policy(direct="1", edge="5"),
    )
    assert guarded.related == [], "第 1 跳不达标，整条链都走不出去"


# -- 3. 落盘与接口 ---------------------------------------------------------
def test_three_thresholds_roundtrip_and_normalisation():
    saved = settings_store.save_scan_thresholds(" 10.00 ", "0.50", "2.00")
    assert saved == {
        "min_edge_tao": "10",
        "min_total_tao": "0.5",
        "min_direct_tao": "2",
    }
    assert settings_store.load_scan_thresholds() == saved


def test_omitted_threshold_keeps_previous_value():
    """空输入框不能被硬塞成出厂值，把用户设好的防蹭关联门槛冲掉。"""

    settings_store.save_scan_thresholds("10", "50", "3")
    kept = settings_store.save_scan_thresholds(None, None, None)
    assert kept == {
        "min_edge_tao": "10",
        "min_total_tao": "50",
        "min_direct_tao": "3",
    }
    partial = settings_store.save_scan_thresholds(None, "80", None)
    assert partial["min_edge_tao"] == "10"
    assert partial["min_direct_tao"] == "3"
    assert partial["min_total_tao"] == "80"


def test_threshold_endpoint_does_not_clobber_omitted_fields(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "threshold.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        try:
            with TestClient(main_module.app) as client:
                client.post(
                    "/api/settings/scan-thresholds",
                    json={"min_edge_tao": "10", "min_total_tao": "50", "min_direct_tao": "3"},
                )
                response = client.post(
                    "/api/settings/scan-thresholds",
                    json={"min_total_tao": "80"},
                )
                assert response.status_code == 200
                assert response.json() == {
                    "min_edge_tao": "10",
                    "min_total_tao": "80",
                    "min_direct_tao": "3",
                }
        finally:
            database.engine.dispose()


def test_service_context_and_crawl_share_the_direct_floor(monkeypatch):
    """prepare_relation_context → LinkPolicy → CrawlState 一路不能丢字段。"""

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "ctx.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        try:
            context = service.prepare_relation_context(
                seed=SEED, depth=2,
                min_edge_tao="1", min_total_tao="0", min_direct_tao="3",
                data_source="taostats",
            )
            assert context.policy.min_direct_tao == Decimal("3")
            assert service.new_crawl(context).state.policy.min_direct_tao == Decimal("3")
        finally:
            database.engine.dispose()


def test_relations_endpoint_echoes_the_direct_floor(monkeypatch):
    """单轮接口要把本次生效的 1 级门槛回显，历史结果才解释得清。"""

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "relations.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        monkeypatch.setattr(main_module, "DATABASE", database)
        monkeypatch.setattr(main_module, "SERVICE", service)
        monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
        # 空库不会有关联地址，这里只验证参数透传；余额 RPC 一律打桩。
        monkeypatch.setattr(service_module, "query_account_balances", lambda *a, **k: {})
        try:
            with TestClient(main_module.app) as client:
                response = client.post(
                    "/api/relations",
                    json={
                        "seed": SEED, "depth": 1,
                        "min_edge_tao": "1", "min_total_tao": "0",
                        "min_direct_tao": "3", "data_source": "local",
                    },
                )
                assert response.status_code == 200
                assert response.json()["policy"]["min_direct_tao"] == "3"
        finally:
            database.engine.dispose()


def test_job_input_carries_the_direct_floor():
    assert main_module.RelationInput(seed=SEED).min_direct_tao == "0"
    assert main_module.RelationJobInput(seed=SEED, min_direct_tao="4").min_direct_tao == "4"
    assert main_module.ScanThresholdInput().min_direct_tao is None


def test_job_params_archive_the_direct_floor(monkeypatch):
    """分轮作业的归档参数要记下 1 级门槛，事后翻记录才能复现口径。"""

    monkeypatch.setattr(
        service_module, "get_transfer_page",
        lambda *_a, **_k: TransferPage([], None),
    )
    monkeypatch.setattr(
        service_module, "get_stake_transfer_page",
        lambda *_a, **_k: TransferPage([], None),
    )
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "job.db")
        database.initialize()
        service = LinkService(database, "wss://example.invalid")
        try:
            manager = RelationJobManager(round_interval=600, max_rounds=2)
            job = manager.create(
                service, seed=SEED, depth=1,
                min_edge_tao="1", min_total_tao="0", min_direct_tao="6",
            )
            assert job.params["min_direct_tao"] == "6"
            assert job.context.policy.min_direct_tao == Decimal("6")
        finally:
            database.engine.dispose()


# -- 4. Taostats 爬取侧：第一跳不再把灰尘对手方排进队列 ----------------------
def crawl_with_policy(policy: LinkPolicy, rows: list[dict]):
    state = CrawlState(seed=SEED, depth=3, policy=policy)
    crawl = TaostatsCrawl(
        state,
        fetch_transfer=lambda _addr, _page: TransferPage(rows, None),
        fetch_stake=lambda _addr, _page: TransferPage([], None),
        record_request=lambda *_a, **_k: None,
    )
    assert crawl._run_transfer(SEED, 1, 0) == "ok"
    return state


def transfer_rows():
    return [
        {
            "id": "1", "from": "DUST", "to": SEED, "block_number": 1,
            "amount": 1_000_000, "transaction_hash": "h1", "timestamp": "",
        },
        {
            "id": "2", "from": SEED, "to": "REAL", "block_number": 2,
            "amount": 5_000_000_000, "transaction_hash": "h2", "timestamp": "",
        },
    ]


def test_crawl_first_hop_respects_direct_floor():
    """设 1 TAO 后，0.001 TAO 的对手方不进待查队列，省额度也断下游。"""

    state = crawl_with_policy(make_policy(direct="1"), transfer_rows())
    assert "DUST" not in state.address_depth
    assert "REAL" in state.address_depth
    # 记录仍完整保留（本地索引口径不变），只是不拿它去扩展
    assert len(state.records_by_key) == 2


def test_crawl_default_zero_keeps_legacy_first_hop():
    """默认 0 时两方都入队，旧行为一字不改。"""

    state = crawl_with_policy(make_policy(direct="0"), transfer_rows())
    assert {"DUST", "REAL"} <= set(state.address_depth)


# -- 5. 前端与文档接线 -----------------------------------------------------
def test_frontend_and_docs_wire_the_third_threshold():
    html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="min-direct"' in html
    assert "1 级关联最低 TAO" in html
    assert 'min_direct_tao: $("#min-direct")?.value ?? "0"' in js
    assert "body.min_direct_tao = direct" in js
    assert '"#min-direct"' in js
    assert "policy.min_direct_tao" in js
    assert ".threshold-tip" in css

    for name in ("README.md", "使用说明.md", "使用说明.html"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "1 级关联最低 TAO" in text, f"{name} 未同步第三把尺"
    assert "min_direct_tao" in (ROOT / "README.md").read_text(encoding="utf-8")
