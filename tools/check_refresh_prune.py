import asyncio, json, subprocess, tempfile, time, urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
import websockets

PUB = "5F55a6mSNftUoETdw8JZqCbz22fzCfetrf4vFybmyKsrgp3n"

LOAD = """(() => {
  const b = document.querySelector('[data-history-view]');
  if (!b) return 'no-button';
  b.click();
  return 'clicked';
})()"""

# 往当前结果里注入一条"穿过公共地址"的假行（只改内存，不落库、不发请求）
INJECT = """(() => {
  const data = state.lastResult;
  if (!data) return 'no-result';
  const before = (data.related || []).length;
  // 同时补上 edges，否则假行不可达、会被"连通性"那条规则剔掉，
  // 测不到"穿过终点"这条新规则。
  data.edges = [...(data.edges || []),
    {from_address: data.seed, to_address: 'MIDDLE'},
    {from_address: 'MIDDLE', to_address: "5F55a6mSNftUoETdw8JZqCbz22fzCfetrf4vFybmyKsrgp3n"},
    {from_address: "5F55a6mSNftUoETdw8JZqCbz22fzCfetrf4vFybmyKsrgp3n", to_address: 'FAKE_THROUGH_TERMINAL'},
  ];
  data.related = [...(data.related || []), {
    address: 'FAKE_THROUGH_TERMINAL',
    depth: 3,
    amount_tao: '1',
    tx_count: 1,
    stake_count: 0,
    strength: 1,
    library_category: '',
    library_categories: [],
    path_transfers: [
      {path_from: data.seed, path_to: 'MIDDLE'},
      {path_from: 'MIDDLE', path_to: %s},
      {path_from: %s, path_to: 'FAKE_THROUGH_TERMINAL'},
    ],
  }];
  renderResult(state.lastResult);
  return {before, after: state.lastResult.related.length};
})()""" % (json.dumps(PUB), json.dumps(PUB))

CLICK_REFRESH = """(() => {
  const b = document.querySelector('#refresh-result');
  if (!b) return 'no-button';
  b.click();
  return 'clicked';
})()"""

AFTER = """(() => ({
  rows: (state.lastResult?.related || []).length,
  hasFake: (state.lastResult?.related || []).some((r) => r.address === 'FAKE_THROUGH_TERMINAL'),
  toast: (document.querySelector('#toast') || {}).textContent || '',
}))()"""


async def main():
    prof = tempfile.mkdtemp()
    p = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--remote-debugging-port=9371", f"--user-data-dir={prof}",
         "--window-size=1440,1800", "http://127.0.0.1:8766/#history"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        tgt = None
        for _ in range(40):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9371/json", timeout=1) as r:
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

            print("load:", await ev(LOAD))
            for _ in range(15):
                time.sleep(2)
                n = await ev("(state.lastResult?.related || []).length")
                if n:
                    break
            print("rows before inject:", n)
            print("inject:", json.dumps(await ev(INJECT), ensure_ascii=False))
            print("refresh:", await ev(CLICK_REFRESH))
            time.sleep(2)
            print("after:", json.dumps(await ev(AFTER), ensure_ascii=False))
    finally:
        p.terminate()
        time.sleep(1)


asyncio.run(main())
