# 验证交付记录

## 0.8.2 验证记录（2026-11-07）

**改动**：路径明细重排为「每跳一行」；「收起明细」改为强调色实心按钮；深灰主题按钮对比度修正；日均频次天数改按北京时间算。

**问题定位（改前实测）**：1440 宽、内容区 1164px 时，明细表六列平均分配为
`跳数 582 / 方向 174 / 类型 92 / 金额 69 / 时间 159 / 区块 88`，「跳数」白占一半宽度，地址列只有 174px 导致两个 48 位地址折行，每跳行高 **62px**。

**改后实测（CDP 数值化核查，真实历史结果 47）**：

| 检查项 | 结果 |
| --- | --- |
| 列宽 | 跳 44 / 路径 632 / 流向类型 118 / 金额 104 / 时间 150 / 区块 88 |
| 每跳行高 | 30（表头）→ 34～35，2 跳 / 6 跳全部 `allOneLine=true` |
| 地址折行 | `wayTruncated=0`，路径列 612px 内容宽度够放头6尾6两个地址 |
| 横条溢出 | `pageOverflowX=false`（1440 与窄屏均无） |
| 收起按钮 | 78×30、`rgb(9,111,100)` 实心底、12px/750、对比度 6.05（亮）/ 8.39（深灰） |
| 展开标记 | 行内按钮实心 + 行 `.path-open`（左竖条 + 浅底），收起后 `rowText=路径明细`、`detailGone=true` |
| 底部收起 | ≥4 跳时出现，6 跳实测 `footBtn=收起明细`、高 27px |
| 外层关联表 | 9 列宽度未被明细表规则污染（min-width 1044px，首行高 114px 正常） |

**根因（下一任改这块别再踩）**：
1. 明细表是嵌在 `#related > table` 里的**第二张表**。原列宽规则 `#related > table th:nth-child(n)` 是后代选择器，会命中内层表头，把 9 列宽度套到 6 列上 —— 「跳」列被撑到 712px。已收窄为 `#related > table > thead > tr > th`。
2. `table-layout:fixed` 下 `<colgroup><col width>` 会被浏览器忽略（实测仍是 84/169/226/200/288/169）。列宽必须写在 `thead th` 上。
3. 深灰主题 `--accent` 是浅灰绿 #a9b5a6，按钮用 `color:#fff` 对比度只有 **2.13**。新增 `--on-accent`（亮/护眼=白、暗色/深灰=深色），四主题分别 6.05 / 8.39。

**测试**：`tests/test_path_detail_layout.py` 新增 4 条（列宽写 thead 不写 colgroup、外层列宽不外溢、按钮用 `--on-accent`、长路径两个收起入口）。全量 **268 passed**。

**顺带修的一个时区缺陷**：日均频次天数原用 `utcnow().date()`，在北京时间 00:00–08:00 之间比实际少一天（`test_profile_combines_account_and_transfer_total` 就是在这个时段暴露的）。已改为 `to_beijing(utcnow()).date()`，与界面/导出的北京日口径一致。

## 0.8.1 验证记录（2026-11-06）

**需求：质押解质押是不是只能本地索引？API 查要调几次？扫描开始优先查起始地址信息，查到先显示左栏，关联继续扫描，结束再汇总**

- **问题一：质押/解质押只能本地索引吗？** 不是。本地索引只有 `stake_transfer`（子网 stake 转移）边，**没有** `stake` / `unstake` 操作事件，数不出质押次数；真实次数必须走 Taostats `GET /v1/subnets/stake-events?coldkey=<地址>&action=all` 的 `pagination.total_items`。0.8.0 的实现只在 `data_source="taostats"` 时走 API，选本地索引会退回转账笔数——**这是个口径错误**，已改成不分数据源、只要配了 key 就走 API。
- **问题二：API 要调几次？** 画像固定 **2 次**：`/api/account/latest/v1`（注册日期 + 余额构成）1 次，`/v1/subnets/stake-events`（质押总量，`limit=1` 只读分页）1 次。都要 5 分钟缓存，命中则为 0 次。关联扫描本身另算（单轮 5 次、分轮每轮 5 次）。
- 新增接口 `POST /api/seed-profile`（`{seed, data_source}` → `{seed, seed_profile, api_requests, warnings}`），只查画像不跑关联；新增 `RelationInput.prefetched_requests` / `RelationJobInput.prefetched_requests`（0–10）。
- 预算扣减：`RelationContext.prefetched_requests` → `relations()` 的爬取预算改为 `5 - validator_requests - prefetched_requests`；分轮作业只在 `rounds_done == 0` 时扣（后续轮次已隔 62 秒）。
- **实测（真实 key，本机 5 次/分钟限速，地址 `5CQBKc8nXsmgDyR4L8daUY1Xq2evTNhLaNNJoAsp68hWjMQ4`）**：
  - `POST /api/seed-profile` → 2.6 秒返回，`api_requests=2`，注册 `2026-03-27 / finney`、质押 `9,468` 次、`active_days=224`、`daily_frequency=42.27`、余额 `37.453912447`（可用 16.514131557 + 质押 20.93978089），`warnings=[]`。
  - 紧接着 `POST /api/relations`（depth 2、`prefetched_requests=2`）→ `api_requests=3`（5−2，画像命中缓存 0 次）、`prefetched_requests=2`、`seed_profile_requests=0`、**15 个关联地址**、无 429。
  - 地址 `5CCo5n…TFX`：画像 `api_requests=2`、质押 `906` 次、日均 `1.5 次/天`、注册 `2025-03-11`。
- **时序实测（CDP，1440×1000，真实点击「扫描关联」，不 mock 前端）**：
  - 3.1 秒左栏出现并显示「查询中…」→ 5.5 秒填入真实数字（`906 次`）→ 11.9 秒关联结果才渲染。`sidebarFilledBeforeRelations = true`，画像比结果早 **6.4 秒**。
  - 请求体确认 `prefetched_requests: 2`、`data_source: "taostats"` 已实际传给 `/api/relations`。
- **口径修正（本轮发现并修掉）**：配了 key 但质押接口被限流时，旧逻辑会拿「本地索引转账 1 笔」填「质押 1 次」，日均算成 `0.0 次/天`，看起来像这个地址从没质押过。现在如实显示「未获取」+ 原因；只有完全没配 key 才退回本地转账笔数并标注口径，且此时不计算日均频次（两种口径不同）。
- 画像请求现在计入「Taostats 查询次数」统计（此前漏记）；结果新增 `seed_profile_requests`、`api_requests_total`，副标题显示「+ 起始地址画像 N 次」。
- **排版数值核查（CDP）**：
  - 宽屏三档 1440 / 1180 / 1080：`fitsSidebar=true`、`cardOverflowX=false`、`pageOverflowX=false`、`valuesAligned=true`（四行右边缘统一 `207px`）、`overlap=false`、行无溢出；字号 `dt=11px` / `value=13px` / `note=10px`；地址显示 `5CQB…jMQ4`，`dataset.full` 为完整 48 位。
  - 窄屏 950 / 900 / 760 / 560 / 480：`inSlot=true`、`hasInSlotClass=true`；卡片宽 903 → 433，高度在 ≤760px 由 131px 变 178px（降两列），全程 `cardOverflowX=false`、`pageOverflowX=false`、无重叠无溢出。
  - 探针踩坑记录：卡片搬到 `#seed-card-slot` 后若 `#result` 仍 hidden，量到的是 `0×0`、`overlap` 全真——必须先 `classList.remove('hidden')`；另外 `--user-data-dir` 必须给绝对路径，相对路径在 Git Bash 下会让 Chrome 起不来。
- 回归：**264 passed**（新增 `tests/test_seed_profile_endpoint.py` 8 条 + `tests/test_seed_profile.py` 5 条：接口四项指标、缓存时请求数 0、非法地址 422、无 key 不报错、预算扣减到 3 次、画像命中缓存不重复取、作业首轮扣减、payload 暴露 `prefetched_requests`、本地模式走 API、缓存命中不计请求数、失败不拿转账笔数顶替）。

## 0.8.0 验证记录（2026-11-06）

**需求：起始地址的「历史交易」「日均频次」不算转账笔数，改为统计子网质押买卖次数（质押与解质押都算）**

- 现状核查：原实现走 `get_transfer_page_query(limit=1)` 取 `pagination.total_items`，统计的是转账笔数，口径确实不对。
- 接口探测（真实 API key，按本地 5 次/分钟限速节奏）：
  - `action` 的合法值实测为 `stake` / `unstake` / `all`（传 `Stake`/`Unstake` 报 `unknown variant`）。
  - 地址 `5CCo5n…TFX`：`all=906`、`stake=475`、`unstake=431` → **475 + 431 = 906** 精确相等。
  - 地址 `5CQBKc…MQ4`：`all=9466`、`stake=5302`、`unstake=4164` → **5302 + 4164 = 9466** 精确相等。
  - 明细拉取 200 行确认：`action` 只有 `stake`/`unstake` 两种，`is_transfer` 全为 `False`；带 `is_transfer=true` 时 `total_items` 会变小（906 → 1），说明**不能带该参数**，否则会漏掉正常质押。
  - `limit=1` 与 `limit=200` 返回的 `total_items` 一致，一次请求即可拿到全量。
- 实现：新增 `taostats.get_stake_event_total(address)`，请求 `{coldkey, action: "all", page: 1, limit: 1}`，只读 `pagination.total_items`；独立缓存 `_stake_total_cache`（TTL 同 `CACHE_TTL_SECONDS`），并纳入 `reset_client_state_for_tests()`。
- 服务层：`_seed_profile` 改调 `get_stake_event_total`，新增 `stake_event_count` / `stake_event_scope`；`transfer_count` / `transfer_count_scope` 保留且同值，避免旧前端读到 `None`；warning 文案改为「质押操作次数未获取」。
- 真实端到端（`POST /api/relations`，seed `5CQBKc8nXsmgDyR4L8daUY1Xq2evTNhLaNNJoAsp68hWjMQ4`，depth 1）：`stake_event_count=9466`、`stake_event_scope=taostats`、注册 `2026-03-27 / finney`、`active_days=224`、`daily_frequency=42.26`（9466 ÷ 224 = 42.259…）、余额 `37.481755491`（可用 12.462866208 + 质押 25.018889283）。
- UI 三态（CDP，1440px）：正常态显示「质押次数 9,466 次 / 质押 + 解质押」「日均频次 42.26 次/天 / 按 224 天算」；本地索引态显示「67 次 / 本地索引无质押事件，改为转账笔数」；缺失态显示「未获取 / 需要 Taostats API key」。四行 `dtR=74`（余额行 52）右值统一 `bR=207`，`overlap=false`、`ovf=false`、`cardOv=false`、`pageOv=false`。
- 回归：**251 passed**（新增 `tests/test_seed_profile.py` 5 条：请求参数校验、缓存命中、429 限流、无 key 鉴权、画像汇总与日均）。

