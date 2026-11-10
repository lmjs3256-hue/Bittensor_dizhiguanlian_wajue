import asyncio, json, subprocess, tempfile, time, urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
import websockets

SEED = "5HinyU1VsBUu51mSdeWCm8FUkuVGXCJMQj2QMpTSYtoqW1fn"
PUB = "5F55a6mSNftUoETdw8JZqCbz22fzCfetrf4vFybmyKsrgp3n"
TGT = "5HCT4AarReToT1BKyLtJXJfSLs4zRS7dENnZ7iysqrqxXyV7"

OPEN_HISTORY = """(() => { location.hash = '#history'; return location.hash; })()"""

LOAD_LATEST = """(() => {
  const b = document.querySelector('[data-history-view]');
  if (!b) return 'no-button';
  b.click();
  return 'clicked';
})()"""

PROBE = """(() => {
  const out = {rows: 0, terminals: 0, sample: null, prunedByRole: null, toast: null};
  if (!state.lastResult) return {...out, err: 'no result'};
  out.rows = (state.lastResult.related || []).length;
  const t = localTerminalRoles();
  out.terminals = t.size;
  // 纯函数验证：造一条"穿过公共地址"的假行
  out.sample = passesThroughTerminal(
    {address: 'FAKE_TARGET', path_transfers: [
      {path_from: 'FAKE_SEED', path_to: 'MIDDLE'},
      {path_from: 'MIDDLE', path_to: %s},
      {path_from: %s, path_to: 'FAKE_TARGET'},
    ]},
    t, 'FAKE_SEED',
  );
  // 纯函数验证：终点自己不能被误删
  out.selfKept = !passesThroughTerminal(
    {address: %s, path_transfers: [
      {path_from: 'FAKE_SEED', path_to: %s},
    ]},
    t, 'FAKE_SEED',
  );
  return out;
})()""" % (json.dumps(PUB), json.dumps(PUB), json.dumps(PUB), json.dumps(PUB))

CONFIRM_PROBE = """(() => {
  const saved = window.confirm;
  let captured = null;
  window.confirm = (msg) => { captured = msg; return false; };
  const res = confirmRoleSwitch(%s, 'EXCHANGE');
  window.confirm = saved;
  return {returned: res, captured};
})()""" % json.dumps(PUB)


async def main():
    prof = tempfile.mkdtemp()
    p = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--remote-debugging-port=9370", f"--user-data-dir={prof}",
         "--window-size=1440,1800", "http://127.0.0.1:8766/#history"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        tgt = None
        for _ in range(40):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9370/json", timeout=1) as r:
                    pages = json.load(r)
                tgt = next((x for x in pages if x.get("type") == "page"), None)
                if tgt:
                    break
            except Exception:
                continue
        time.sleep(8)

        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
            async def ev(expr):
                await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "returnByValue": True}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == 1:
                        return m["result"]["result"].get("value")

            print("open:", await ev(OPEN_HISTORY))
            time.sleep(2)
            print("load:", await ev(LOAD_LATEST))
            for _ in range(15):
                time.sleep(2)
                v = await ev(PROBE)
                if v and v.get("rows"):
                    break
            print("probe:", json.dumps(v, ensure_ascii=False))
            print("confirm:", json.dumps(await ev(CONFIRM_PROBE), ensure_ascii=False))
    finally:
        p.terminate()
        time.sleep(1)


asyncio.run(main())
