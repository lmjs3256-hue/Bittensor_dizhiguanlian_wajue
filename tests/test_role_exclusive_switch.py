"""系统角色互斥 + 标记后刷新/历史自动校正。

**用户要求（原话）**：

1. 「公共地址和交易所地址互斥，如果一个地址先标记公共地址、后又标记交易所
   地址，以最后操作为准，要有弹窗提示该地址要从原来的库移出，并移入后面
   确认的库。」
2. 「要确定这些特殊地址只要标记上，历史查询记录、刷新仍然要能够自动校正。」
3. 「远程传输延迟。我在远程标记了，可能主机那边还没更改过来，会有延迟。」

对应实现：

- 后端 `_drop_other_roles()`：三个标记入口共用，切身份时清掉另外两种角色，
  并在响应里回报 `switched_from`，让界面能说清「已从 X 分组移出」。
- 只有「确认」才算切换：「人工否定交易所」只是说"它不是交易所"，
  不该顺手抹掉已有的公共地址标记。
- 前端 `confirmRoleSwitch()`：发请求前若发现已有另一种角色，弹框写清
  "从 X 移出、移入 Y"。
- 前端 `localTerminalRoles()` / `passesThroughTerminal()`：本地重算用与后端
  同一口径判断终点，所以「标记 → 刷新结果」不需要重查也能剔除穿过终点的下游。
- 隧道延迟下前端状态可能是旧的：弹框未必触发，所以 toast 仍按后端回报补一句
  `switched_from`，不能默默挪库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.service as service_module
from app.db import AddressLibrary, Database
from app.service import LinkService

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

A = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
B = "5CF6Ruq6u7DoF6TYxyaFzEuviWi7tjvJtz19XgPS2b7XDCFM"


@pytest.fixture()
def service(tmp_path):
    database = Database(tmp_path / "switch.db")
    database.initialize()
    yield LinkService(database, "wss://example.invalid")
    database.engine.dispose()


def _add(service, address, **kw):
    with service.database.session() as db:
        row = AddressLibrary(
            address=address,
            label=kw.pop("label", ""),
            category=kw.pop("category", "其他"),
            categories=service_module.encode_categories(
                kw.pop("categories", ["其他"])
            ),
            book=kw.pop("book", ""),
            note=kw.pop("note", ""),
            source=kw.pop("source", "manual"),
            **kw,
        )
        db.add(row)
    return address


def _row(service, address):
    with service.database.session() as db:
        return db.query(AddressLibrary).filter_by(address=address).one()


def _cats(service, address):
    return service_module.row_categories(_row(service, address))


# -- 互斥：以最后一次操作为准 -------------------------------------------------
def test_public_then_exchange_drops_public(service):
    """先标公共地址、后标交易所：公共地址必须被清掉。"""

    _add(service, A, category="项目方", categories=["项目方"])

    service.decide_public_address(A, True)
    assert "PUBLIC" in _cats(service, A)

    result = service.decide_exchange_address(A, "confirm")
    cats = _cats(service, A)
    assert "EXCHANGE" in cats
    assert "PUBLIC" not in cats, "标交易所以后不能再留着公共地址标记"
    # 响应要能告诉前端"它原来是公共地址"
    assert result["switched_from"] == ["公共地址"]


def test_exchange_then_public_drops_exchange(service):
    """反方向同样以最后一次为准。"""

    _add(service, A, category="EXCHANGE", categories=["EXCHANGE"], exchange_confirmed=True)

    result = service.decide_public_address(A, True)
    cats = _cats(service, A)
    assert "PUBLIC" in cats
    assert "EXCHANGE" not in cats
    # 用的是分组名（与地址库清单里的组名一致），界面才能说清"从哪个分组移出"
    assert result["switched_from"] == [service_module.ROLE_LABEL_TEXT["EXCHANGE"]]


def test_validator_then_public_drops_validator(service):
    _add(service, A, category="VALIDATOR", categories=["VALIDATOR"])

    result = service.decide_public_address(A, True)
    cats = _cats(service, A)
    assert "PUBLIC" in cats
    assert "VALIDATOR" not in cats
    assert result["switched_from"] == [service_module.ROLE_LABEL_TEXT["VALIDATOR"]]


def test_no_switch_reports_empty_list(service):
    """本来就不是另一种角色时不要乱报"已移出"，否则界面会提示错东西。"""

    _add(service, A, category="项目方", categories=["项目方"])
    result = service.decide_public_address(A, True)
    assert result["switched_from"] == []


def test_rejecting_exchange_keeps_public(service):
    """「人工否定交易所」只是在否定交易所身份，不该抹掉公共地址。"""

    _add(service, A, category="EXCHANGE", categories=["EXCHANGE", "PUBLIC"])

    result = service.decide_exchange_address(A, "reject")
    cats = _cats(service, A)
    assert "PUBLIC" in cats, "否定交易所不应清掉公共地址标记"
    assert result["switched_from"] == []


def test_reset_exchange_keeps_public(service):
    """重置回「待确认」也不动已有身份。"""

    _add(service, A, category="EXCHANGE", categories=["EXCHANGE", "PUBLIC"])

    service.decide_exchange_address(A, "reset")
    assert "PUBLIC" in _cats(service, A)


# -- 标记后历史结果自动校正 ---------------------------------------------------
def test_marking_public_triggers_history_correction(service):
    """标记公共地址要顺手把历史快照里穿过它的下游剪掉。"""

    _add(service, A, category="项目方", categories=["项目方"])
    with service.database.session() as db:
        db.add(
            service_module.RelationArchive(
                seed="5Seed",
                depth=3,
                status="complete",
                related_count=2,
                payload="{}",
            )
        )

    result = service.decide_public_address(A, True)
    # 只要有这条键就说明走了校正流程（没有历史记录时统计为 0 也算正常）
    assert "correction" in result
    assert result["correction"]["ok"] is True


# -- 前端契约 ---------------------------------------------------------------
def test_frontend_has_role_switch_confirm():
    assert "function confirmRoleSwitch(" in APP_JS
    # 三个标记入口都要在切换前询问
    for anchor in (
        'decision === "confirm" && !confirmRoleSwitch(address, "EXCHANGE")',
        'marking && !confirmRoleSwitch(address, "PUBLIC")',
        'marking && !confirmRoleSwitch(address, "VALIDATOR")',
    ):
        assert anchor in APP_JS, anchor


def test_confirm_text_mentions_move_between_groups():
    """弹窗必须写清"从原分组移出、移入新分组"——这是用户明确要求的。"""

    start = APP_JS.index("function confirmRoleSwitch(")
    body = APP_JS[start : start + 900]
    assert "分组移出" in body
    assert "移入" in body
    assert "以本次操作为准" in body


def test_frontend_prunes_paths_through_terminal_on_refresh():
    """刷新（本地重算）也要按终点规则剔除"穿过终点"的下游。

    否则会出现"后端历史已剪、前端刷新又放回来"的来回打架；而且用户要求的
    「标记上以后刷新就能自动校正」也做不到。
    """

    assert "function localTerminalRoles(" in APP_JS
    assert "function passesThroughTerminal(" in APP_JS
    start = APP_JS.index("function rebuildRelationsLocally(")
    body = APP_JS[start : start + 6000]
    assert "passesThroughTerminal(row, terminals, seed)" in body
    assert "prunedByRole" in body


def test_local_terminal_rules_match_backend():
    """前端口径必须和后端一致：否定措辞不算终点、未确认的交易所不算终点。"""

    start = APP_JS.index("function localTerminalRoles(")
    body = APP_JS[start : start + 1600]
    assert '["否定"' in body or '"否定"' in body
    assert "exchange_confirmed" in body
    assert "exchange_rejected" in body
    assert "公共地址" in body and "验证者" in body and "交易所" in body


def test_toast_reports_backend_switch():
    """隧道延迟下前端状态可能过期、弹框不触发，后端回报仍要出现在 toast 里。"""

    assert "function switchedText(" in APP_JS
    assert APP_JS.count("switchedText(result)") >= 3