## 0.7.9 验证记录（2026-11-06）

**需求：起始地址画像（地址 / 注册日期 / 历史交易 / 日均频次 / 余额）移到左侧栏空白处，地址只显示头尾 4 位并加复制按钮**

- 结构：结果区原 `.seed-balance-card` 五列卡片删除，改为 `#seed-card-slot` 占位；画像块 `#side-seed` 插在左栏 `<nav>` 与 `.aside-status` 之间，默认 `hidden`，首次出结果时显示。
- 左栏实测（CDP，1440 / 1180 / 1080 三档）：卡片 `x=16, w=205`，侧栏 `w=238`，`fitsSidebar=true`、`cardOverflowX=false`、`pageOverflowX=false`；卡片位于状态行之上（`beforeStatus=true`）。
- 地址：`addrText="5Hiv…N8kS"`、`addrFull` 为完整 48 位、`addrFont=13px`、`addrTruncated=false`；复制按钮 `btnRight=207 ≤ cardRight=221`（不出框），点击后 `navigator.clipboard` 收到完整地址、按钮回显「已复制」。
- 四个指标渲染：注册日期 `2022-10-30` / 注册网络 finney、历史交易 `47,288 笔`（Taostats 全量）、日均频次 `32.21 笔/天`（按 1,468 天算）、余额 `119,310.99 TAO`（可用 210.50 · 质押 119,100.49）；四项高度统一 31px，均无横向溢出。
- 响应式搬家：900px 时卡片 `parent=seed-card-slot`、`in-slot=true`、网格 2 列、页面无横向溢出；回到 1440px 自动搬回侧栏（`inSidebar=true`、`in-slot=false`）。
- 深灰主题（`data-theme=darkgray`）：卡片底 `rgb(41,43,47)`、地址色 `rgb(169,181,166)`、数值 `rgb(226,226,222)`、标签 `rgb(169,170,171)`，对比度正常。
- 截图：`.probe/shots/side_seed_1440.png`；探针脚本 `.probe/ui_check_078_side.py`。
- 回归：`pytest -q` 全量通过（无后端改动，仅模板/样式/前端脚本）。

## 0.7.8 验证记录（2026-11-06）

**根因定位：`start.ps1` 在服务已运行时直接 `exit 0`，「升级后功能不生效」不是代码问题**

- 远端症状：`git pull` 到 0.7.7 后双击「启动.cmd」，页脚显示 0.7.7，但地址库里没有预置验证者、点「同步验证者地址」仍报旧错。
- 定位：`start.ps1` 第 51 行附近，探到同目录服务在跑就打印 `Service is already running at …` 并 `exit 0`。旧进程从未被替换，新代码一行没执行。
- 为什么看不出来：`/api/version` 读的是磁盘 `VERSION` 文件（`app/version_info.py` 的 `read_version_info()`），与运行中的进程无关。`git pull` 改了文件 → 页脚立刻显示新版本 → 用户以为升级成功。
- 结论：0.7.7 的 405 修复和预置名单写入逻辑本身没问题（本机 234 passed、`/api/library/import-validators` GET/POST 均 200），只是远端从没跑到那段代码。

**自动重启修复实测**

修复前运行态：`/api/health` = `0.7.7`（pid 19768），磁盘 `VERSION` 改为 `0.7.8`。

```
$ powershell -NoProfile -ExecutionPolicy Bypass -File start.ps1 -Port 8766
检测到代码已更新：运行中 0.7.7 → 磁盘 0.7.8
正在重启服务以加载新版本…
Ready: http://127.0.0.1:8766
```

重启后 `/api/health`：

```json
{"ok":true,"version":"0.7.8","pid":34680,
 "started_at":"2026-11-06T21:30:30+08:00",
 "validator_snapshot":1196,"validator_snapshot_mtime":"2026-11-06T20:43:23+08:00"}
```

启动日志（`logs/wallet-link.stderr.log`）：

```
[builtin] 版本 0.7.8 预置验证者 1196 条；本次启动新增 0 条、补标记 0 条
```

- 停止逻辑就地实现而非 `& stop.ps1`：后者含 `exit 0`，被调用会连带结束启动会话（试过，脚本在 `Test-PortFree` 等待段直接退出）。改为 pid 文件优先、端口兜底、只杀 `python*` 进程，再等端口释放（最多 20 秒）。

**环境自检（`/api/diagnose`）实测**

| 检查项 | 状态 | 输出 |
| --- | --- | --- |
| 代码版本 | pass | VERSION 文件与运行进程都是 0.7.8，pid 34680，启动于 2026-11-06 21:30:30 |
| 预置验证者文件 | pass | 1196 条，修改时间 2026-11-06T20:43:23+08:00 |
| 预置名单落库 | pass | 验证者分类 1195 条，其中「预置验证者」标签 1194 条；内置来源合计 6 条（曾从 Taostats 同步，保留官方来源，属正常） |
| 本次启动写入 | pass | 新增 0 条，补标记 0 条，格式无效跳过 0 条 |
| Taostats API key | pass | 已配置 |

- 落库判定口径：初版只看 `source LIKE 'builtin:%'`，本机只有 6 条（1194 个验证者来源是 `taostats:`，内置写入按规则不覆盖官方来源），会误报未通过。改为 `max(验证者分类数, 预置标签数)` 对文件条数算覆盖率，<90% 才判 fail。
- 命令行 `python run.py --doctor` 输出同上，退出码 0；空库时为 1。

**界面实测**（`.probe/ui_check_078.py` / `.probe/ui_check_078_fail.py`，1440）

| 场景 | 结果 |
| --- | --- |
| 正常态 | 5 项全 `pass`，摘要「全部通过 · 版本 0.7.8 · 预置验证者 1196 条（pid 34680）」，横幅隐藏，页脚「当前版本：0.7.8」 |
| 失败态（模拟 0.7.7 进程 + 空库） | 2 项 fail、1 项 warn，横幅可见且带「重启后再自检」按钮，页脚「运行中版本：0.7.7（磁盘已是 0.7.8，需重启）」+ `version-stale` 类 |
| 项内配色 | pass 底色白 / 左边框 `#096f64`；fail 底色 `#fee9e7` / 左边框 `#b42318`；warn 底色 `#fff2e2` / 左边框 `#a14f00`；正文 `#1c2532`、次要 `#6f7c8d` |
| 深灰主题 | fail 卡片底 `rgb(73,56,54)`、标记底 `rgb(43,44,47)`、标题字 `rgb(226,226,222)`，对比度正常 |
| 处理建议条数 | 3 条（每个非 pass 项各一条） |
| 对齐 / 溢出 | 1440 与 1080 两档，5 项左右边距均 24/24，零横向溢出（`scrollWidth ≤ innerWidth`） |

**回归**：`pytest -q` = **246 passed**（新增 `tests/test_diagnostics.py` 12 条：快照文件缺失 / 截断、空库、key 缺失仅提醒、启动写入展示、缓存 TTL、文本输出、`/api/diagnose`、`/api/health` 快照字段、版本失配判 fail、`/api/version` 的 `stale`）；页面资源戳 `?v=0.7.8`；`start.ps1` 语法经 `[Parser]::ParseFile` 校验通过。

## 0.7.7 验证记录（2026-11-06）

**远端 405 的复现与修复**：

| 请求 | 修复前 | 修复后 |
| --- | --- | --- |
| `POST /api/library/import-validators` | 200 | 200 |
| `GET /api/library/import-validators` | **405 Method Not Allowed** | 200 |
| `GET /api/library/not-a-real-action` | 405（被 `/api/library/{row_id}` 抢匹配） | **404** |

- 成因：`@app.delete("/api/library/{row_id}")` / `@app.patch(...)` 注册在 `import-validators` 之前，`{row_id}` 是字符串路径参数，`import-validators` 也能匹配上，方法不符就落到 405 分支。远端浏览器发出的是 GET（本地一直 POST），所以本地怎么点都正常。
- 前端 `syncValidatorLibrary()` 把 `error.message` 原样弹出，于是「Method Not Allowed」被显示成「验证者地址同步失败」。

**预置验证者快照**：

- `app/data/builtin_validators.txt`：1196 行地址（3 行注释），`load_validator_snapshot()` 读出 1196 条。
- `builtin_entries()` 去重后 **1201** 条（5 交易所 + 1196 验证者，其中 1 个验证者与人工名单重叠）。
- 在全新库上 `ensure_builtin_addresses()` 写入后，快照地址的 `category=VALIDATOR`、`source=builtin:validator`、`builtin_locked=true`。
- 现有库（1902 条）启动后：预置验证者分组 1194 条（本机原本已通过 Taostats 同步入过库，来源保留 `taostats:`，不重复插入）。

**地址库分页**（`.probe/check_pager.py`，1440）：

| 项 | 结果 |
| --- | --- |
| 组名 / 条数 | 预置验证者 / 1194 |
| 第 1 页 | 「第 1 / 24 页 · 共 1194 个地址，本页 50 个（每页 50 条）」 |
| 翻到第 2 页 | 页码变 2/24，首行地址从 `5C4hrfjx…` 变成 `5C4i761Q…`，组仍展开 |
| 末页 | 「第 24 / 24 页 · 共 1194 个地址，本页 44 个」，下一页按钮禁用 |

**结果地址单行**（`.probe/ui_check_077b.py` + `.probe/diag_overflow.py`）：

| 宽度 | 采样行数 | 折行行数 | 链接高度 / 行高 | 表格横向溢出 |
| --- | --- | --- | --- | --- |
| 1440 | 10 | **0** | 17 / 18 | 8（改动前同为 8） |
| 1180 | 10 | **0** | 17 / 18 | 253（表格固有宽度，原本可横向滚动） |
| 1024 | 10 | **0** | 17 / 18 | 399（同上） |

