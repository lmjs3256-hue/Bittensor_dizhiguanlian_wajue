# -*- coding: utf-8 -*-
"""0.6.11 界面验证：地址库清单分组展开（含重渲染后再展开）。"""
import asyncio
import base64
import json
import subprocess
import time
import urllib.request

PORT = 8766
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
URL = f"http://127.0.0.1:{PORT}/"


def find_chrome():
    for path in CHROME_CANDIDATES:
        try:
            open(path, "rb").close(  # noqa: B015
            )
        except OSError:
            continue
        return path
    raise RuntimeError("chrome not found")


def new_tab(url):
    data = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/new?{url}").read()
    return json.loads(data)["webSocketDebuggerUrl"]


async def cdp(ws_url, method, **params):
    import websockets

    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == 1:
                return message.get("result", {})


async def shot(ws_url, path, expr):
    await cdp(ws_url, "Runtime.evaluate", expression=expr, returnByValue=True)
    await asyncio.sleep(0.5)
    data = await cdp(ws_url, "Page.captureScreenshot", format="png")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(data["data"]))


MOCK = """
(() => {
  // 切到地址库页面
  document.querySelector('[data-nav="library"]').click();
  // 注入两组模拟数据并渲染
  state.library = [
    {id: 1, address: "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt", label: "A1", category: "PERSONAL", category_label: "其他", note: "", book: "测试库一", source: "查询同步"},
    {id: 2, address: "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg", label: "A2", category: "PERSONAL", category_label: "其他", note: "", book: "测试库一", source: "查询同步"},
    {id: 3, address: "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN", label: "B1", category: "FRIEND", category_label: "群友", note: "", book: "", source: "手工"},
  ];
  state.books = [{name: "测试库一", count: 2, categories: {"PERSONAL": 2}, created_at: "2026-11-05T08:00:00", seed: ""}];
  renderBooks();
  renderBookFilter();
  renderLibraryList();
  return document.querySelectorAll("[data-library-toggle]").length;
})()
"""

CLICK_TOGGLE = """
(() => {
  const btn = document.querySelector('[data-library-toggle]');
  btn.scrollIntoView({block: 'center'});
  btn.click();
  const details = document.querySelector('.library-group-details');
  return details.classList.contains('hidden') ? 'STILL_HIDDEN' : 'EXPANDED';
})()
"""

RERENDER_THEN_TOGGLE = """
(() => {
  // 模拟用户切换筛选（触发重渲染），再点展开
  selectBook("测试库一");
  const btn = document.querySelector('[data-library-toggle]');
  btn.click();
  const details = document.querySelector('.library-group-details');
  return details.classList.contains('hidden') ? 'STILL_HIDDEN' : 'EXPANDED';
})()
"""


async def main():
    chrome = find_chrome()
    profile = r"E:\Bittensor_dizhiwajue\Bittensor_dizhiguanlian_wajue\.probe\profile"
    proc = subprocess.Popen([
        chrome, "--headless=new", "--remote-debugging-port=9333",
        f"--user-data-dir={profile}", "--no-first-run", "--window-size=1440,1000",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen("http://127.0.0.1:9333/json/version")
                break
            except OSError:
                time.sleep(0.25)
        req = urllib.request.Request(f"http://127.0.0.1:9333/json/new?{URL}", method="PUT")
        urllib.request.urlopen(req)
        tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
        page = next(t for t in tabs if t["type"] == "page")
        ws_url = page["webSocketDebuggerUrl"]
        await asyncio.sleep(1.5)

        result = await cdp(ws_url, "Runtime.evaluate", expression=MOCK, returnByValue=True)
        print("groups:", result.get("result", {}).get("value"))

        result = await cdp(ws_url, "Runtime.evaluate", expression=CLICK_TOGGLE, returnByValue=True)
        print("first toggle:", result.get("result", {}).get("value"))
        await shot(ws_url, r".probe\14_library_expanded.png", "void 0")

        result = await cdp(ws_url, "Runtime.evaluate", expression=RERENDER_THEN_TOGGLE, returnByValue=True)
        print("after rerender toggle:", result.get("result", {}).get("value"))
        await shot(ws_url, r".probe\15_library_rerender_expanded.png", "void 0")
    finally:
        proc.kill()


asyncio.run(main())
