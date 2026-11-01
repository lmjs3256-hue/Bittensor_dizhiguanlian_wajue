# 0.1.0 验证交付记录

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
