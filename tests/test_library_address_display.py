"""回归测试：地址库清单的地址必须「掩码显示 + 复制按钮 + 不换行」。

锁死契约：
- 库清单每一行的地址列用 `shortAddr(row.address)`（头 4 + 尾 4、中间隐藏）渲染，
  不再直接渲染完整 SS58（`addressLink(row.address, row.address)` 那种子会折两行）。
- 地址后紧跟一个「复制」按钮，带 `data-copy-address`（复制完整地址）。
- CSS 里 `.library-address-cell .address-main` 设了 `white-space:nowrap`，确保单行不换行。
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
STYLES_CSS = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")


def _library_rows_table_body():
    start = APP_JS.index("function libraryRowsTable(")
    end = APP_JS.index("function libraryTable(", start)
    return APP_JS[start:end]


def test_library_row_uses_masked_address_not_full():
    body = _library_rows_table_body()
    # 新契约：用 shortAddr 渲染掩码地址（仍可跳 Taostats）。
    assert "addressLink(row.address, shortAddr(row.address))" in body
    # 旧写法（完整地址直接当链接文字，窄列会折两行）必须消失。
    assert "addressLink(row.address, row.address)" not in body


def test_library_row_has_copy_button_with_full_address():
    body = _library_rows_table_body()
    assert "copy-address" in body
    assert "data-copy-address" in body


def test_library_address_cell_css_prevs_single_line():
    assert ".library-address-cell .address-main" in STYLES_CSS
    # 截取该规则块，确认含 white-space:nowrap（单行不换行）。
    idx = STYLES_CSS.index(".library-address-cell .address-main")
    block = STYLES_CSS[idx : idx + 200]
    assert "white-space:nowrap" in block
