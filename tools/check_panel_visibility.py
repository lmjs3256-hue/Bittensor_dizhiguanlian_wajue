import asyncio, json, subprocess, tempfile, time, urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
import websockets

TUNNEL = "https://necessary-com-rainbow-charts.trycloudflare.com"
LOCAL = "http://127.0.0.1:8766"

PROBE = """(() => {
  const disp = (s) => {
    const el = document.querySelector(s);
    if (!el) return 'no-el';
    return getComputedStyle(el).display;
  };
  const gate = document.querySelector('#access-gate');
  return {
    hero: disp('#hero-panel'),
    keyPanel: disp('.api-key-panel'),
    gateShown: gate ? getComputedStyle(gate).display !== 'none' : 'no-el',
    locked: document.body.classList.contains('access-locked'),
    heroRows: document.querySelectorAll('#access-list .access-row').length,
    heroText: (document.querySelector('#access-list') || {}).textContent ? document.querySelector('#access-list').textContent.trim().slice(0, 60) : '',
  };
})()"""


async def check(url, label):
    prof = tempfile.mkdtemp()
    p = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--remote-debugging-port=9382", f"--user-data-dir={prof}",
         "--window-size=1400,1200", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        tgt = None
        for _ in range(60):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9382/json", timeout=1) as r:
                    pages = json.load(r)
                tgt = next((x for x in pages if x.get("type") == "page"), None)
                if tgt:
                    break
            except Exception:
                continue
        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
            async def ev(expr):
                await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "returnByValue": True}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == 1:
                        return m["result"]["result"].get("value")

            last = None
            for _ in range(12):
                time.sleep(3)
                last = await ev(PROBE)
                # 等到 access 状态应用完：面板要么隐藏、要么出现口令框
                if last and (last["hero"] == "none" or last["gateShown"]):
                    break
        print(label, json.dumps(last, ensure_ascii=False))
    finally:
        p.terminate()
        time.sleep(1)


async def main():
    await check(LOCAL, "本机 :")
    await check(TUNNEL, "远程 :")


asyncio.run(main())
