"""地址库「按后四位快速检索」的契约测试。

用户需求：「在已知地址库里通过地址后四位快速检索匹配的地址」。

这里锁死三件事：
1. 输入 4 位后缀能精确命中，且**排在结果最前**（用户就是来找那一个地址）；
2. 「后缀命中」与「其他字段/子串命中」要区分开——实测 `Thor` 这类词会同时
   命中一个真以 Thor 结尾的地址，以及另一个地址中间的 Thor 片段，不区分的话
   用户无法判断哪条才是自己要找的；
3. 输入明显像后缀却无命中时，要明确说「没有以 XXX 结尾的地址」，而不是
   让用户盯着空列表猜是搜索坏了还是真没有。
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
STYLES = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")


def test_suffix_match_helper_exists_and_is_strict():
    """匹配必须用 endsWith，不能用 includes（否则中间片段也算后缀命中）。"""

    start = APP_JS.index("function librarySearchMatchKind(")
    body = APP_JS[start : APP_JS.index("\nfunction ", start + 10)]
    assert "endsWith(query)" in body
    assert "includes(" not in body
    assert 'return "suffix"' in body


def test_suffix_hits_are_sorted_first():
    start = APP_JS.index("function sortLibraryRowsBySearch(")
    body = APP_JS[start : APP_JS.index("\nfunction ", start + 10)]
    # 分两组、后缀组在前（不锁具体写法，锁行为）
    assert "suffix" in body and "rest" in body
    assert "[...suffix, ...rest]" in body
    assert '=== "suffix"' in body


def test_render_uses_the_sort_helper():
    """只写排序函数不接进渲染等于没做。"""

    start = APP_JS.index("function renderLibraryList(")
    body = APP_JS[start : APP_JS.index("\nfunction ", start + 10)]
    assert "sortLibraryRowsBySearch(" in body


def test_hint_distinguishes_no_suffix_hit():
    start = APP_JS.index("function renderLibrarySearchHint(")
    body = APP_JS[start : APP_JS.index("\nfunction ", start + 10)]
    assert "按地址后四位匹配到" in body
    assert "没有以" in body and "结尾的地址" in body


def test_search_box_advertises_suffix_usage():
    assert "后四位" in INDEX_HTML
    assert 'id="library-search-hint"' in INDEX_HTML


def test_suffix_hit_row_is_highlighted():
    assert 'class="library-row-suffix-hit"' in APP_JS
    assert ".library-row-suffix-hit" in STYLES
    assert ".library-suffix-tag" in STYLES


def test_match_minimum_length_is_four():
    assert "const LIBRARY_SUFFIX_MIN = 4;" in APP_JS


def test_groups_auto_expand_while_searching():
    """搜索时命中的组要自动展开。

    否则用户搜到地址却只看到折叠的组标题，还得挨个点开——
    「搜到了却看不到」比搜不到更让人困惑。（实测踩到）
    """

    start = APP_JS.index("const groupId = `library-group-")
    body = APP_JS[start : start + 400]
    assert "searching" in body
    assert "searching ||" in body