- 只对 `.related-address-cell .addr-link` 加 `white-space:nowrap`，没有对整行 `address-main` 加 `nowrap`：试过整行不换行，表格会被撑宽 137px（1181 vs 1044），因此保留按钮换行、只锁地址。

**回归**：`pytest -q` = **234 passed**；`/api/health` 版本 0.7.7（pid 19768）；页面资源戳 `?v=0.7.7`。

## 0.7.6 验证记录（2026-11-06）

**故障复现与定位**（`POST /api/library/import-validators`）：

- 修复前实测：`HTTP 502` → `Taostats 验证者名单请求失败：[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`。同进程再请求一次又成功，确认是**传输层偶发**而非接口变更。
- 用保存的 key 独立直连核对接口本身：`GET https://api.taostats.io/v1/validators/active`（不带参数）返回 `200`，`pagination.total_items=7439`、`data` 7439 行、去重后 **1196** 个地址；带 `network=finney` 会被拒（`unknown field network, expected netuid`），带 `limit` 同样被拒 —— 现有代码本来就不带这两个参数，**参数不是问题原因**。

**修复后实测**：

| 项 | 结果 |
| --- | --- |
| HTTP | 200 |
| `official_total` | 1196 |
| `inserted` / `updated` / `skipped` | 0 / 0 / 1196（名单未变，幂等） |
| `source` | `taostats:/v1/validators/active` |
| `synced_at` | 2026-11-06T12:13:53Z |
| `from_cache` | false |

**回归**：`tests/test_taostats.py` 新增 `test_request_get_retries_transient_transport_error`（前两次 `httpx.ConnectError` SSL EOF、第三次成功，断言调用 3 次且退避为 `[1.2, 2.4]`）与 `test_request_get_gives_up_after_all_attempts`（三次全失败才 `TaostatsUnavailableError`）；`pytest -q` = **230 passed**；`/api/health` 版本 0.7.6，页面资源戳 `?v=0.7.6`。

## 0.7.5 验证记录（2026-11-06）

**起始地址画像**（真实 Taostats 账户，`POST /api/relations` 实测）：

| 地址 | 注册日期 | 交易次数 | 注册天数 | 日均频次 | 总余额（可用 / 质押） |
| --- | --- | --- | --- | --- | --- |
| `5HiveMEo…FLCv`（交易所） | 2022-10-30（nakamoto） | 47 288 | 1 468 | **32.21 笔/天** | 119 310.99（50.00 / 119 260.98） |
| `5FqqXKb9…Z8a5` | 2025-09-17（finney） | 171 389 | 416 | 411.99 笔/天 | 17 137.24 |
| `5HK5tp6t…REpeN`（已清空） | 2022-12-30（nakamoto） | 6 | 1 407 | 0.0 笔/天 | 0（0 / 0） |

- 第三条是「真实为 0」与「未获取」的区分验证：余额 0 显示 `0 TAO`，而不是「未获取」。
- `seed_balance_tao`（RPC 自由余额）与 `seed_profile.balance_total_tao`（Taostats 总余额含质押）同时下发，界面优先用后者。
- 交易次数取自分页 `total_items`，与本次翻页页数无关：只查 1 页也能拿到 17 万这一级的总量。

**降级路径实测**：
- 本地 5 次/分钟限速用满后，`seed_profile` 退回 `transfer_count_scope=local_index`（用本次结果里的转账笔数），注册日期/余额留空，主查询照常返回 61 条关联——没有被画像拖垮。
- 使用一个不存在的地址查询，四项全为 `null` 且无异常抛出。
- `include_balances=False`（停止续查）时不发画像请求，测试用 monkeypatch 断言调用次数为 0。

**界面探针**（`.probe/ui_check_074_profile.py`，1440 / 1180 / 1024 / 760 四档，输出 `.probe/shots/check-074-profile-<width>.json`）：

| 宽度 | 地址字号 | 是否截断 | 网格列 | 指标重叠 | 页面横向溢出 |
| --- | --- | --- | --- | --- | --- |
| 1440 | 17px / 700 | 否 | 278px + 4×174px | 无 | 0 |
| 1180 | 17px / 700 | 否 | 3 列（地址独占整行） | 无 | 0 |
| 1024 | 17px / 700 | 否 | 3 列 | 无 | 0 |
| 760 | 17px / 700 | 否 | 2 列 | 无 | 0 |

- 地址颜色为当前主题的强调色（浅色主题实测 `rgb(9, 111, 100)`），左侧 3px 竖条同色。
- 四个指标的标题 12px / 数值 20px / 副行 11px，副行文字最长的「可用 50.00 · 质押 119,260.98」在四档下 `overflow=0`。

**回归**：`pytest -q` = **228 passed**（新增 `tests/test_seed_profile.py` 8 条）；`/api/health` 报 `0.7.5`；页面资源戳 `?v=0.7.5`；`使用说明.html` 重新生成 73 348 bytes。

## 0.7.4 验证记录（2026-11-06）

**剪枝单元测试**（`tests/test_reconcile_history.py`，11 条）：覆盖「路径逐跳判断（终点之后的下游删、终点本身留）」「路径缺失时退回中转图可达性判断」「两者都无证据时不删且计入 `unverified`」「边表原样保留」「`correction` 元信息（轮次累计、按角色统计）」「`watch_candidates` / `auto_import_log` 的清理与撤回」「`dry_run` 不落库」等分支，全部通过。

**真实库全量校正**（`data/wallet_link.db`，1902 条地址）：
- 校正前统计当前关联终点 **1214 个**：交易所 17 / 验证者 1196 / 系统 1。
- `POST /api/library/reconcile {dry_run:true}` → 扫到相关归档快照 35 条，其中 `changed=5`、合计 `related_removed=415`（交易所下游 262、系统下游 153）、`unverified=180`（老的分轮续查记录，`path_transfers` 为空且边表不连通，按「无证据不删」原则保留并提示重查）。
- 单条样本：id=24 的记录关联数 **199 → 9**，`payload.correction` 写入 `removed_related`、`rounds=1`、`by_role`、`at`。
- 落库后再跑一次预演 → `changed=0`（收敛，不会二次误删）。
- **收敛性问题的修复过程**：第一版剪枝连边表一起精简，导致第二次校正可达图变小、又多删 27 条（不收敛）；改为**边表原封不动**，只剪可达结果集。另外老记录曾纯靠可达性判断误删 180 条，改为「有证据才删」。

**中转图遍历缺陷的发现与修复**（0.7.4 交付后复核时发现）：

- 症状：`unverified=180` 一直被当成「老记录缺证据」，实际复核发现这两条记录（id13 111 条 / id15 122 条）的边表是**单连通分量**（113 / 125 个节点，覆盖全部关联条目），证据是有的。
- 根因：BFS 里发现终点时执行 `blocked_by[neighbor] = neighbor; continue`，**终点没有入队**；而「标记终点整片下游」的代码写在「出队节点是终点」那个分支里，于是该分支成为死代码，任何终点下游都不会被标记。
- 最小复现：`SEED → A → T(终点) → B`，期望删 B，实际返回 `related_removed=0, unverified=1`。
- 修复：终点照常入队（自身仍保留在结果里），弹出时执行下游标记。修复后同一用例正确删掉 B，`unverified=0`。
- 补跑校正（先备份 `data/wallet_link.before-reconcile-nodegraph-fix.db`，`integrity_check` ok）：

| 记录 | 校正前 | 本次移除 | 校正后 |
| --- | --- | --- | --- |
| id 15 | 122 | 92（交易所 3、系统 89） | 30 |
| id 13 | 111 | 88（交易所 88） | 23 |

- 合计本次移除 **180 条**，`unverified` 归零；再预演 `changed=0`。两轮累计移除 **595 条**（415 + 180），全部关联条目 1380 → 785。
- 新增回归测试 4 条：`test_prune_without_paths_drops_terminal_downstream`、`..._drops_whole_downstream_chain`（整链）、`..._keeps_reachable_side_branch`（绕开终点可达的旁支必须保留）、`test_prune_counts_unverified_when_graph_does_not_connect`（边表确实不连通时才计 unverified）。

**触发点实测**：`decide_exchange` / `decide_exchange_address`(confirm) / `decide_public_address`(marked) / `update_library_categories` / `update_library_note`（含「交易所」）/ `update_library_label`（含「交易所」）六处返回体都带 `correction`，前端在「已标为交易所 / 已标为公共地址」提示后补一句「已顺带校正 N 条历史结果」；同步交易所 / 同步验证者名单完成后自动跑一次校正（`watch=False`）。

**界面探针**（`.probe/ui_check_074.py`，1440 / 1180 / 1024 三档，输出 `.probe/shots/check-074-<width>.json`）：
- 地址库清单工具栏新增「校正历史结果」后，三档下按钮排布为 2 行（第一行：撤回上次导入 / 重新导入预置库 / 合并群组 / 格式 / 导出全部 / 导出选中；第二行：校正历史结果 + 删除所选），`barOverflow=0`、`panelOverflow=0`、`tableOverflow=0`、按钮矩形互不重叠（`overlap=false`）；1440 下第二行只有这两个按钮，低频校正动作不占主行。
- 录入区 4 个按钮（添加地址 / 导入文件 / 同步官方交易所地址 / 同步验证者地址）仍为单行，与 0.7.3 一致。
- 点击行为：拦截 `fetch` 后点「校正历史结果」，只发出 **1 次** `{"dry_run":true}` 请求，未触发落库；返回 `changed=0` 时走提示分支而非确认框，toast 显示「历史结果已经是干净的：35 条记录里没有交易所/验证者/公共地址下游的无效关联。」，按钮文案与可用状态复原。`changed>0` 的确认分支（`window.confirm` 展示记录数 / 移除条数 / 巡检候选清理 / 自动入库撤回 / unverified 提示 / 当前终点按角色统计）由 0.7.4 落库前那次真实预演（35 条记录、415 条关联）验证。

**回归**：`pytest -q` = **220 passed**（含中转图缺陷的 4 条回归）；`/api/health` 报 `0.7.4`；页面资源戳 `?v=0.7.4`；`使用说明.html` 重新生成 **71 508 bytes**。

## 0.7.3 验证记录（2026-11-06）

