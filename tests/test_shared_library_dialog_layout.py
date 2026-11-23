"""事件监控「选择地址库」弹窗的布局回归测试（1.0.27）。

线上现象：点开弹窗后，143 个地址库全部显示成一条条 1~2px 的灰色横线，
库名和地址数完全看不见。DOM 里内容其实都在，是 CSS 把卡片压扁了。

根因：.shared-book-list 同时是 grid（自动行默认按可用高度均分）、设了
max-height、又被 1005 行那段 flex:1 / min-height:0 规则接管。子项一多，
grid 就把每个自动行压缩到 1px 左右；卡片内的 .shared-book-row 虽然有 58px
内容，但因为 overflow:hidden 被裁掉，视觉上只剩横线。

本测试锁定两条防御规则，缺一条都可能让问题复现：
  - grid-auto-rows: min-content：行高至少等于内容高度，不再被均分压扁；
  - align-content: start：行从顶部排布，多余空间留在底部而不是拉伸行。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app" / "static" / "event-monitor" / "styles.css").read_text(encoding="utf-8")


def _rule_body(selector: str) -> str:
    """取出某个选择器的 CSS 规则体（只取第一处）。"""

    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS)
    assert match, f"styles.css 里找不到 {selector} 规则"
    return match.group(1)


def test_shared_book_list_keeps_natural_row_height():
    """弹窗列表必须锁住行高，否则地址库一多就被压成看不见的横线。"""

    body = _rule_body(".shared-book-list")
    assert re.search(r"grid-auto-rows\s*:\s*min-content", body), (
        ".shared-book-list 缺少 grid-auto-rows:min-content；"
        "100+ 个库卡片会被 grid 均分压扁到 1px，弹窗看起来像空白骨架"
    )
    assert re.search(r"align-content\s*:\s*start", body), (
        ".shared-book-list 缺少 align-content:start；"
        "行会被拉伸或分散，卡片高度不稳定"
    )


def test_shared_book_list_still_scrolls_in_dialog():
    """修复不能牺牲滚动：固定高度 + overflow:auto 必须保留。"""

    body = _rule_body(".shared-book-list")
    assert re.search(r"max-height\s*:", body), "列表需要 max-height 才能在弹窗内滚动"
    assert re.search(r"overflow\s*:\s*auto", body), "列表需要 overflow:auto 才能滚动查看全部库"
