"""用 CDP 量取巡检进度窗口的真实几何，验证布局没有被挤压或溢出。

不依赖 vision：直接读 DOM 的 getBoundingClientRect，比截图目测更可靠。
用法：python tools/check_watch_layout.py [url]
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766/#library"

PROBE = r"""
(() => {
  const q = (sel) => document.querySelector(sel);
  const box = (sel) => {
    const el = q(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      display: cs.display, overflowX: cs.overflowX,
      scrollW: el.scrollWidth, clientW: el.clientWidth,
      text: (el.textContent || "").trim().slice(0, 120),
    };
  };
  const panels = [...document.querySelectorAll(".watch-progress-meta span")].map((s) => {
    const r = s.getBoundingClientRect();
    return { text: s.textContent.trim().slice(0, 40), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) };
  });
  // 按 y 分行，检测同一行内是否横向重叠
  const rows = {};
  for (const p of panels) {
    const key = Math.round(p.y / 4);
    (rows[key] ||= []).push(p);
  }
  const overlaps = [];
  for (const list of Object.values(rows)) {
    list.sort((a, b) => a.x - b.x);
    for (let i = 1; i < list.length; i += 1) {
      if (list[i].x < list[i - 1].x + list[i - 1].w) {
        overlaps.push([list[i - 1].text, list[i].text]);
      }
    }
  }
  return {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    panel: box("#watch-progress"),
    head: box(".watch-progress-head"),
    track: box(".watch-progress-track"),
    bar: box("#watch-progress-bar"),
    meta: box("#watch-progress-meta"),
    hint: box("#watch-progress-hint"),
    stats: panels,
    overlaps,
  };
})()
"""


async def main() -> int:
    import websockets

    profile = tempfile.mkdtemp(prefix="watch-cdp-")
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--remote-debugging-port=9333",
            f"--user-data-dir={profile}",
            "--window-size=1400,1600",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(40):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9333/json", timeout=1) as res:
                    pages = json.load(res)
                target = next((p for p in pages if p.get("type") == "page"), None)
                if target:
                    break
            except Exception:
                continue
        if not target:
            print("无法连上 CDP")
            return 1
        time.sleep(6)  # 等前端拉完 /api/watch/status
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": PROBE, "returnByValue": True}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    break
        result = msg["result"]["result"]["value"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        problems = []
        if not result.get("panel"):
            problems.append("进度窗口不存在（可能还是 hidden）")
        else:
            if result["panel"]["h"] < 60:
                problems.append(f"进度窗口高度只有 {result['panel']['h']}px，疑似被压扁")
            if result["panel"]["scrollW"] > result["panel"]["clientW"] + 2:
                problems.append("进度窗口内容横向溢出")
        if result.get("overlaps"):
            problems.append(f"统计项横向重叠：{result['overlaps']}")
        if result.get("track") and result["track"]["h"] < 4:
            problems.append("进度条轨道高度异常")
        print("\n=== 结论 ===")
        print("通过" if not problems else "问题：" + "；".join(problems))
        return 0 if not problems else 2
    finally:
        proc.terminate()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