**验证者终止关联**（`tests/test_builtin_addresses.py`）：
- 内置验证者 `5HK5…REpeN` 与人工同步来源（`taostats:/v1/validators/active` + 多分类 VALIDATOR）两种情况都验证过：`SEED → 验证者 → DOWNSTREAM` 结构下，验证者保留在结果里、`DOWNSTREAM` 不出现；验证者行 `address_role` 为「人工确认验证者」。
- `exchange_balance_candidates` 的验证者判断从「只看主分类」改成读全部分类，`大V + VALIDATOR` 这种组合不会再被当成交易所候选去查余额。

**同步验证者地址**（真实 Taostats 账户实测）：
- `POST /api/library/import-validators` → `inserted=1194`、`updated=1`（内置验证者补上官方分类）、`skipped=1`；返回体含新增地址明细与 `official_total`，与交易所同步同结构。
- 第二次同步（5 分钟进程缓存内）不再新增：`inserted=0`、`updated=0`、`skipped=2`，`from_cache=true`。
- 库内总计 708 → 1902 条；新增记录 `category=VALIDATOR`、`source=taostats:/v1/validators/active`，名称留空显示地址后四位，地址库清单折叠在「验证者（系统）· 未命名」一组里。

**内置固定名单**（`app/builtin_addresses.py`，真实库启动实测）：
- 启动后 `source like 'builtin:%'` 共 6 条：5 个交易所（`builtin:exchange`）+ 1 个验证者（`builtin:validator`）。
- 已存在的人工交易所（4 条）沿用原记录、来源改为 `builtin:exchange`；`5FV99…` 原本是「大V / rrik 自建库」，写入后主分类与名称保留，多分类变成 `["大V","EXCHANGE"]`，`builtin_locked=true`。
- 重复启动（`ensure_builtin_addresses` 第二次调用）`inserted=0 / updated=0`，不产生重复行。
- 锁定行为：单条删除抛 `InputError`（接口 409）；批量删除自动跳过内置行（测试里 3 条地址批量删只删掉 2 条普通地址）；解散地址库保留内置行；改分类、改判交易所、取消公共标记均被拒绝。

**界面探针**（`.probe/ui_check_073.py`，1440 / 1180 / 1024 三档）：
- 地址库录入区 4 个按钮（添加地址 / 导入文件 / 同步官方交易所地址 / 同步验证者地址）在 1440、1180、1024 下都是**同一行**（`buttonRows=1`），容器 `panelOverflow=0`、表格 `tableOverflow=0`。
- 地址库清单里 6 行显示「内置锁定」禁用按钮与「内置固定」来源标签，6 个勾选框全部禁用（不参与批量删除/选中导出）。
- 结果页 1 级关联行现在有「路径明细」按钮（`depth1WithButton=1 / depth1=1`），点击后展开 1 行路径表，标题为「从起始地址 … 到 … 的关联路径（共 1 笔转账，按路径顺序）」。

**回归**：`pytest -q` = **205 passed**；`/api/health` 报 `0.7.3`，页面资源戳 `?v=0.7.3`；`tools/build_usage_html.py` 重新生成 `使用说明.html`（68 956 bytes）。

## 0.7.2 验证记录（2026-11-06）

**地址库导出范围**（接口层，本机 707 条地址实测）：
- `POST /api/library/export {format:"csv", addresses:[2 条]}` → 返回 2 行，`Content-Disposition` 含 `selected-2`；
- `GET /api/library/export?format=json` → 707 条，与改前一致；
- 选中集合为空、或选中的地址已不在库中 → 422 并给出中文原因，不会退化成导出全库。

**界面探针**（`.probe/ui_check_072.py`，1440×900）：
- `导出选中 (0)` 初始为禁用；勾选 2 行后变成 `导出选中 (2)` 且可点；
- 工具栏含「格式」下拉（CSV / JSON）+「导出全部」+「导出选中 (N)」三个控件；
- `#filter-publics`（过滤公共地址）存在；地址库 707 行每行都有「标为公共地址」按钮；
- 结果页与地址库页的标记按钮走同一个 `decidePublic()`，地址库页点击不会跳回结果页。

**公共地址行为**（`tests/test_public_address.py`，6 条）：
- 标记/取消只动多分类，主分类保持「群友」不变；重复标记幂等；新地址标记时主分类为 PUBLIC；
- `SEED → 公共地址 → DOWNSTREAM` 结构下，公共地址保留在结果里但 DOWNSTREAM 不出现，`address_role` 为「公共地址」；
- 勾 `filter_publics` 后公共地址本身也被排除；
- 排序：交易所第 1、公共地址第 2；
- 导出全部 / 选中（CSV/JSON）条数正确，空集合报错；
- `POST /api/library/mark-public` 返回的 categories 含 PUBLIC。

**回归**：`pytest -q` = **197 passed**；`node --check app/static/app.js` 通过；`/api/health` 报 `0.7.2`，页面资源戳 `?v=0.7.2`；`tools/build_usage_html.py` 重新生成 `使用说明.html`（66 013 bytes）。

## 0.7.1 验证记录（2026-11-06）

**布局量测**（`.probe/measure_forms.py`，1440 / 1180 / 1024 / 760 / 420 五档）：
- 1440px：关联结果表 1051/1051、地址库表 1029/1029、查询记录表 1051/1051，三张表**零横向滚动**；页面 `overflowX = 0`；面板内无元素越过右边界。
- 1180px：关联结果 1044/806（滚 238）、地址库 949/784（滚 165）、查询记录 1020/806（滚 214）——窄屏按设计横滚。
- 1024 / 760 / 420px：页面 `overflowX = 0`，表单转 1–2 列，按钮整行，表格按需横滚（420px 下关联表滚 608）。
- 侧栏：`position: sticky`，`scrollY=772/806/900` 时 `sidebar.top` 恒为 0；≤950px 为顶部横条（原行为）。

**控件规格**（CDP 计算样式）：
- `#run-scan` 122×46、15px / 800 字重、`linear-gradient` 实心渐变、圆角 11px。
- `#seed` 390×46、`Cascadia Mono` 12px，48 位 SS58 可完整显示；`#depth` 96×42。
- 扫描设置一行排布（`source-row` 高 40，三项同行）。

**回归**：`pytest -q` = **191 passed**；`node --check app/static/app.js` 通过；`/` 返回资源戳 `styles.css?v=0.7.1`。

## 0.7.0 验证记录（2026-11-06）

**后端**（`tests/test_watch.py`，11 例）：
- 基线：首次巡检只写 `watch_state.last_block`，历史对手方不灌进候选表；第二次巡检只取 `block > baseline` 的边。
- 阈值：单笔 ≥ N 命中、与该地址累计 ≥ N 命中、两者都不满足时不入库。
- 多库归属：同一对手方在两个库都有流水时取累计金额最大的库；后到更高累计时改判 `book` 并更新同一条 active 日志（不新增第二条）。
- 撤回：删除自动写入地址 + 拉黑，再次巡检不再自动入库；已在库地址不重复写入。
- 库间规模：`book_pair_stats` 按方向累计；关联强度分档（弱/中/强/极强）、3 笔升档、≥2 跳降档、交易所优先排序各有用例。

**界面**（`.probe/ui_check_0700.py`）：
- 巡检面板渲染出状态文案、阈值（30）、间隔、每日预算；点「入库日志」后 `#watch-log-panel` 取消 `hidden` 并显示条数。
- 结果表表头含「关联强度」列，首行该列渲染 `.strength-badge`，`title` 带计算依据；`#related` 无 JS 报错。

**回归**：`pytest -q` = **191 passed**；`node --check app/static/app.js` 通过。

## 0.6.29 验证记录（2026-11-06）

**后端**（`tests/test_timezone_display.py`，4 例）：
- `db.iso_utc()` 对 naive 时间补 `+00:00`、对已带 tz 的时间不重复加；`to_beijing()` 换算正确。
- 查询记录归档摘要里的时间带时区；地址库 `created_at` / `updated_at` 输出带时区。
- 失败查询也会产生一条归档记录（含 `job` 字段：rounds / requests / stopped）。

**界面**（`.probe/ui_check_0629.py`）：
- 查询记录「完成时间（北京）」列显示的时与后端 UTC + 8 一致；地址库时间、本地索引任务时间同样按北京时间渲染，浏览器时区改动不影响结果。

**回归**：`pytest -q` = **180 passed**；`node --check app/static/app.js` 通过。

## 0.6.28 验证记录（2026-11-06）

**后端**（`tests/test_official_exchange_lock.py`，9 例）：
- 官方来源地址：`decide_exchange` / `decide_exchange_address` 拒绝改判并返回原因，人工来源地址可正常改判。
- 只增不改：二次同步不改原主分类、名称、备注、所属库，只追加 `EXCHANGE` 并锁定；名单中消失的地址不被删除。
- 多分类下 `大V + EXCHANGE` 被识别为已确认交易所，可用于关联终点/过滤/导出排除。
- 导入、备份还原、往「人工确认/否定交易所」组移地址都动不了官方地址。
- `GET /api/library/exchange-sync` 返回条数/最近同步/待提示新增地址，`ack` 后待提示清空。

**界面**（`.probe/ui_check_0628.py`、`.probe/ui_check_0628_notice.py`）：
- 官方地址行只显示「官方交易所 · 不可改判」，无「改为否定 / 重置」按钮；人工确认行仍可改判。
- 「同步官方交易所地址」下方显示官方条数与最近同步时间；开机自动同步的新增地址提示只弹一次，`ack` 后刷新不再出现。

**回归**：`pytest -q` = **176 passed**；`node --check app/static/app.js` 通过。

## 0.6.23 验证记录（2026-11-05）

**后端**（`tests/test_merge_groups.py`，6 例）：
- 两库合并成新名字：4 个地址全部归到新库，`label` 同步；`note` / `exchange_rejected` 保留（被改判的地址 `category=EXCHANGE` 也保留）；旧库从 `address_books` 消失。
- 继承组名：`target` 取来源库之一时不新建库，另一库并入后删除。
- 自建库 + 标签组混合合并成功，原标签组不再有地址。
- 撤回：两库各 2 条恢复、合并库清空、两个库名重建。
- 拒绝：不足两组 / 目标名与第三方库重名 / 来源库不存在 / 非法 kind，且失败不动数据。

**界面**（`.probe/ui_check_0624.py`）：
- 打开面板 → 勾「合并测甲」「合并测乙」→ 汇总显示「已选 2 组，共 3 个地址」，按钮变「合并（2 组 / 3 个地址）」；
- 勾选后输入框内容仍在（`nameKeptAfterRerender=合并测丙`）；
- 合并后 toast「已把 2 个组合并成「合并测丙」，共 3 个地址。」，面板自动收起，组列表出现新组、两个旧组消失；
- 撤回后两库恢复，再以「沿用已有组名」合并到「合并测乙」，甲消失、乙保留。

