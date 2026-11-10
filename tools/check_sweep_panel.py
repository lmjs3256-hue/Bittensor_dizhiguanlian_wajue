import asyncio
import json
import subprocess
import tempfile
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
import websockets

URL = "http://127.0.0.1:8766/#library"

PROBE = """(() => {
  const disp = (s) => { const el = document.querySelector(s); return el ? getComputedStyle(el).display : 'no-el'; };
  const dot = document.querySelector('#sweep-dot');
  const meta = document.querySelector('#sweep-meta');
  const hint = document.querySelector('#sweep-hint');
  return JSON.stringify({
    switchExists: Boolean(document.querySelector('#watch-sweep')),
    switchChecked: document.querySelector('#watch-sweep') ? document.querySelector('#watch-sweep').checked : null,
    panelDisplay: disp('#sweep-panel'),
    dotClass: dot ? dot.className : 'no-el',
    title: (document.querySelector('#sweep-title') || {}).textContent,
    rate: (document.querySelector('#sweep-rate') || {}).textContent,
    meta: meta ? meta.textContent.trim().slice(0, 160) : 'no-el',
    hint: hint ? hint.textContent.trim().slice(0, 200) : 'no-el',
    progressHasSweep: (document.querySelector('#watch-progress-meta') || {}).textContent || '',
  });
})()"""

TOGGLE_OFF = """(() => {
  const box = document.querySelector('#watch-sweep');
  box.checked = false;
  document.querySelector('#watch-save').click();
  return 'saved-off';
})()"""


async def main():
    prof = tempfile.mkdtemp()
    p = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--remote-debugging-port=9401", f"--user-data-dir={prof}",
         "--window-size=1400,1400", URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        tgt = None
        for _ in range(60):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9401/json", timeout=1) as r:
                    pages = json.load(r)
                tgt = next((x for x in pages if x.get("type") == "page"), None)
                if tgt:
                    break
            except Exception:
                continue
        time.sleep(10)

        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
            async def ev(expr, await_promise=False):
                await ws.send(json.dumps({
                    "id": 1, "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
                }))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == 1:
                        return m["result"]["result"].get("value")

            # 等巡检面板加载出来
            for _ in range(10):
                v = await ev(PROBE)
                if v and json.loads(v)["panelDisplay"] != "none":
                    break
                time.sleep(2)
            print("开启时:", v)

            print("关掉:", await ev(TOGGLE_OFF))
            time.sleep(8)
            print("关闭后:", await ev(PROBE))

            # 恢复开启，别把用户设置留在关闭状态
            await ev("""(() => { const b=document.querySelector('#watch-sweep'); b.checked=true;
                        document.querySelector('#watch-save').click(); return 'restored'; })()""")
            time.sleep(6)
            print("已恢复开启")
    finally:
        p.terminate()
        time.sleep(1)


asyncio.run(main())
