"""入库日志的地址必须能点开 Taostats 核对（人工判断是否为特殊系统地址）。

用户需求原话：「入库日志的入库地址，要能点击跳转 taostars 查看信息，方便
人工判断是否为特殊系统地址」。

锁死契约：
- 列表行用 `addressLink(..., shortAddr(...))`：掩码文字但可点，避免窄屏撑破；
- 详情面板用 `addressLink(item.address, item.address)`：完整地址也能点；
- 两处都走同一个 `taostatsAccountUrl()`，`target=_blank` + `rel=noopener`；
- 触发地址同样可点（判断归属时经常要顺着往回看）。
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


def _watch_log_html():
    start = APP_JS.index("function watchLogHtml(")
    end = APP_JS.index("\nfunction ", start + 10)
    return APP_JS[start:end]


def test_line_address_is_a_taostats_link():
    body = _watch_log_html()
    # 列表行：掩码地址做链接文字，不是裸 <code>
    assert "addressLink(item.address, shortAddr(item.address))" in body
    # 旧写法（裸 code 包短地址）必须消失，否则点了没反应
    assert '<code class="watch-log-addr">' not in body


def test_detail_shows_clickable_full_address():
    body = _watch_log_html()
    assert "addressLink(item.address, item.address)" in body
    # 详情里的完整地址不能退回裸 code
    assert "<code>${esc(item.address)}</code>" not in body


def test_trigger_address_is_also_clickable():
    body = _watch_log_html()
    assert "addressLink(item.trigger_address, item.trigger_address)" in body


def test_address_link_opens_in_new_tab_safely():
    start = APP_JS.index("function taostatsAccountUrl(")
    body = APP_JS[start : APP_JS.index("\nfunction libraryRowsTable(", start)]
    assert "https://taostats.io/account/" in body
    assert "encodeURIComponent(address)" in body
    assert 'target="_blank"' in body
    assert 'rel="noopener"' in body
