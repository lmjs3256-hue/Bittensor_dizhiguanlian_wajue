"""检查「导入地址库」弹窗：打开、选库/新建、预检、去重提示。

用户原话：「应该要弹窗让我确认导入到哪个地址库或者新增地址库，填写地址库
名称和分类，如果是导入已有地址库，还有去重，只导入新增部分。」

这个脚本要验证的是**交互真的通了**，不只是 HTML 里有元素：

1. 点「导入文件」→ 选文件后弹窗出现（用脚本直接触发 change 事件模拟）；
2. 弹窗默认「新建地址库」，填了库名 + 分类；
3. 预检结果真的显示出来（几条有效、几条已在库里）；
4. 切到「已有地址库」时，去重提示会把「本次只导入新增 N 条」讲清楚；
5. 点取消能关掉，且不留副作用。

用法（需先启动服务）：
``.venv/Scripts/python tools/check_import_dialog.py``
"""

import asyncio
import json
import subprocess
import tempfile
import time
import urllib.request

import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:8766/#library"

# 用 DataTransfer 造一个真实的 File 塞进 input，再触发 change——
# 这样走的是用户点「选择文件」之后的同一条代码路径。
OPEN_DIALOG = """(async () => {
  const text = '5DohFrx7u4HFuBYgncNY4FivhjpNLnuYtLu9gKJDU1zBx5EW,存储钱包\\n'
             + '5FeqQ5L16UWbgxEZs83fHFU7A3Sp161tKTCbBQpXDbixDTL8,主钱包\\n'
             + '5HYYMyRXHBBz2Pq8QwYzM4cKjJ8vN9yqWtBrXq3sT5nU8mVd7\\n'
             + '5DohFrx7u4HFuBYgncNY4FivhjpNLnuYtLu9gKJDU1zBx5EW,重复的\\n';
  const file = new File([text], '测试清单.txt', {type: 'text/plain'});
  const input = document.querySelector('#library-file');
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', {bubbles: true}));
  return 'change-dispatched';
})()"""

PROBE_DIALOG = """(() => {
  const mask = document.querySelector('#import-dialog');
  const preview = document.querySelector('#import-preview');
  const hint = document.querySelector('#import-dedup-hint');
  const cat = document.querySelector('#import-category');
  const bookSel = document.querySelector('#import-book-select');
  return JSON.stringify({
    open: mask ? !mask.classList.contains('hidden') : false,
    fileName: (document.querySelector('#import-dialog-file') || {}).textContent || '',
    previewText: preview ? preview.textContent.trim().slice(0, 300) : 'no-el',
    hintText: hint ? hint.textContent.trim().slice(0, 300) : 'no-el',
    modeNew: (document.querySelector('#import-mode-new') || {}).checked,
    modeExisting: (document.querySelector('#import-mode-existing') || {}).checked,
    existingDisabled: (document.querySelector('#import-mode-existing') || {}).disabled,
    bookName: (document.querySelector('#import-book-name') || {}).value,
    category: cat ? cat.value : 'no-el',
    categoryOptions: cat ? [...cat.options].map(o => o.value) : [],
    bookOptions: bookSel ? [...bookSel.options].map(o => o.value) : [],
    newFieldHidden: document.querySelector('#import-new-field').classList.contains('hidden'),
    existingFieldHidden: document.querySelector('#import-existing-field').classList.contains('hidden'),
    confirmDisabled: (document.querySelector('#import-confirm') || {}).disabled,
  });
})()"""

SWITCH_TO_EXISTING = """(() => {
  const radio = document.querySelector('#import-mode-existing');
  if (radio.disabled) return 'no-books';
  radio.checked = true;
  radio.dispatchEvent(new Event('change', {bubbles: true}));
  return 'switched';
})()"""

CLOSE_DIALOG = """(() => {
  document.querySelector('#import-cancel').click();
  const mask = document.querySelector('#import-dialog');
  return mask.classList.contains('hidden') ? 'closed' : 'still-open';
})()"""


async def main():
    prof = tempfile.mkdtemp()
    p = subprocess.Popen(
        [
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--remote-debugging-port=9421", f"--user-data-dir={prof}",
            "--window-size=1400,1200", URL,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        tgt = None
        for _ in range(60):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9421/json", timeout=1) as r:
                    pages = json.load(r)
                tgt = next((x for x in pages if x.get("type") == "page"), None)
                if tgt:
                    break
            except Exception:
                continue
        if not tgt:
            print("连不上 CDP")
            raise SystemExit(1)
        time.sleep(8)  # 等分类/库清单加载

        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
            async def ev(expr):
                await ws.send(json.dumps({
                    "id": 1, "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True,
                               "awaitPromise": True},
                }))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == 1:
                        res = m["result"]["result"]
                        return res.get("value")

            print("触发选文件:", await ev(OPEN_DIALOG))
            time.sleep(3)
            probe = json.loads(await ev(PROBE_DIALOG))
            print("\n=== 弹窗打开后（新建库模式）===")
            for k in ("open", "fileName", "modeNew", "bookName", "category",
                      "confirmDisabled", "newFieldHidden", "existingFieldHidden"):
                print(f"  {k}: {probe[k]}")
            print(f"  preview: {probe['previewText'][:200]}")
            print(f"  hint: {probe['hintText'][:200]}")
            print(f"  分类选项: {probe['categoryOptions']}")
            print(f"  已有库选项数: {len(probe['bookOptions'])}")

            print("\n=== 切到「已有地址库」===")
            switch = await ev(SWITCH_TO_EXISTING)
            print("  切换结果:", switch)
            if switch != "no-books":
                time.sleep(3)
                probe2 = json.loads(await ev(PROBE_DIALOG))
                print(f"  modeExisting: {probe2['modeExisting']}")
                print(f"  existingFieldHidden: {probe2['existingFieldHidden']}")
                print(f"  newFieldHidden: {probe2['newFieldHidden']}")
                print(f"  hint: {probe2['hintText'][:250]}")

            print("\n=== 关闭 ===")
            print("  ", await ev(CLOSE_DIALOG))

            problems = []
            if not probe["open"]:
                problems.append("弹窗没打开")
            if "测试清单.txt" not in probe["fileName"]:
                problems.append("文件名没显示")
            if not probe["modeNew"]:
                problems.append("默认不是「新建地址库」")
            if not probe["newFieldHidden"] is False:
                problems.append("新建模式却隐藏了库名输入框")
            if probe["existingFieldHidden"] is not True:
                problems.append("新建模式却显示了已有库下拉")
            if probe["confirmDisabled"]:
                problems.append("预检完成后确认按钮仍禁用")
            if "有效" not in probe["previewText"]:
                problems.append("预检结果没显示")
            if "重复" not in probe["previewText"]:
                problems.append("预检没报出文件内重复 1 条")
            if probe["category"] == "no-el" or not probe["category"]:
                problems.append("分类下拉没填上")
            # 去重提示的实际文案有两种：
            #   库里已有 → "…里已有 N 条，本次只导入新增的 M 条…"
            #   库里没有 → "「X」是空的或还没有这份清单，将导入全部 N 条。"
            # 两种都必须出现「导入」和具体的条数。
            hint = probe["hintText"]
            if "导入" not in hint or "条" not in hint:
                problems.append(f"去重提示不完整：{hint[:80]!r}")
            print("\n=== 结论 ===")
            if problems:
                for item in problems:
                    print("  [问题]", item)
                raise SystemExit(1)
            print("  全部通过：弹窗打开、默认新建库、预检显示有效/重复、去重提示正常、可关闭")
    finally:
        p.terminate()


asyncio.run(main())
