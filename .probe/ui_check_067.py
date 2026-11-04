"""0.6.7 界面验证：截图 API key 折叠态 + 强制显示红色进度条。"""
import asyncio
import json
import subprocess
import time
import urllib.request

import websockets

CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"
PORT = 9333
BASE = "http://127.0.0.1:8766"


def new_tab(url):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
    data = urllib.request.urlopen(req).read()
    return json.loads(data)["webSocketDebuggerUrl"]


async def cdp(ws_url, method, **params):
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {})


async def shot(ws_url, path, expr):
    await cdp(ws_url, "Runtime.evaluate", expression=expr, returnByValue=True)
    await asyncio.sleep(0.6)
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                import base64
                with open(path, "wb") as fh:
                    fh.write(base64.b64decode(msg["result"]["data"]))
                return


async def main():
    chrome = subprocess.Popen([
        CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
        "--user-data-dir=.probe/profile", "--window-size=1440,1100",
        "--hide-scrollbars", "about:blank",
    ])
    time.sleep(3)
    try:
        ws = new_tab(BASE + "/")
        time.sleep(1.5)
        await cdp(ws, "Page.enable")
        await shot(ws, ".probe/07_api_collapsed.png",
                   "document.getElementById('api-key-body') ? 1 : 0")
        # 强制显示进度条并填入示例文案（仅截图验证用，不落库）
        await shot(ws, ".probe/08_progress_red.png", """
(() => {
  const box = document.getElementById('job-progress');
  box.classList.remove('hidden');
  document.getElementById('job-text').textContent =
    '正在执行第 2/5 轮 · 累计请求 7 次 · 剩余 3 个请求（约 1 轮）· 已发现 18 个地址';
  document.getElementById('job-plan').textContent = '本轮已查 4 个地址 · 下一轮约 45 秒后';
  box.scrollIntoView({block: 'center'});
})()
""")
        # 折叠交互验证：点「填写 / 修改」展开
        await shot(ws, ".probe/09_api_expanded.png",
                   "document.getElementById('toggle-api-key').click(); 1")
    finally:
        chrome.kill()


asyncio.run(main())
print("done")
