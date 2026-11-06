"""地址库关联明细检索：跳数与翻页上限跟关联层级对齐（0.8.4）。

这里锁三件事：

1. 上限只有一个真源 —— ``MAX_RELATION_DEPTH``，不是各处写死 3 / 20。
2. 深层链路是真的能拼出来（2～10 跳逐跳比对），不是「放开参数但只能跑到 3 跳」。
3. 请求预算只有 5 次时，报告要说人话（哪一侧没展开、能靠缓存接着走），
   而不是把「预算不够」说成「没有通路」。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app import interlink, taostats
from app.db import Database
from app.relation_config import DEFAULT_RELATION_DEPTH, MAX_RELATION_DEPTH
from app.service import LinkService

RAO = 1_000_000_000
A = "5HTLuZKULeBe3PEcXYsL9ABRCf3vJuw6dVS2wTLwyoRuTif7"
B = "5C51bmwMxk9TTswMj2wkiCvdAyWDo3wXw9i4Bcoqf93WjaHP"


def build_chain(hops: int, branches: int = 0):
    """造一条 A → m1 → … → B 的链，返回（节点列表, 转账行列表）。

    ``branches`` 给每个节点挂几个旁支地址：纯链在 5 次预算里就能走完，测不出
    「预算不够」的分支；带旁支之后两岸候选变多，更接近真实地址的样子。
    """

    nodes = [A] + [f"addr{index}" for index in range(1, hops)] + [B]
    edges = [
        {
            "from": nodes[index],
            "to": nodes[index + 1],
            "amount": str((100 - index) * RAO),
            "block_number": 1000 + index,
            "timestamp": f"2026-01-{index + 1:02d}T00:00:00.000Z",
            "transaction_hash": f"0x{index:04d}",
            "extrinsic_id": f"{1000 + index}-0001",
        }
        for index in range(hops)
    ]
    for position, address in enumerate(nodes):
        for branch in range(branches):
            edges.append(
                {
                    "from": address,
                    "to": f"leaf{position}_{branch}",
                    "amount": str(5 * RAO),
                    "block_number": 2000 + position * 10 + branch,
                    "timestamp": "2026-02-01T00:00:00.000Z",
                    "transaction_hash": f"0xleaf{position}_{branch}",
                    "extrinsic_id": f"leaf{position}_{branch}",
                }
            )
    return nodes, edges


def make_fetcher(edges):
    """按 address 返回该地址的流水；from_address/to_address 精确查询一律空。"""

    def fake(**kwargs):
        if kwargs.get("from_address") or kwargs.get("to_address"):
            return taostats.TransferPage(rows=[], next_page=None)
        address = kwargs.get("address")
        rows = [row for row in edges if row["from"] == address or row["to"] == address]
        return taostats.TransferPage(rows=rows, next_page=None)

    return fake


@pytest.mark.parametrize("hops", list(range(2, MAX_RELATION_DEPTH + 1)))
def test_walk_paths_reaches_any_depth_up_to_the_cap(monkeypatch, hops):
    """2～10 跳都要真的拼出完整链路，节点数、跳数、逐跳方向全对。"""

    nodes, edges = build_chain(hops)
    monkeypatch.setattr(taostats, "get_transfer_page_query", make_fetcher(edges))

    query = interlink.InterlinkQuery(
        source=A,
        members=[interlink.Member(address=B)],
        mode="address",
        max_hops=hops,
        max_pages=1,
    )
    fetcher = interlink._Fetcher(budget=80)  # 预算给足，只测搜索本身
    paths, notes, truncated = interlink._walk_paths(fetcher, A, B, query)

    assert paths, f"{hops} 跳链路没拼出来：{notes}"
    path = paths[0]
    assert path["hops"] == hops
    assert path["nodes"] == nodes
    assert len(path["steps"]) == hops
    # 每一跳的 from 必须正好是路径上的当前节点，方向不能反。
    for index, step in enumerate(path["steps"]):
        assert step["from"] == path["nodes"][index]
        assert step["to"] == path["nodes"][index + 1]
    assert truncated is False


def test_walk_paths_respects_the_hop_limit(monkeypatch):
    """链路 5 跳但只允许 2 跳时不能硬凑出一条 5 跳的路径。"""

    nodes, edges = build_chain(5)
    monkeypatch.setattr(taostats, "get_transfer_page_query", make_fetcher(edges))

    query = interlink.InterlinkQuery(
        source=A, members=[interlink.Member(address=B)], mode="address",
        max_hops=2, max_pages=1,
    )
    fetcher = interlink._Fetcher(budget=80)
    paths, notes, _ = interlink._walk_paths(fetcher, A, B, query)

    assert paths == []
    assert any("2 跳" in note for note in notes)


def test_budget_short_fallback_says_which_side_stalled(monkeypatch):
    """5 次预算展开不到 8 跳时，要说「预算只到几跳」而不是「没有通路」。"""

    nodes, edges = build_chain(8, branches=2)
    monkeypatch.setattr(taostats, "get_transfer_page_query", make_fetcher(edges))

    query = interlink.InterlinkQuery(
        source=A, members=[interlink.Member(address=B)], mode="address",
        max_hops=8, max_pages=1,
    )
    fetcher = interlink._Fetcher(budget=taostats.LOCAL_REQUESTS_PER_MINUTE)
    paths, notes, truncated = interlink._walk_paths(fetcher, A, B, query)

    assert paths == []
    assert truncated is True
    text = " ".join(notes)
    assert "预算" in text
    assert "缓存" in text, "要告诉用户再点一次能靠缓存接着走，否则以为没救"


def test_second_pass_uses_cache_and_finishes(monkeypatch):
    """第一次预算用完，再点几次能靠 5 分钟缓存逐轮加深直到走完。

    提示语里写的是「再点一次会接着往更深处走」，这条测试就是锁那句话是真的：
    每轮都会因为上一轮的缓存而多展开一层，直到 8 跳链路被拼出来。
    """

    nodes, edges = build_chain(8, branches=2)
    seen: set[str] = set()

    def fake(**kwargs):
        if kwargs.get("from_address") or kwargs.get("to_address"):
            return taostats.TransferPage(rows=[], next_page=None)
        address = kwargs.get("address")
        cached = address in seen
        seen.add(address)
        rows = [row for row in edges if row["from"] == address or row["to"] == address]
        return taostats.TransferPage(rows=rows, next_page=None, from_cache=cached)

    monkeypatch.setattr(taostats, "get_transfer_page_query", fake)
    query = interlink.InterlinkQuery(
        source=A, members=[interlink.Member(address=B)], mode="address",
        max_hops=8, max_pages=1,
    )

    rounds: list[tuple[int, int]] = []  # (本轮新请求, 本轮缓存命中)
    paths: list = []
    for _ in range(4):
        fetcher = interlink._Fetcher(budget=taostats.LOCAL_REQUESTS_PER_MINUTE)
        paths, _, _ = interlink._walk_paths(fetcher, A, B, query)
        rounds.append((fetcher.requests, fetcher.cache_hits))
        if paths:
            break

    assert paths, f"四轮都没走完，缓存没有累积：{rounds}"
    assert paths[0]["hops"] == 8
    # 第一轮全是新请求、零缓存；最后一轮一定有缓存命中，否则「接着走」是假的。
    assert rounds[0][1] == 0
    assert rounds[-1][1] > 0


def test_hop_and_page_caps_follow_relation_depth():
    """上限只认 MAX_RELATION_DEPTH 一个真源。"""

    assert interlink.MAX_PAGES == MAX_RELATION_DEPTH
    assert interlink.InterlinkQuery(source=A, members=[]).max_hops == DEFAULT_RELATION_DEPTH
    assert interlink.InterlinkQuery(source=A, members=[]).max_pages == DEFAULT_RELATION_DEPTH


@pytest.mark.parametrize("field_name", ["max_hops", "max_pages"])
def test_api_accepts_up_to_cap_and_rejects_beyond(field_name):
    """接口层 1..10 收，11 拒；默认仍是 3。"""

    from fastapi.testclient import TestClient

    import app.main as main_module

    payload = {"source": A, "target": B, field_name: MAX_RELATION_DEPTH}
    with TestClient(main_module.app) as client:
        ok = client.post("/api/interlink", json=payload)
        over = client.post("/api/interlink", json={**payload, field_name: MAX_RELATION_DEPTH + 1})
        below = client.post("/api/interlink", json={**payload, field_name: 0})

    assert ok.status_code == 200
    assert over.status_code == 422
    assert below.status_code == 422


def test_service_clamps_hop_and_page_values(monkeypatch):
    """绕过接口直接调 service 时也要夹住，不能把超大值带进搜索。"""

    monkeypatch.setattr(
        taostats, "get_transfer_page_query", make_fetcher([])
    )
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "cap.db")
        database.initialize()
        try:
            service = LinkService(database, "wss://example.invalid")
            result = service.interlink_search(
                source=A, target=B, max_hops=99, max_pages=99
            )
        finally:
            database.engine.dispose()

    assert result["stats"]["max_hops"] == MAX_RELATION_DEPTH


def test_frontend_drops_the_hardcoded_option_lists():
    """模板不能再写死跳数 / 翻页上限，否则后端放开页面还是选不到。"""

    template = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert 'id="interlink-hops"' in template
    assert "data-hop-options" in template, "跳数下拉要由 applyDepthOptions 动态生成"
    # 翻页上限的 max 由 JS 按层级上限写，模板里不能留 20 这个旧硬编码。
    assert 'max="20"' not in template.split('id="interlink-max-pages"')[1][:200]

    app_js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert 'document.querySelectorAll("select[data-depth-options]")' in app_js
    assert 'const hops = $("#interlink-hops");' in app_js
    assert 'pages.max = String(max);' in app_js