**布局**（`.probe/layout_0624.py`，1422px）：工具栏 8 个控件仍单行（右边界 1400 = 容器右边），面板宽 1025、无右溢出，组列表首行同排，命名行单行（高 39px）。

**回归**：`pytest -q` = **158 passed**；`node --check app/static/app.js` 通过。探针结束后数据复原（总地址 672、`address_books` 7 个、测试地址 0 条）。

## 0.6.22 验证记录（2026-11-05）

**问题复现**：`POST /api/library/group-addresses`（`label=人工确认交易所`，不带 `move_existing`）对两个已在 `rrik` 的地址返回 `inserted=0, skipped=2, conflicts=[{group:rrik}×2]`，与用户“看不到新增”的现象一致。

**转入验证**：同请求加 `move_existing=true` → `moved=2`，库内两条变为 `label=人工确认交易所 / book='' / category=EXCHANGE / exchange_confirmed=1`，确认组 3→5 条、rrik 199→197 条。

**界面全链路**（`.probe/ui_check_0623.py`，headless Chrome + CDP）：
- 复选框存在、默认**不勾**；
- 不勾直接保存 → toast：「1 个已在「rrik」组，已跳过——勾选“已在别的组的地址也移到本组”再点一次就能转过来。」
- 勾上再保存 → toast：「从别的组转入 1 个。」组内 5→6 条，编辑器自动收起；
- 两次「撤回上次导入」后，该测试地址回到 `rrik`、确认组回到 5 条（探针未污染用户数据）。

**回归**：`pytest -q` = **152 passed**（新增 2 例）；`node --check app/static/app.js` 通过。

## 0.6.21 验证记录（2026-11-05）

- `pytest -q`：**150 passed**（新增 `tests/test_group_add_and_exchange.py` 6 个用例：多分隔符/去重、跳过已存在且不改原记录、无有效地址报错、撤回快照、确认↔否定来回改判、保留人工组名、接口层 200/422）。
- `node --check app/static/app.js` 通过；`使用说明.html` 重新生成（49 578 bytes）。
- 界面探针（真实服务 + 临时库，测完即解散）：
  - 未展开组时「＋ 手动添加地址」在折叠区里不可见；展开后可见、可点开输入框。
  - 输入 1 个合法 + 1 个非法地址 → toast「已加到「临时添加测试」1 个地址，1 个格式不对（not-an-ss58-address）」；组内地址数 1 → 2，新地址备注为「UI 手动添加」，编辑器自动收起。
- 交易所改判渲染：构造 4 行结果（已确认 / 已否定 / 待确认 / 未入库）→ 按钮分别为「改为否定 + 重置为待确认」「改为交易所 + 重置为待确认」「确认交易所 + 否定交易所」「标为交易所」。
- 真实库核对：`5EiXej3A…TiN`、`5DunDrFV…N9Bn5E` 两条此前被人工否定的地址，在 rrik 组展开后显示「已人工否定 + 改为交易所 + 重置」。
- 全库 20 个标签组，各组展开后均有「＋ 手动添加地址」入口。

## 0.6.20 验证记录（2026-11-05）

- `node --check app/static/app.js` 通过；`pytest -q` 见下；`tools/build_usage_html.py` 重新生成 `使用说明.html`（47 207 bytes）。
- 构造 8 条关联数据（1 级 3 条 / 2 级 3 条 / 3 级 2 条，金额 0–500 TAO）做界面探针：

| 筛选 | 结果 |
| --- | --- |
| 不限 | 8 条 |
| 流入或流出 ≥ 10 | 4 条（5A/5C/5E/5G），提示“已隐藏 4 条轻微关联（显示 4 / 8 条）” |
| 流入 ≥ 10 | 4 条 |
| 流出 ≥ 10 | 1 条（5C） |
| 累计 ≥ 10 | 5 条（5A/5C/5D/5E/5G） |
| 流入且流出 ≥ 10 | 1 条（5C） |
| 2 级 + 流入或流出 ≥ 10 | 1 条（显示 1 / 3） |
| 重置 | 8 条，mode=off、min 清空、提示隐藏 |

- 记忆：设 `either/1` 后 `localStorage["wb.relatedAmountFilter"] = {"mode":"either","min":"1"}`，筛后 6 条（滤掉 0.5/0.2 与 0/0 两条）。
- 布局量测（1422px）：层级下拉、金额下拉、TAO 输入、重置按钮**同一行**（ySpread = 0），无重叠，控件右边界 1331 < 面板右边界 1355，无横向溢出。

## 0.6.19 验证记录（2026-11-05）

- 删除类入口逐项复核：单条地址删除、批量删除所选、删除分类、解散地址库、删除/清空查询记录、撤回上次导入、清除 API key 共 7 处，全部带 `window.confirm` 前置确认。
- 确认文案增强：单条删除显示 `地址（前后各四位）/ 名称（含所属库）/ 分类 / 备注摘要`；批量删除按组汇总（最多 6 组 + “其余 N 个组共 M 条”）；删除分类带当前地址数；清除 API key 由“无确认”改为确认。
- `node --check app/static/app.js` 通过；`app/templates/index.html` 资源戳 `v=0.6.24`。

## 0.6.13–0.6.16 摘要

- **0.6.13**：关联结果分页（10/20/50/100，默认 10，首页/末页/跳页）落地；路径明细按 `_oriented_path()` 从扫描起始地址定向（`path_from` / `path_to` / `direction`）；确认交易所后其下游噪音关联在刷新后自动剪除。
- **0.6.14 / 0.6.15**：标签未填默认取地址后四位；取消单条改名，改为整组改名 + 单条只改备注；同步支持合并进已有地址库（未命中才新建）；新增「撤回上次导入」（`ImportBatch` 单槽快照）。当时回归 **134 passed**。
- **0.6.16**：外发预置地址库（6 组 / 355 地址，随包 `app/seed_books`）；解散改为连库带地址一起删除且可撤回；地址库清单支持整组全选。当时回归 **138 passed**。

## 0.6.18 验证记录（2026-11-05）

- `.venv\Scripts\python.exe -m pytest -q`：**144 passed**（1 条既有 Starlette/httpx 弃用 warning）；`node --check app/static/app.js` 通过。
- 接口实测：`POST /api/library/group-categories {"categories":["群友"],"book":"惩罚者"}` → `updated 16`；`last-import` 返回 `kind="category"`、`updated 16`；`undo-last-import` → `restored 16`，16 条全部回到「其他」。
- 界面探针：`[data-edit-categories]` 计数为 **0**（行内入口已移除）；19 个标签组各有 1 个「改分类」按钮；点开后编辑器默认只勾当前主分类；保存后该组 38 条全部换分类并提示「整组分类已改为 …（38 个地址）」，编辑器自动收起。
- 多选误操作已修正：默认只勾主分类，勾多个时确认框额外提示"这组地址会同时属于这几个分类"。

## 0.6.17 验证记录（2026-11-05）

### 自动化检查

- `.venv\Scripts\python.exe -m pytest -q`：**142 passed**，1 条既有 Starlette/httpx 弃用 warning。
- `node --check app/static/app.js`：通过。
- `tools/build_usage_html.py`：重新生成 `使用说明.html`（44 372 bytes）。

### 真实数据库链路（127.0.0.1:8766，data/wallet_link.db）

| 步骤 | 请求 | 结果 |
| --- | --- | --- |
| 概况 | `GET /api/library/seed-books` | `{"groups": 6, "addresses": 355}` |
| 解散预置组 | `DELETE /api/library/books/DFV` | `{"dissolved": true, "deleted": 1}`，库列表剩 5 组 |
| 重新导入 | `POST /api/library/seed-books/reinstall` | `inserted 1`、`created_books ["DFV"]`，库列表回到 6 组（DFV 1） |
| 撤回 | `POST /api/library/undo-last-import` | `deleted 1`、`book_removed true`，DFV 从库列表消失 |
| 再次重新导入 | 同上 | `inserted 1`、`created ["DFV"]`，DFV 复原 |
| 幂等 | 已补齐后再点 | `inserted 0`，不写新快照（`last-import.created_at` 不变） |

### 界面（Chrome headless CDP 探针）

- `#reinstall-seed-books` 按钮存在、文案“重新导入预置库”、`disabled=false`、提示含“6 组 / 355 个地址”。
- 确认框文本包含三条影响说明；已补齐时 toast 显示“预置地址库已是最新，没有缺失的地址。”
- 工具栏 8 个控件一行排开：最大右边界 1331 px，视口 1422 px，`barOverflow=false`，无控件被裁剪。

### 覆盖测试

- `tests/test_seed_books.py` 新增：解散后重新导入补回、人工改动保留且可撤回、两个接口往返、预置库概况统计。
- `tests/test_address_books.py`：解散改为删除地址后清单不再残留，撤回可连库带地址恢复。
- `tests/test_version.py`：版本号与更新日志标题同步到 0.6.17。

**外发包**：`Bittensor地址关联挖掘-0.6.17-外发包.zip`，39.6 MB / 79 文件，含 6 个 `app/seed_books/*.txt`。

## 0.1.5 余额和地址显示验证

- 关系结果模型和服务结果均返回起始地址余额、关联地址余额。
- 页面金额和余额均按两位小数显示。
- 页面仅显示关联地址前四位和后四位，复制按钮仍使用完整地址。
- 余额查询失败时保留关联结果并显示“未获取”提示。

## 0.1.4 地址库导出与迁移验证

- 地址库 CSV/JSON 导出与重新解析测试通过。
- 交易所人工确认/否定状态可随导出文件保留。
- 导出接口只包含迁移所需字段，不包含本机数据库内部 ID。

## 0.1.3 页面版本与更新日志验证

- `pytest -q`：`43 passed`，1 条既有 Starlette/httpx 弃用 warning。
- `python -m compileall -q app tests`：通过。
- `node --check app/static/app.js`：通过。
- `git diff --check`：通过。
- 重启 `127.0.0.1:8766` 后，`/api/health` 返回版本 `0.1.3`；`/api/version` 返回 4 个版本记录。
- Playwright 验证页面底部显示 `当前版本：0.1.3`，侧边栏更新日志页面显示当前版本条目。

