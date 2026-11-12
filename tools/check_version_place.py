"""检查「版本号在侧栏底部」的实际几何位置。

用户要求：把版本号从页脚移到左侧栏的底部。

只看 HTML 里有 `aside-version` 不算数——要确认：

1. 它确实在 `.sidebar` 内部（不是被挪到 main 里）；
2. 它在侧栏**垂直接近底部**（底边与侧栏底边差距小于 40px）；
3. 它排在 `.aside-status`（连接状态）**下方**；
4. 页脚里不再有版本号（`footer-release-link` 与主内容区里的 `#app-version`
   都不存在重复项）；
5. 桌面端可见、移动端（侧栏变横条）隐藏，不占地方、不横向溢出。

用法：先启动服务（默认 127.0.0.1:8766），再
``.venv/Scripts/python tools/check_version_place.py``
"""

import asyncio
import json
import subprocess
import tempfile
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:8766/"

import websockets


PROBE = """(() => {
  const ver = document.querySelector('#app-version');
  const box = document.querySelector('.aside-version');
  const side = document.querySelector('.sidebar');
  const status = document.querySelector('.aside-status');
  const footer = document.querySelector('.app-footer');
  if (!ver || !box || !side) {
    return JSON.stringify({ error: 'missing element' });
  }
  const r = box.getBoundingClientRect();
  const s = side.getBoundingClientRect();
  const st = status ? status.getBoundingClientRect() : null;
  const cs = getComputedStyle(box);
  const vcs = getComputedStyle(ver);
  return JSON.stringify({
    text: (ver.textContent || '').trim(),
    visible: cs.display !== 'none' && r.height > 0,
    display: cs.display,
    // 是否在侧栏内部
    insideSidebar: side.contains(box),
    // 底边距侧栏底边多少像素（越小越贴底）
    gapToSidebarBottom: Math.round(s.bottom - r.bottom),
    // 与连接状态相比是否更靠下
    belowStatus: st ? Math.round(r.top - st.bottom) : null,
    // 是否溢出侧栏宽度
    overflowX: Math.round(r.right - s.right),
    fontSize: vcs.fontSize,
    color: vcs.color,
    mono: vcs.fontFamily.includes('mono') || vcs.fontFamily.includes('Consolas') || vcs.fontFamily.includes('monospace'),
    // 页脚里不该再有版本号
    footerHasVersion: Boolean(footer && footer.querySelector('#app-version')),
    // 整个文档里 #app-version 只能有一个
    versionCount: document.querySelectorAll('#app-version').length,
    // 更新日志入口是否还在（侧栏按钮）
    releaseButton: Boolean(box.querySelector('[data-nav="release"]')),
    docWidth: document.documentElement.scrollWidth,
    winWidth: window.innerWidth,
  });
})()"""


async def main():
    prof = tempfile.mkdtemp()

    async def run_once(port, width, label):
        p = subprocess.Popen(
            [
                CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                f"--remote-debugging-port={port}", f"--user-data-dir={prof}-{port}",
                f"--window-size={width},1200", URL,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            tgt = None
            for _ in range(60):
                time.sleep(0.25)
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json", timeout=1
                    ) as r:
                        pages = json.load(r)
                    tgt = next((x for x in pages if x.get("type") == "page"), None)
                    if tgt:
                        break
                except Exception:
                    continue
            if not tgt:
                print(f"{label}: 连不上 CDP")
                return None

            async with websockets.connect(
                tgt["webSocketDebuggerUrl"], max_size=None
            ) as ws:
                async def ev(expr):
                    await ws.send(json.dumps({
                        "id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True},
                    }))
                    while True:
                        m = json.loads(await ws.recv())
                        if m.get("id") == 1:
                            return m["result"]["result"].get("value")

                # 等 /api/version 之类把版本号填进去
                val = None
                for _ in range(12):
                    time.sleep(1)
                    val = await ev(PROBE)
                    if val and "error" not in val:
                        data = json.loads(val)
                        if data.get("text") and "读取中" not in data["text"]:
                            break
                print(f"\n=== {label} (宽度 {width}) ===")
                print(val)
                return json.loads(val) if val else None
        finally:
            p.terminate()
            time.sleep(1)

    desktop = await run_once(9411, 1400, "桌面端")
    mobile = await run_once(9412, 390, "移动端")

    problems = []
    if desktop:
        if not desktop["insideSidebar"]:
            problems.append("版本号不在侧栏内部")
        if desktop["gapToSidebarBottom"] > 40:
            problems.append(
                f"版本号离侧栏底边 {desktop['gapToSidebarBottom']}px，不算贴底"
            )
        if desktop["belowStatus"] is not None and desktop["belowStatus"] < 0:
            problems.append("版本号跑到了连接状态上面")
        if desktop["footerHasVersion"]:
            problems.append("页脚里还有版本号")
        if desktop["versionCount"] != 1:
            problems.append(f"#app-version 出现 {desktop['versionCount']} 次")
        if not desktop["releaseButton"]:
            problems.append("更新日志入口丢了")
        if desktop["overflowX"] > 0:
            problems.append("版本号横向溢出侧栏")
        if desktop["docWidth"] > desktop["winWidth"]:
            problems.append("页面出现横向滚动")
        if "读取中" in desktop["text"]:
            problems.append("版本号没填上")
    if mobile:
        if mobile["visible"]:
            problems.append("移动端仍显示版本号（应隐藏，侧栏是横条没空间）")
        if mobile["docWidth"] > mobile["winWidth"] + 1:
            problems.append("移动端出现横向滚动")

    print("\n=== 结论 ===")
    if problems:
        for item in problems:
            print("  [问题]", item)
        raise SystemExit(1)
    print("  全部通过：版本号在侧栏底部、页脚已移除、移动端隐藏、无横向溢出")
    print(f"  桌面端显示为 {desktop['text']!r}，距侧栏底边 {desktop['gapToSidebarBottom']}px")


asyncio.run(main())
