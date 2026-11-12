"""「同步地址库」必须跟结果表的筛选走（0.9.7）。

用户原话：「我明明筛选条件是关联层级 1 级，关联强度全部强以上，刷新结果只有 8
条地址记录，为何同步地址库会新增 66 条。」

原因是同步按钮直接提交 `state.lastResult.related` 全量，完全无视结果表的
层级 / 强度 / 金额筛选和手动移除。建库（`createBookFromResult`）早就改成走
`relatedVisibleRows()` 并在提交前弹确认框，同步这条路径一直没跟上——于是
同一屏上下两个按钮对"当前条件"的理解不一样。

这里锁住四件事：

1. 同步的地址来源必须是 `relatedVisibleRows()`，不能是 `lastResult.related`；
2. 剔除规则与建库同一套（交易所 / 验证者 / 公共地址不进展用户库）；
3. 提交前弹确认框，把筛选条件、本次条数、被隐藏的条数都说清楚；
4. 同步提示（`#sync-hint`）随筛选变化，而不是永远报全量。
"""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _block(start_marker: str) -> str:
    """截取一个顶层函数的完整函数体（按 `\n}\n` 收尾）。"""

    js = _js()
    start = js.index(start_marker)
    return js[start: js.index("\n}\n", start)]


def test_sync_uses_visible_rows_instead_of_all_related():
    """回归核心：同步不能再把 related 全量提交。"""

    block = _block("async function syncCurrentResult(mode)")
    assert "relatedVisibleRows()" in block, "同步要按当前可见集合取地址"
    assert "selectExportableAddresses(visible)" in block, "交易所/验证者/公共地址要剔除"
    assert "related: state.lastResult.related" not in block, "不能直接提交全量关联"
    assert "related: addresses" in block, "提交的是筛过的地址清单"


def test_sync_confirm_explains_hidden_rows():
    """确认框必须说明「还有多少条被筛选隐藏、不会同步」。

    用户就是被"看不到的 58 条"坑到的，光改提交范围而不告知，下次还会困惑。
    """

    block = _block("async function syncCurrentResult(mode)")
    assert "describeCurrentFilters()" in block, "确认框要列出当前筛选条件"
    assert "另有 ${hidden} 个" in block
    assert "不会同步" in block
    assert "window.confirm(" in block, "提交前要让人确认一次"
    # 取消就不发请求：confirm 的返回值必须被判断。
    assert "if (!window.confirm(lines.join(\"\\n\"))) return;" in block


def test_sync_hint_tracks_filters():
    """提示文案要报"当前能同步几个"，并在筛选变化时刷新。"""

    hint = _block("function updateSyncHint()")
    assert "relatedVisibleRows().length" in hint
    assert "hidden" in hint and "不会同步" in hint

    table = _block("function refreshRelatedTable()")
    assert "updateSyncHint()" in table, "筛选一变，同步提示要跟着变"


def test_sync_accepts_plain_address_list():
    """地址清单现在传字符串数组，syncAddresses 不能再假设是行对象。"""

    block = _block("async function syncAddresses(")
    assert 'typeof row === "string"' in block


def test_history_sync_still_passes_record_rows():
    """历史记录的同步走自己的记录数据，不受结果表筛选影响（回归保护）。"""

    block = _block("async function syncHistoryRecord(id)")
    assert "data.result.related" in block