**验收时间：** 2026-10-31 12:39:42（Asia/Shanghai）
**交付目录：** G:\bittensor-wallet-link-intel

## 已通过

- 程序测试：5/5 通过。
- API 端到端：首页、健康检查、SS58 校验、三级查询、地址库命中、CSV 导入、四级拒绝均通过。
- 独立服务启动健康检查通过。
- 最终公开 RPC 扫描 1 个最新区块：扫描 1 块、入库 2 条 Balances.Transfer，交易哈希存在。
- 地址库命中可同时返回关联地址、路径层级、金额与库标签。

## 尚未满足

任意历史窗口的全链三级追溯尚未完成。当前公开入口节点会裁剪历史运行时状态；需接入保留完整历史的归档节点或可靠第三方索引，并通过覆盖完整性、缺口和容量验收。

运行前请阅读 README.md。设计与边界见 DESIGN_FEASIBILITY.md。

## 2026-10-31 扫描租约锁修复复验

### 修复

- `Database.initialize()` 现在创建 SQLAlchemy 声明的全部表，包括 `scan_lock`。
- 扫描租约领取前显式执行 SQLite `BEGIN IMMEDIATE`，将“检查租约、创建或接管租约、创建扫描任务”放在同一写事务中，避免并发进程同时取得空锁。
- Windows 临时数据库测试在 `finally` 中释放 SQLAlchemy 连接池，避免测试失败时临时数据库文件被占用。

### 本轮实测

- 完整 pytest：**9 passed**。
- API 端到端脚本：**API_E2E_OK**。健康检查和首页成功；三级关系和地址库命中正确；CSV 导入成功；四级查询返回 422。
- 租约测试覆盖有效租约拒绝重复扫描、过期租约接管并释放，以及两个并发首次扫描只有一个进入扫描器。
- API 测试使用独立临时 SQLite 文件；项目已有的 `data/test_api_final.db` 未被覆盖。

端到端测试输出包含一条 Starlette 关于 `httpx` TestClient 的弃用提醒，但测试通过。上述测试没有重新扫描公开 RPC。本节不更新 0.1.0 既有 RPC 实测记录；该记录仍是此前一次扫描的历史结果。

## 2026-10-31 Taostats 免费档接入验收

### 实现

- 新增 `app/taostats.py`：调用 `GET https://api.taostats.io/v1/transfers`，密钥可由页面设置到当前服务进程内存，也兼容进程环境变量；没有把密钥写进代码、数据库或返回响应。
- 官方文档将 `data[].amount` 定义为 u64 十进制字符串。客户端按 RAO 读取，并按 `1 TAO = 10^9 RAO` 转成 TAO；记录保留交易哈希、区块和时间。
- 支持地址双向查询、分页、每进程最多 5 次请求/分钟的本地限速，以及同一地址和页 5 分钟内存缓存。
- 每次关联最多发起 5 个 Taostats 请求。免费额度用完、遇到鉴权/服务端限流、返回坏记录或分页服务窗口受限时，结果会显示部分状态和原因。
- 页面默认选择 Taostats，也可关闭后只查本地索引。Taostats 与本地索引合并分析；页面展示两种来源、API 请求数、缓存命中、分页状态与地址库命中。
- `/api/relations` 保留 API 客户端的默认本地数据源，Taostats 由界面显式选择；`/api/overview` 只返回 key 是否已配置的布尔值。

### 本轮实测

- 完整 pytest：**20 passed**。覆盖 RAO 转 TAO、请求参数和 Authorization 头、分页和缓存、5 次/分钟本地限速、401/429、三级地址扩展、地址库命中、单次查询预算、鉴权失败状态、API 数据源切换，以及页面 API key 保存/清除/不回显。
- Python 模块 `compileall` 通过。
- 官方文档页面可访问，确认端点、响应字段、`amount` u64 字符串和 `pagination.next_page`；官方套餐页当前展示免费档 $0、5 API credits/分钟及 10,000/月。
- 未配置 key 请求官方端点返回 HTTP 401，响应提示 Authorization token 缺失。这只验证鉴权要求，**没有**验证任何已认证 API key、免费额度实际扣点、真实转账历史范围或数据完整性。
- pytest 输出一条 Starlette 对 `httpx` TestClient 的弃用提醒；本轮测试仍全部通过。

### 验收边界与下一步

Taostats 的真实 API key 尚未配置，因此本轮没有进行真实地址的已认证转账查询。用户可在首页 Taostats API key 密码框中输入并保存，key 只保留在当前服务进程内存；服务重启后需要重新输入。之后选用一笔已有区块/交易哈希的公开转账核对双方地址、RAO 金额、区块、交易哈希与翻页状态。官方套餐页还显示 Free 的历史时间控制范围信息；当前无法据此确认 `/v1/transfers` API 的精确历史保留上限。分页读取完毕只表示本轮已读取 API 返回的分页，不等于全链历史完整。

Taostats. “Get Transfers.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/transfers/get-transfers. Accessed 31 Oct. 2026.

Taostats. “Taostats Pro.” *Taostats*, https://taostats.io/zh/pro. Accessed 31 Oct. 2026.

## 2026-10-31 界面主题验证

- 页眉主题选择器包含“跟随系统”“护眼·暖色”“护眼·深灰”三档。
- Playwright 验证系统、暖色与深灰三档均能切换；暖色与深灰会切换根主题和背景颜色，跟随系统会移除手动主题，刷新后会恢复上次选择。
- 390px 窄屏检查主题选择器保持可见，文档宽度没有横向溢出；浏览器控制台没有错误。
- 用户旧版暖色选择值 `on` 会迁移为暖色主题；关闭旧版选择会回落为系统主题。

## 2026-10-31 8766 运行服务诊断

- 本机 `127.0.0.1:8766` 已重启并健康检查通过；新版 `/api/overview` 返回本地索引 20 条转账、区块范围 `9,403,064–9,403,584`，并提供布尔型 `taostats_configured: false`。
- 对 `/api/relations` 发送无效 `data_source` 探针，当前新版服务返回 HTTP 422；此前旧进程会返回 200 并忽略数据源选项。
- 页面现在提供 Taostats API key 密码框、保存和清除按钮；key 只在当前服务进程内存中使用，不回显、不写入文件或数据库。

## 2026-10-31 关联结果、交易所和验证者过滤复验

### 本轮实现

- 查询结果页移除独立的地址库命中面板和交易明细展示，保留一张简洁关联表：地址、层级、流入 TAO、流出 TAO、累计 TAO、地址库标签。
- 简化导出 JSON，去除交易边、交易哈希、区块和 from/to 详情。
- 地址库新增 `EXCHANGE`、`VALIDATOR` 分类；支持手动添加、文件导入、批量删除。
- 新增官方交易所地址同步接口和页面按钮。接口通过当前进程的 Taostats key 请求 `/v1/exchange-addresses`，对 `coldkey` 去重并校验 SS58 后导入。
- 查询新增交易所和验证者过滤。验证者过滤支持手工地址库，并在有 key 时读取 `/v1/validators/active`；过滤地址不会继续穿透扩展。
- 关联结果和地址库明细均支持备注保存；地址库按标签折叠展示，当前 DSV 导入记录默认合并为一个 DSV 标签。
- 新增保守角色标记：明确地址库角色直接显示；转账次数、对手方和累计金额达到条件的地址显示“疑似交易所”；仅观察到转账活动且没有接入子网购买记录的地址显示“疑似中转钱包”。
- 用户指定的 `5Gr1Ab7LSV4eT6RdWmVeoxnTpDBWAZFLXHHrcaWNDdjK22pp` 已写入当前数据库，标签为 `OKX交易所`、分类为 `EXCHANGE`。

### 本轮实测

- 完整 pytest：**33 passed**，有 1 条现有 Starlette/httpx 弃用提醒。
- Python `compileall`：通过。
- `node --check app/static/app.js`：通过。
- 服务重启：`http://127.0.0.1:8766` 健康。
- Playwright 页面检查：结果页实际显示关联钱包、层级和金额；未发现交易哈希、区块号、from/to 交易明细；深灰主题切换生效；地址库页显示“同步官方交易所地址”按钮。
- 无 key 调用 `POST /api/library/import-exchanges`：HTTP 401，提示先配置 Taostats API key。没有伪造交易所目录，也没有宣称已完成真实官方地址同步。

本轮仍未使用用户真实 Taostats key 做认证请求；免费档历史覆盖、真实扣点和交易所目录当前内容需要在用户填写 key 后由页面实际同步确认。

## 2026-11-01 新规则回归

- 默认单边阈值和累计阈值均为 `1` TAO；页面、Pydantic 请求模型和 `LinkPolicy` 一致。
- 自动候选交易所只显示“疑似交易所（待确认）”，过滤集合只接受已确认的 `EXCHANGE` 地址。新增人工确认接口 `POST /api/library/confirm-exchange-address` 和地址库行确认接口；未确认候选在勾选交易所过滤时仍保留。
- 起始地址命中返回 `seed_library_matches`，页面按具体标签高亮；例如 DSV 记录显示为 `DSV地址库`。
- 新增 SQLite `api_requests` 持久化计数；`/api/overview` 返回今日、本月、累计、10,000 本月参考上限和剩余参考值。计数范围为本机实际网络请求，缓存不计数，未声称读取 Taostats 账户余额。
- 运行回归：`35 passed`；Python `compileall` 通过；`node --check app/static/app.js` 通过；重启 8766 后健康检查通过，页面无 console error，并看到默认 `1` TAO 和查询统计。

## 2026-11-01 关联结果与交易所判定改进

### 本轮实现

- 关联地址新增“复制”按钮，复制失败时给出手动复制提示。
- 关联结果新增全部、1 级、2 级、3 级筛选；筛选在前端完成，不重新调用 Taostats。
- 疑似交易所支持“确认交易所”“否定”“重置”；`exchange_rejected` 写入 SQLite，否定地址不会进入交易所过滤集合，也不会再次作为自动候选。
- 自动交易所候选只使用带时间戳的近 30 天转账，候选先读取公开 Bittensor RPC 的 `System.Account` free balance；余额低于 100 TAO或余额不可读时不自动标记疑似交易所。
- 通过公开 RPC 实测用户举例地址 `5CMaXXPk1qfbJxXAZr1ow7qyyai39qhRcocVtdXyDGZbQ99E` 当前 free balance 为 `10000 Rao = 0.00001 TAO`。这是当前余额快照，不代表历史余额或地址身份；该数值低于自动疑似交易所门槛。

