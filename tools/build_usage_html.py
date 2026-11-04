"""把《使用说明.md》生成一份可以离线双击查看的 HTML。

用法：
    .venv\\Scripts\\python.exe -m pip install markdown
    .venv\\Scripts\\python.exe tools\\build_usage_html.py

仓库里的 使用说明.html 就是这个脚本的产物；改完 Markdown 后重新跑一次即可。
markdown 只用于生成文档，不是运行时依赖，所以没有写进 requirements.txt。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import markdown
except ModuleNotFoundError:  # pragma: no cover - 仅在缺少文档依赖时触发
    sys.exit(
        "缺少 markdown 库。先执行：\n"
        "  .venv\\Scripts\\python.exe -m pip install markdown\n"
        "然后重新运行本脚本。"
    )


def github_slug(value: str, separator: str) -> str:
    """按 GitHub 的规则生成锚点：去掉标点、空格转连字符、保留中文。

    这样 Markdown 里手写的 `#三安装只需一次` 这类内链在离线 HTML 里同样可用。
    """

    value = value.strip().lower()
    value = re.sub(r"[^\w\u4e00-\u9fff \-]", "", value)
    value = re.sub(r"\s+", separator, value)
    return value.strip(separator)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "使用说明.md"
TARGET = ROOT / "使用说明.html"
REPO = "https://github.com/lmjs3256-hue/Bittensor_dizhiguanlian_wajue/blob/main"

GITHUB_LINKS = {
    "(README.md)": f"({REPO}/README.md)",
    "(CHANGELOG.md)": f"({REPO}/CHANGELOG.md)",
    "(docs/DESIGN_FEASIBILITY.md)": f"({REPO}/docs/DESIGN_FEASIBILITY.md)",
    "(docs/VERIFICATION.md)": f"({REPO}/docs/VERIFICATION.md)",
    "(docs/HANDOFF.md)": f"({REPO}/docs/HANDOFF.md)",
}

STYLE = """
:root {
  --bg: #1e2126;
  --panel: #282c33;
  --line: #3b424c;
  --ink: #e8ecf2;
  --muted: #a4afbd;
  --accent: #7fb0ee;
  --warn: #f0c072;
  --danger: #f08078;
  --mono: ui-monospace, "Cascadia Mono", Consolas, "Courier New", monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 20px 80px;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.85 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 920px; margin: 0 auto; }
header.doc-head { padding: 46px 0 22px; border-bottom: 1px solid var(--line); margin-bottom: 26px; }
header.doc-head .eyebrow { color: var(--accent); font-size: 11.5px; letter-spacing: 2px; font-weight: 700; }
header.doc-head h1 { margin: 10px 0 8px; font-size: 30px; letter-spacing: -.3px; }
header.doc-head p { margin: 0; color: var(--muted); font-size: 13.5px; }
nav.toc {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px 22px;
  margin: 0 0 30px;
}
nav.toc .toc-title { color: var(--muted); font-size: 12px; letter-spacing: 1.6px; font-weight: 700; margin-bottom: 8px; }
nav.toc ul { list-style: none; margin: 0; padding: 0; columns: 2; column-gap: 28px; }
nav.toc > ul > li { font-size: 13.5px; margin: 3px 0; break-inside: avoid; }
nav.toc ul ul { display: none; }
nav.toc a { color: var(--ink); text-decoration: none; }
nav.toc a:hover { color: var(--accent); }
h2 {
  margin: 46px 0 14px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
  font-size: 21px;
  letter-spacing: -.2px;
}
h3 { margin: 30px 0 10px; font-size: 16.5px; color: var(--ink); }
h4 { margin: 22px 0 8px; font-size: 14.5px; color: var(--muted); }
p { margin: 11px 0; }
a { color: var(--accent); }
strong { color: #fff; font-weight: 650; }
ul, ol { padding-left: 24px; }
li { margin: 6px 0; }
code {
  font-family: var(--mono);
  font-size: 12.8px;
  padding: 2px 6px;
  border-radius: 5px;
  background: #16181c;
  border: 1px solid var(--line);
  color: #ffd9a0;
}
pre {
  background: #16181c;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 14px 16px;
  overflow-x: auto;
  margin: 14px 0;
}
pre code { border: 0; background: transparent; padding: 0; color: #d8e0ea; font-size: 12.8px; line-height: 1.7; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13.5px; }
th, td { padding: 9px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { background: #23272d; color: var(--muted); font-weight: 650; font-size: 12.5px; white-space: nowrap; }
tbody tr:hover { background: #23272d; }
blockquote {
  margin: 18px 0;
  padding: 12px 18px;
  border-left: 3px solid var(--accent);
  background: var(--panel);
  border-radius: 0 8px 8px 0;
  color: #dbe3ee;
}
blockquote p { margin: 4px 0; }
hr { border: 0; border-top: 1px solid var(--line); margin: 34px 0; }
footer.doc-foot { margin-top: 50px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12.5px; }
@media (max-width: 640px) {
  body { padding: 0 14px 60px; font-size: 14.5px; }
  nav.toc ul { columns: 1; }
  header.doc-head h1 { font-size: 24px; }
  table { font-size: 12.5px; }
  th, td { padding: 8px; }
}
"""


def build() -> Path:
    text = SOURCE.read_text(encoding="utf-8")
    for old, new in GITHUB_LINKS.items():
        text = text.replace(old, new)

    converter = markdown.Markdown(
        extensions=["extra", "toc", "sane_lists"],
        extension_configs={
            "toc": {"toc_depth": "2-2", "permalink": False, "slugify": github_slug}
        },
    )
    body = converter.convert(text)
    # toc 扩展把目录放在 md.toc 里，这里单独拿出来放到页头下方
    toc_html = ""
    if getattr(converter, "toc", ""):
        toc_html = '<nav class="toc"><div class="toc-title">目录</div>' + converter.toc + "</nav>"

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bittensor 地址关联挖掘 · 使用说明</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="doc-head">
  <div class="eyebrow">BITTENSOR ADDRESS LINK · USER GUIDE</div>
  <h1>Bittensor 地址关联挖掘 · 使用说明</h1>
  <p>面向第一次使用的人：安装、启动、查询、导出、常见问题。离线可看，无需联网。</p>
</header>
{toc_html}
<article>
{body}
</article>
<footer class="doc-foot">本页由 tools/build_usage_html.py 从 使用说明.md 自动生成，请勿直接编辑 HTML。</footer>
</div>
</body>
</html>
"""
    TARGET.write_text(document, encoding="utf-8")
    return TARGET


if __name__ == "__main__":
    path = build()
    print(f"generated {path} ({path.stat().st_size} bytes)")
