"""路径明细的静态资源回归测试。

这两条断言来自真实的排版 bug：
1. 明细表用 `table-layout:fixed` + thead th 定宽。如果列宽写回 colgroup，
   `<col>` 的 width 会被浏览器忽略，六列被平均成 84/169/226/200/288/169，
   「跳」列白占 84px、路径列只剩 169px，两个 48 位地址被迫换行。
2. 外层关联表的列宽规则原来是 `#related > table th:nth-child(n)`，
   这是**后代**选择器，会把嵌在表格里的明细表表头也按 9 列宽度分配，
   导致「跳」列被撑到 700px 以上。必须收窄到 `#related > table > thead > tr > th`。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


def test_path_detail_columns_use_thead_not_colgroup():
    """明细表列宽必须写在 thead th 上，不能靠 colgroup。"""

    # 生成明细行时，表头要带 c-hop / c-way / c-kind 等 class
    assert 'class="c-hop"' in JS
    assert 'class="c-way"' in JS
    assert "<colgroup>" not in JS, "colgroup 的 col width 在 fixed 布局下会被忽略"
    # CSS 里必须真的用 th.c-hop 定宽
    assert ".path-detail thead th.c-hop" in CSS
    assert "table-layout:fixed" in CSS.replace(" ", "").replace(": ", ":")


def test_related_column_widths_do_not_leak_into_nested_path_table():
    """外层关联表的列宽规则不能命中内层明细表。"""

    assert "#related > table th:nth-child(" not in CSS, (
        "后代选择器会把明细表表头也按 9 列宽度分配，跳数列会被撑到 700px"
    )
    assert "#related > table > thead > tr > th:nth-child(1)" in CSS


def test_collapse_button_uses_theme_aware_on_accent_color():
    """深灰主题的 --accent 是浅色，白字压上去对比度只有 2.13；要用 --on-accent。"""

    assert "--on-accent" in CSS
    # 每个主题都要定义，否则回落到没有值
    for theme in (
        ':root {',
        '@media (prefers-color-scheme: dark)',
        ':root[data-theme="eye"]',
        ':root[data-theme="darkgray"]',
    ):
        start = CSS.index(theme)
        block = CSS[start : start + 700]
        assert "--on-accent" in block, f"{theme} 缺少 --on-accent"
    # 实心按钮的文字色必须跟着主题走
    collapse_rule = CSS[CSS.index("button.path-collapse {") :][:400]
    assert "var(--on-accent)" in collapse_rule
    assert "color:#fff !important" not in collapse_rule


def test_path_detail_has_two_collapse_entries_for_long_paths():
    """超过 3 跳时底部再给一个收起入口，长路径不用滚回顶部。"""

    assert "path-detail-foot" in JS
    assert "hops.length > 3" in JS
    assert ".path-detail-foot" in CSS


def test_path_detail_address_uses_the_same_truncation_as_main_table():
    """路径明细的地址截断必须和主表同一套（头 4 + 尾 4）。

    0.8.2 重排明细表时这里单独用了头6尾6「方便人工比对」，结果同一屏里
    主表 `5Hiv…N8kS`、明细 `5Hiv4k…8kSn2P` 两种长度，同一个地址看着像两个。
    现在 `pathAddr()` 直接复用 `shortAddr()`，全站只有一处截断规则。
    """

    # 全站唯一截断规则仍在 shortAddr：
    assert "text.slice(0, 4)}…${text.slice(-4)}" in JS
    # 明细的 pathAddr 不许再自己写一套数字
    start = JS.index("function pathAddr(")
    body = JS[start : JS.index("}", start)]
    assert "shortAddr(value)" in body, "pathAddr 应复用 shortAddr，不要再单独截断"
    assert "slice(" not in body, "pathAddr 里不应再出现自己的 slice 截断"
    # 明细行必须真的用 pathAddr 渲染（而不是直接短地址或完整地址）
    assert "pathAddr(from)" in JS and "pathAddr(to)" in JS