### 验证结果

- 完整 pytest：`38 passed, 1 warning`。
- Python `compileall`：通过。
- Node `--check app/static/app.js`：通过。
- 重启 `8766`：健康检查通过，页面无 JavaScript error。
- API 人工否定回归：先将现有交易所记录临时设为 `reject`，确认返回 `exchange_confirmed=false`、`exchange_rejected=true`，再恢复 `confirm`，确认返回 `true/false`。
- Taostats 新版转账文档实测页面显示：`page` 的可达窗口为 1,000,000 行，深层历史应使用 `block_start/block_end` 或 `timestamp_start/timestamp_end`；未发现“免费档固定只保留 7 天”的官方表述。因此项目不把 7 天当作已证实事实。

### 仍然存在的限制

- 当前余额是查询时点快照，不能证明一两个月前余额；交易所判定仍需人工确认。
- `Balances.Transfer` 记录没有完整表达子网质押、解质押、购买或代理语义，因此代码不能仅凭“只有转账记录”证明地址没有其他链上行为。
- 本轮没有使用用户真实 Taostats API key，免费档账号的实际历史范围、credits 消耗和权限仍需用户在页面配置 key 后，以认证响应实测。

## 2026-11-01 交易所终点与关联中转识别

- 已确认的 `EXCHANGE` 地址现在作为关联终点：不勾选过滤时仍显示该地址，扫描不会继续读取其下游转账；勾选过滤时仍按原规则跳过该地址。
- 与已确认交易所公共地址直接相连的普通地址显示“疑似中转钱包（交易所关联）”，不受最低转账次数和当前余额门槛影响。
- 本地测试覆盖：交易所公共地址之后的下游地址不会出现在结果；只有一笔转账的个人地址会被标记为交易所关联疑似中转地址。
- Taostats mock 覆盖：不勾选交易所过滤时，查询只调用起始地址，不调用已确认交易所地址；结果保留交易所公共地址。
- 回归结果：`41 passed, 1 warning`；`compileall`、`node --check app/static/app.js` 和 `git diff --check` 通过。

## 2026-11-04 0.6.2 离线授权与同机多副本验收

> 0.2.0–0.6.1 的验收细节见 `CHANGELOG.md` 与 git 提交记录，本节记录当前版本的实测结论。

- 全量 pytest：`101 passed, 1 warning`（新增 `/api/health` 的 `pid`／`root` 断言；`tests/test_version.py` 版本号同步到 `0.6.2`）。
- `node --check app/static/app.js` 通过；`start.ps1`、`launch_desktop.ps1`、`stop.ps1` 经 `[System.Management.Automation.Language.Parser]::ParseFile` 校验无语法错误。
- 同一台电脑复制副本实测（`robocopy` 整目录到 `D:\wallet_link_test`）：`.venv` 在新路径可用（`sys.prefix` 指向新目录）；机器码仍为 `KNT5-YFER-N3PA-5W7Z`；`license.key` 拷过去直接生效（`licensed=true`）；`/api/library`、`/api/library/books`、`/api/library/categories` 与首页均返回 200。验证完成后已删除该副本。
- 端口冲突实测：D 盘副本占用 8798 时，主目录执行 `start.ps1 -Port 8798` 输出“端口 8798 已被 D:\wallet_link_test 占用。本副本改用端口 8799 启动”，`logs/wallet-link.port` 写入 `8799`；同目录重复启动输出 `Service is already running`；`stop.ps1 -Port 8799` 在无服务时输出“服务当前没有在运行”。
- 授权口径核对：解码本机 `license.key`（`base64` 解 payload）得到 `machines: ["KNT5YFERN3PA5W7Z"]`、`team: 内部团队`、无到期日，与“一机一码”一致；`tools/issue_license.py` 对超过 50 个机器码直接报错，`--machine "*"` 生成不绑机的码。
- 文档同步：`授权管理.md`（三种签法对照 + 机器码何时变）、`使用说明.md`／`使用说明.html`（同一台电脑复制一份做测试）、`README.md`、`CHANGELOG.md`、`VERSION` 全部更新到 `0.6.2`；静态资源缓存戳 `?v=0.6.7`。


## 2026-11-05 0.6.24 刷新按钮位置与按压手感验收

- 全量 pytest：`158 passed, 1 warning`；`tests/test_version.py` 改为从 `VERSION` 读取当前版本，不再硬编码。
- CDP 探针 `.probe/ui_check_0625.py`（1422×1100，从「查询记录」第一条恢复真实结果）：
  - `#refresh-result` 位于 `.related-panel` 内（`inRelatedPanel=true`），不再出现在顶部 `.actions`（`inActionsRow=false`）。
  - 计算样式：`font-size 15px`、`font-weight 800`、高 46px、白字、`linear-gradient(rgb(16,152,138), rgb(10,110,98))`、`box-shadow rgb(6,74,66) 0 4px 0, rgba(0,0,0,.15) 0 10px 20px`。
  - 与 `.result-filters` 同排（`sameRowWithFilters=true`），排在筛选组左侧，面板内右侧余量 -594px（无溢出）。
  - 手动移除一条关联后：文案「刷新结果（已移除 1）」，`is-armed=true`，伪元素涟漪 `refreshPulse` 生效。
  - `Input.dispatchMouseEvent` 按住不放：`transform: matrix(1,0,0,1,0,4)`（下沉 4px）、`box-shadow: rgb(6,74,66) 0 0 0 0, rgba(0,0,0,.42) 0 3px 9px inset`（下沿压平 + 内阴影），松开回弹。
  - 420px 窄屏：按钮宽 337px 占满一行、位于筛选组上方（`stackedAboveFilters=true`）、面板内无溢出。
  - 纠正记录：`Emulation.setDeviceMetricsOverride` 必须带 `mobile=True` 才会改变 `innerWidth`，否则仍是 1422px，窄屏断言会假通过。
- 截图：`.probe/71_refresh_button.png`（默认态）、`72_refresh_pressed.png`（按住态）、`73_refresh_narrow.png`（420px）。
- 回归：`node --check app/static/app.js`、`git diff --check` 通过。

## 2026-11-05 0.6.25 地址关联明细检索验收

- 全量 pytest：`167 passed, 1 warning`（新增 `tests/test_interlink.py` 9 例，Taostats 全程 mock）。
- 真实接口（本机 key，1 页 = 1 次请求）：
  - 组模式 `source=5HTL…RTif7`、`group_kind=label`、`group_name=6NCK`：`target_count=102`（组内 103 条排除 A 自身），命中 1 个地址 `5CPF…L5ex`，`in_tao=5.0`、`tx_count=1`、`last_seen=2026-04-10T00:06:12Z`，明细区块 7934019、tx `0x988f9abb14…`。
  - 地址模式同一对：`requests=2`（双向 from/to 精确查），同样命中 1 笔流入 5 TAO，说明两种口径一致。
  - 首轮曾出现一次 `SSL: UNEXPECTED_EOF_WHILE_READING`（本机代理抖动），重试即成功；该情况已按「优雅降级」处理：`stats.stop_reason=error:…`，不抛 500。
- 探针 `.probe/ui_check_0626.py`（1440×1200）：面板在地址库页顶部；组下拉首项 `6NCK（103 条）`；切到「和另一个地址比」时组字段隐藏、地址 B 字段显示；检索后统计条「命中地址 1 / 转账明细 1 / 请求 0，缓存命中 1 次 / 已查完当前范围 / 对比地址 102」，汇总表 6 列 1 行、明细表 7 列 1 行，面板内右侧余量 -24px（无横向溢出）。
- 截图：`.probe/74_interlink_panel.png`（面板）、`75_interlink_page.png`（整页位置）。
- 回归：`node --check app/static/app.js`、`git diff --check` 通过。

## 2026-11-05 0.6.26 启动脚本冲突标记拦截验收

- 复现场景：`git pull` 后 `start.ps1` 残留 `<<<<<<< HEAD`，双击 `启动.cmd` 报
  `The '<' operator is reserved for future use`（行 31 char 5）与 `Missing file specification after redirection operator`（char 6/7）。
- 仓库侧确认干净：`git grep -n "^<<<<<<<\|^>>>>>>>" HEAD -- '*.ps1' '*.cmd'` 无命中；`git diff --stat HEAD -- start.ps1` 无差异。
- `findstr` 行为实测（直接调用 `findstr.exe`，避开 Git Bash 代码页影响）：

  | 场景 | 命令 | exit | 结论 |
  | --- | --- | --- | --- |
  | 文件含 `<<<<<<< HEAD` | `/m /c:"<<<<"` | 0 | 命中，拦截 |
  | 文件含 `<<<<<<< HEAD` | `/m /c:"<<<<<<<"` | 1 | 7 个字符匹配不稳定，弃用 |
  | 正常 `param([int]$Port = 8766)` | `/m /c:"<<<<"` | 1 | 放行 |

- 分支走向（`cmd //c 启动.cmd`，临时目录内构造）：有冲突标记 → 打印「脚本里残留了 Git 冲突标记」并 `exit /b 1`；
  无标记 → 继续执行到 `.venv` 检查（「还没有安装运行环境」）。`安装.cmd` 同样在冲突时停下，不再调用 `setup.ps1`。
- 换行校验：两个 `.cmd` 均为纯 CRLF（`启动.cmd` 41 行、`安装.cmd` 25 行，裸 LF 计数 0）。
- 记录一个当时的误判：从 Git Bash 用管道执行 `.cmd` 时出现 `'xx' is not recognized`，当时对照 `git show HEAD:启动.cmd` 原版同样复现，
  于是判为「测试环境假象」。**这个判断是错的**——0.6.27 确认它是真 bug（UTF-8 批处理 + `chcp 65001` 的字节偏移错位），只是原版恰好没跨过读取边界。
- 回归：全量 pytest 通过；`git diff --check` 通过；服务启动后 `/api/version` 返回 `0.6.26`。

## 2026-11-05 0.6.27 批处理改 GBK 验收

- 现象复核：`@echo on` 追踪（`.probe/trace_cmd.py`）显示第 9 行 `rem PowerShell 会把它当成保留运算符，…` 被切成
  `>不懂的语法错误，这里先拦下来。`，`rem` 前缀丢失后作为命令执行，触发 `not recognized`。
