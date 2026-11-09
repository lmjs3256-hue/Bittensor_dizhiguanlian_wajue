"""起始地址输入框：变窄时优先露出「后四位」。

用户需求原话：「起始地址输入框在页面变窄时优先显示地址后4位，当前是页面
变窄时优先隐藏后四位。」

原生 `<input>` 宽度不够时只按光标位置滚动，失焦后回到开头，于是看到的是
`5DceuTr7XPqw…`——而 SS58 前几位几乎都是 `5`，真正能区分地址的是尾巴 4 位。
实测（1440px 桌面）输入框只有 313px、地址需要 358px，`scrollLeft` 停在 0。

修法是显式把 `scrollLeft` 推到最右，并在失焦、赋值、窗口 resize 时都执行。
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


def _auto_select_block():
    start = APP_JS.index("(function enableSeedAutoSelect()")
    end = APP_JS.index("\n})();", start)
    return APP_JS[start:end]


def test_reveal_seed_tail_sets_scroll_left():
    body = _auto_select_block()
    assert "function revealSeedTail(" in body
    # 关键：真的推到最右，而不是只把光标移到末尾
    assert "el.scrollLeft = el.scrollWidth" in body


def test_reveal_only_when_content_overflows():
    body = _auto_select_block()
    # 放得下就别乱动，否则短输入也会被强行滚动
    assert "el.scrollWidth <= el.clientWidth" in body


def test_reveal_runs_on_blur_and_resize():
    body = _auto_select_block()
    assert 'addEventListener("blur"' in body
    assert 'addEventListener("resize"' in body
    assert "revealSeedTail(input)" in body


def test_reveal_after_programmatic_value_set():
    """JS 赋地址后也要滚到尾部，否则点了「用这个地址」还是看到开头。"""

    assert "window.revealSeedTail" in APP_JS
    # 两处赋值：默认地址回填、扫描结果里的「用这个地址」
    assert APP_JS.count("if (window.revealSeedTail) window.revealSeedTail($(\"#seed\"));") >= 2
