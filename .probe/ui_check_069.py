"""0.6.8 界面验证：注入模拟关联结果，截图路径明细展开效果。"""
import asyncio
import json
import subprocess
import time
import urllib.request
import base64

import websockets

CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"
PORT = 9333
BASE = "http://127.0.0.1:8766"

FAKE = {
    "related": [
        {
            "address": "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
            "depth": 2,
            "balance_tao": "1250.5",
            "incoming_tao": "302.5",
            "outgoing_tao": "102.5",
            "total_tao": "405",
            "address_role": "",
            "role_reason": "",
            "match": True,
            "library_label": "疑似实体A",
            "library_category": "其他",
            "library_note": "",
            "path_transfers": [
                {"tx_hash": "0xabc1", "block_number": 9433000, "from_address": "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt", "to_address": "5FcBSpfg6nkksP6BJXrPKDAn5Y6arXY3YWnsYtDWFY5fX2vC", "amount_tao": "200", "timestamp": "2026-10-28T09:15:00Z", "edge_type": "tao_transfer", "netuid": None},
                {"tx_hash": "0xabc2", "block_number": 9435000, "from_address": "5FcBSpfg6nkksP6BJXrPKDAn5Y6arXY3YWnsYtDWFY5fX2vC", "to_address": "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg", "amount_tao": "102.5", "timestamp": "2026-10-30T21:40:00Z", "edge_type": "tao_transfer", "netuid": None},
            ],
        },
        {
            "address": "5FcBSpfg6nkksP6BJXrPKDAn5Y6arXY3YWnsYtDWFY5fX2vC",
            "depth": 1,
            "balance_tao": "880",
            "incoming_tao": "200",
            "outgoing_tao": "102.5",
            "total_tao": "302.5",
            "address_role": "",
            "role_reason": "",
            "match": False,
            "library_label": "",
            "library_category": "",
            "library_note": "",
            "path_transfers": [
                {"tx_hash": "0xabc1", "block_number": 9433000, "from_address": "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt", "to_address": "5FcBSpfg6nkksP6BJXrPKDAn5Y6arXY3YWnsYtDWFY5fX2vC", "amount_tao": "200", "timestamp": "2026-10-28T09:15:00Z", "edge_type": "tao_transfer", "netuid": None},
            ],
        },
    ],
    "warnings": [],
}


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
                with open(path, "wb") as fh:
                    fh.write(base64.b64decode(msg["result"]["data"]))
                return


INJECT = """
(() => {
  const data = """ + json.dumps(FAKE) + """;
  document.getElementById('empty').classList.add('hidden');
  const result = document.getElementById('result');
  result.classList.remove('hidden');
  document.getElementById('result-subtitle').textContent = '界面验证用模拟数据';
  state.lastResult = data;
  state.depthFilter = 'all';
  refreshRelatedTable();
  document.getElementById('related').scrollIntoView({block: 'start'});
})()
"""

CLICK_PATH = """
(() => {
  document.querySelector('[data-remove-relation]').click();
  document.getElementById('related').scrollIntoView({block: 'start'});
})()
"""


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
        await shot(ws, ".probe/12_remove_refresh.png", INJECT)
        await shot(ws, ".probe/13_remove_clicked.png", CLICK_PATH)
    finally:
        chrome.kill()


asyncio.run(main())
print("done")