- 编码对照（`.probe/enc_test.py`）：

  | 方案 | 是否错位 |
  | --- | --- |
  | UTF-8 + `chcp 65001` | 错位 |
  | UTF-8 无 `chcp` | 不错位（但 936 控制台下中文乱码） |
  | GBK 无 `chcp` | 不错位，中文正常 |

- 定界实验：`.probe/bisect_cmd.py` 用不同数量的 ASCII 填充行调整注释起始偏移（28–252 字节），全部正常 →
  排除「偏移本身」是唯一因素；`.probe/bisect_cmd2.py` 对真实脚本做前缀截断，确认问题与 0.6.26 新增注释行的字节长度相关。
- 改后验证（临时目录，`cmd //c` 实跑）：
  - `启动.cmd` 正常启动：无任何报错输出。
  - `启动.cmd` 遇 `<<<<<<< HEAD`：输出「脚本里残留了 Git 冲突标记（<<<<<<< HEAD），无法启动。」+ 修复命令，`not recognized` 计数 0。
  - `启动.cmd` 缺 `.venv`：正确输出「还没有安装运行环境 / 请先双击本文件夹里的 安装.cmd」，中文按 GBK 解码完全正确。
  - `安装.cmd` 遇冲突标记：正确拦截，未调用 `setup.ps1`。
  - `停止.cmd`：解析无误。
- 编码与换行：三个 `.cmd` 均为 GBK、无 `chcp`、纯 CRLF（启动 40 / 安装 24 / 停止 6 行）。
- 回归：全量 pytest 通过；`git diff --check` 通过。

## 2026-11-07 0.8.3 十级层级与阈值本地保存验收

### 实现
- 层级上限收敛到 `app/relation_config.py`：`MAX_RELATION_DEPTH = 10`、`DEFAULT_RELATION_DEPTH = 3`。`discover()`、`RelationInput`、`RelationJobInput`、历史导出 `depth` Query、`export_relation_history()` 全部引用它，不再各自写死 `le=3` / `(1, 2, 3)`。
- 层级规则不变：第 2 跳及更深必须过单边门槛，最终地址累计过累计门槛，交易所/验证者/公共地址/「系统」查到即截止，路径仍取 BFS 最短跳数。层级只影响 `while queue` 的 `depth >= max_depth` 判断。
- 三处层级下拉改为按 `/api/overview` 的 `max_depth` 动态生成（`applyDepthOptions()`），模板里不再写死 `1/2/3`。
- 分轮作业地址护栏按层级放大：`address_cap_for_depth()` = `max(150, depth × 50)`，通过 `payload.max_addresses` 回传。
- 阈值本地保存：`settings_store.load_scan_thresholds() / save_scan_thresholds()` + `GET/POST /api/settings/scan-thresholds`，值存 `data/settings.json` 的 `scan_thresholds`。`/api/overview` 一并返回，页面加载不发多余请求。

### 本轮实测
- **层级常量与接口**：`/api/overview` 返回 `max_depth=10 / default_depth=3`；`POST /api/relations` 带 `depth=10` 返回 `max_depth=10`，带 `depth=11` 返回 422。
- **BFS 深层正确性**（单测 + 真实接口）：造 10 跳链逐级跑，`depth=3/5/10` 分别得到 5 / 9 / 19 个关联，`by_depth` 每层地址数与链路构造完全对应；最深一行的 `path_transfers` 长度等于跳数，首跳 `path_from` 是起始地址、末跳 `path_to` 是该地址。
- **深层仍受同样的门槛约束**：第 4 跳插一笔 0.01 TAO（低于单边门槛 1）不会进结果；在第 3 跳位置标一个已确认 EXCHANGE，其后地址（`behind-exchange`）不出现，但交易所本身保留。
- **前端探针**（`.probe/chk_depth_083.py`，1440×1100 真实浏览器）：
  - `#depth` 选项 = `1..10`，默认选中 `3`；导出层级 / 层级筛选 / 历史导出层级三处都是 11 项（全部 + 1..10），末项文案「仅 10 级关联」。
  - 出厂状态阈值回填 `1 / 1`；改成 `20 / 100` 后按钮变「保存阈值 *」（`is-dirty`）；点保存后 toast「阈值已保存到本机：单边最低 20 TAO、累计最低 100 TAO」；刷新页面后两个输入框自动填回 `20 / 100`，层级回落默认 `3`。
  - `scanPayload()` 在层级选 `7` 时提交 `depth:7`，阈值同页面一致。
  - 布局：1440 宽下层级/阈值/方向/保存按钮同一行（`thresholdRow.spread = 42` 即单个控件高度，无错行），保存按钮 80×42 与输入框底边对齐；900px 窄屏同一行仍不溢出，`pageOverflowX = false`。
- **真实 Taostats**：`depth=3` 单轮拿 4 个 1 级地址、5 次请求用满；深层要靠分轮作业推进（单轮 5 次请求吃不下 4 级以上）。
- **地址护栏**：`address_cap_for_depth()` 实测 1–3 级 = 150、4 级 = 200 … 10 级 = 500，单调不减；归档结果的 `job` 里同时记下 `depth` 与 `max_addresses`，事后翻记录能区分「被护栏截停」和「层级本来就只有 3 级」。
- **真实 6 级分轮作业**（`5Gy8difK…jEbt`，`min_edge=10 / min_total=50`，8 轮 40 次请求）：共 **77 个关联地址**，层级分布 `1级 7 / 2级 4 / 3级 9 / 4级 20 / 5级 37`，最深 5 级地址的 `path_transfers` 完整 5 跳、累计 10000 TAO 判「强」。对比同一地址 3 级单轮只有 4 个 1 级地址，层级提升带来的扩展是真实有效的；作业因触到 8 轮上限而 `capped=true`，剩余 207 个待查请求（说明 6 级要跑透远不止 8 轮）。

### 验收边界与下一步
- 单轮 `/api/relations` 固定 5 次请求，4 级以上几乎必须开「自动分轮续查」；把层级调高时页面会提示同时抬高两个 TAO 门槛，否则浅层小额会把结果灌满。
- 本地索引目前只有 1 万条转账、且对手方大多是 `5EYCAe5…` 系统地址，深层在本地索引里走不动（实测 3/5/10 级都只有 1 个 1 级关联）——这是数据覆盖问题，不是层级逻辑问题。
- 阈值只存"最后一次保存"的一组值，没有按层级分别记忆（例如 3 级记 1/1、8 级记 10/50）。用户需求是「层级高时我会把单边最低提升」，当前做法是手动抬高后保存；要自动分档需要再定义一套映射规则。

## 2026-11-07 0.8.4 关联明细检索跳数与翻页上限验收

### 需求
「已知地址库的地址关联明细检索，最多跳数和翻页上限要和最大层级一致。」

### 实现
- 上限只有一个真源：`interlink.MAX_PAGES = MAX_RELATION_DEPTH`，`InterlinkQuery.max_hops` / `max_pages` 默认值改为 `DEFAULT_RELATION_DEPTH`。接口 `main.InterlinkInput` 两个字段都是 `ge=1, le=MAX_RELATION_DEPTH`；`service.interlink_search()` 里的 `min(3, …)` / `min(20, …)` 改成引用常量。
- 前端「最多跳数」下拉改由 `applyDepthOptions()` 生成 `2..10`（1 跳无中间地址，故从 2 起），模板里删掉写死的两个 `<option>` 并加 `data-hop-options`；「翻页上限」的 `max` 属性由 JS 写入，`runInterlink()` 不再夹回 3 / 20。
- **链路搜索重写为通用双向 BFS**（`interlink._walk_paths`）：原实现只有「1 跳找共同对手方」「2 跳挑中转」两段硬编码，参数放开到 10 也跑不出 4 跳以上。现在两岸各展开一半层数、在中间相遇，沿父指针回溯节点与逐跳转账；两岸**交替**发请求，避免一侧吃光 5 次预算导致另一侧空手。

### 本轮实测
- **深层链路正确性**（mock Taostats，budget=80）：2/3/4/5/6/7/8/9/10 跳逐级构造纯链，全部返回 `hops` 等于期望跳数，`nodes` 与构造节点序列**完全一致**，`steps` 长度等于跳数，且每步 `from` 都正好是路径上的当前节点（方向不反）。
- **不硬凑**：5 跳链配 `max_hops=2` 返回空 + 提示「没找到 2 跳以内的通路」，不会返回一条 5 跳的路径。
- **真实预算（5 次/分钟）下的行为**：纯链 2 跳 4 次、3–5 跳 5 次可完成；带旁支的 8 跳链第一轮只能展开到 3 跳左右，返回「本次预算只展开到约 3 跳，还没遇到共同地址；已取到的分页会缓存 5 分钟，再点一次会接着往深处走」。
- **多轮缓存递进是真的**：同一 8 跳链第 1 轮 5 次新请求 / 0 缓存 → 第 2 轮 5 新 + 5 缓存 → 第 3 轮拼出完整 8 跳（5 新 + 10 缓存）；10 跳链第 4 轮拼出。提示语里「再点一次能接着走」经测试验证成立。
- **接口**：`POST /api/interlink` 带 `max_hops=10 / max_pages=10` 返回 200 且 `stats.max_hops=10`；带 11 或 0 返回 422；默认不带时 `max_hops=3`。
- **前端探针**（`.probe/chk_interlink_depth_084.py`，1440×1100 真实浏览器）：`#interlink-hops` 选项 `2..10` 共 9 项、文案「中间最多 1..9 个地址」、默认 `3`；`#interlink-max-pages` 的 `max="10"`、默认 `3`；`#depth` 选项数 10、默认 `3`（两处同源）。选中上限后点检索，抓到的 payload 是 `max_hops=10 / max_pages=10`（没被前端夹回旧上限）。
- **布局**：`#view-library` 内 8 个控件同一行（`left` 305→1340 递增、`top` 2251~2270，按钮比输入框低 15px 属基线对齐），`pageOverflowX=false`。

### 验收边界
- 6 跳以上单轮 5 次请求几乎必然「只展开一部分」，这是 Taostats 免费档限速决定的，不是层级逻辑问题；界面已明确告知并给出「再点一次」的路径。
- 翻页上限只影响「组模式取 A 的流水」和「地址模式取 A↔B 直连」的页数，不影响逐跳展开（每层固定 1 页），这是原有设计，未改动。
