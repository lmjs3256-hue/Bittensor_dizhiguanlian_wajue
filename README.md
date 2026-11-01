# Bittensor 地址关联挖掘工具

版本：**0.1.6**

这是一个独立运行的 Bittensor 地址关联分析工具。输入公开地址后，工具可以调用 Taostats 历史转账索引，或查询本地 SQLite 索引，查找最多三级的普通 TAO 转账关系。工具会把关联地址与人工维护的已知地址库匹配，并支持交易所、验证者和备注管理。

- 技术栈：FastAPI、SQLite、原生 HTML/CSS/JavaScript、`async-substrate-interface`
- 默认服务：<http://127.0.0.1:8766>
- 独立数据库：`data/wallet_link.db`
- 默认数据源：`wss://entrypoint-finney.opentensor.ai:443`

项目仓库：<https://github.com/lmjs3256-hue/Bittensor_dizhiguanlian_wajue>

## 已实现

1. 输入 SS58 地址，最多执行三级 `Balances.Transfer` 关联扫描。
2. 支持流入、流出、双向查询，以及单边金额与关系累计金额阈值。
3. 第一跳支持低于阈值的金额，第二、三跳必须通过单边金额门槛；系统账户默认排除。
4. 人工导入 TXT、CSV、JSON 地址库；支持 SEED、WATCH、BLACKLIST、EXCHANGE、VALIDATOR 分类。
5. 查询结果只显示关联钱包地址、层级、流入/流出/累计金额和地址库标签，不展示交易明细；导出 JSON 也采用同一简化字段。
6. 写入 SQLite 的转账均按事件去重，保留区块号、区块哈希、交易哈希与事件序号。
7. 扫描只读取公开链上事件，不签名、不转账、不修改钱包。
8. 可选调用 Taostats `GET /v1/transfers`，按地址扩展关系；页面显示单次请求数、分页状态、数据来源和部分结果提示。
9. 页面主题支持跟随系统、暖色护眼和深灰护眼，并在当前浏览器本地记住选择。
10. 地址扫描可以分别勾选过滤交易所和验证者；只有已人工确认的 `EXCHANGE` 地址才会过滤，未确认的疑似交易所会保留在结果中。
11. 地址库支持“同步官方交易所地址”；同步使用 Taostats `GET /v1/exchange-addresses`，也可以手动添加或导入交易所/验证者地址。
12. 关联地址和地址库地址都可以直接填写备注；地址库按标签折叠，同一标签默认只显示标签名称，点击后展开地址明细。
13. 已知交易所地址会显示明确标签；满足近 30 天高频转账、对手方多、可观察金额高且当前 free balance 达到门槛的地址，页面才会显示“疑似交易所（待确认）”，确认后才允许过滤；只观察到转账活动的地址，会显示“疑似中转钱包”。子网质押/解质押和购买事件仍未完整接入，这些自动标记都属于线索，不是身份结论。
14. SQLite 持久化统计本机实际发起的 Taostats 请求，页面显示今日、本月、累计、本月参考上限和本月剩余；缓存命中不计数。

15. 关联结果的地址后提供“复制”按钮；结果表支持全部、1 级、2 级、3 级筛选，筛选只改变当前显示，不重新消耗 API 请求。
16. 疑似交易所可以在关联结果或地址库中人工“确认交易所”“否定交易所”或重置结论。只有确认状态才允许交易所过滤；否定状态会持久化并阻止后续自动候选提示。
17. 自动交易所候选增加保守条件：只统计有时间戳的近 30 天转账，先读取 Bittensor `System.Account` 当前 free balance；余额低于 100 TAO、余额读取失败或缺少时间证据时，不自动认定为疑似交易所。当前余额查询最多检查 5 个候选，避免额外 RPC 请求失控。
18. 钱包余额显示钱包总价值：可用 TAO 加各子网质押 Alpha 按当前 Alpha/TAO 价格折合的 TAO；页面同时显示可用和已质押折合部分。质押运行时接口不可用时显示部分获取或未获取，不把可用余额误称为总价值。

### 钱包总价值读取口径

应用通过 Bittensor 官方 `StakeInfoRuntimeApi.get_stake_info_for_coldkey` 读取 coldkey 的质押位置，再通过 `SwapRuntimeApi.current_alpha_price` 把每个子网 Alpha 折算为 TAO。子网 Alpha 不会直接相加。总价值公式为：`可用 TAO + Σ(Alpha 数量 × 当前 Alpha/TAO 价格)`。运行时接口不可用时，页面保留可用余额并标记为“部分获取”，不会把可用余额伪装成总价值。
18. 已确认的交易所公共地址是关联终点：结果保留交易所地址，但不会继续查询或展开该地址的下游钱包。与交易所公共地址直接发生转账的普通地址，即使只有一笔记录或余额为零，也会标记为“疑似中转钱包（交易所关联）”。
19. “已知地址库”页面支持导出 CSV 和 JSON，包含地址、名称、分类、备注及交易所人工确认/否定状态，可在其他设备通过导入文件恢复。
20. 扫描结果显示起始地址和关联地址的当前钱包余额；金额显示保留两位小数，关联地址默认隐藏为前四位和后四位，复制按钮仍复制完整地址。

## 快速开始

在 Windows PowerShell 中运行：

```powershell
Set-Location G:\bittensor-wallet-link-intel
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py --host 127.0.0.1 --port 8766
```

浏览器打开 <http://127.0.0.1:8766>。也可以使用项目内的 `start.ps1` 和 `stop.ps1` 管理后台服务。

### 桌面快捷启动

项目提供桌面快捷方式“Bittensor 地址关联挖掘”。双击后会自动启动本地服务并打开浏览器页面；如果服务已经运行，快捷方式会复用现有服务。快捷方式指向项目中的 `launch_desktop.ps1`，项目移动到其他目录后需要重新创建。

创建或更新快捷方式：

```powershell
Set-Location G:\bittensor-wallet-link-intel
.\create_desktop_shortcut.ps1
```

快捷方式目标为 `C:\Users\gzx\Desktop\Bittensor 地址关联挖掘.lnk`。如果桌面快捷方式被安全策略拦截，可以直接运行 `start.ps1`，然后访问 <http://127.0.0.1:8766>。

首次启动会自动创建 `data/wallet_link.db`。数据库、日志、缓存和本地地址库属于运行数据，默认不会提交到 Git 仓库。

## 启动

```powershell
Set-Location G:\bittensor-wallet-link-intel
& 'G:\jiaoben\.venv\Scripts\python.exe' run.py --host 127.0.0.1 --port 8766
```

打开 <http://127.0.0.1:8766>。

### 启用 Taostats 免费档

1. 在 Taostats 官方网站注册并在账户中创建 API key。免费档标价为 $0；官方套餐页当前列出每分钟 5 API credits、每月 10,000 API credits。实际可用历史范围和单次端点 credit 消耗，以你的账户控制台及 API 响应为准。
2. 打开工具首页，在“Taostats API key”密码框中粘贴 key，点击“保存 API key”。页面只显示是否已配置，不会回显已保存的内容。

应用只把页面提交的 key 保存在当前服务进程内存，不写入数据库、配置文件或浏览器存储；刷新页面后仍显示“已配置”，但不会再次显示 key。服务重启后需要重新输入。点击“清除”会立即停用当前服务中的 key。`/api/overview` 返回 key 是否已配置和本机请求统计。Taostats 查询每次最多发起 5 个 API 请求，并在应用进程内将同一地址同一页缓存 5 分钟；本机统计会跨服务重启保留，但不等同 Taostats 账户 credits 余量。未配置 key、遇到服务端限流或查询超出本轮预算时，界面会保留已取得的结果并标为部分结果/不可用。

如果页面提示“后台版本较旧”，请先重启应用；重启后直接在首页输入 key，不需要设置 PowerShell 环境变量。

如需在后台启动：

```powershell
.\start.ps1
```

日志保存在 `logs/`。停止服务：

```powershell
.\stop.ps1
```

## 地址库文件格式

TXT：每行一个地址，`#` 之后可写备注。TXT 导入的地址默认归为 `WATCH`。

```text
5ColdkeyAddressHere # team treasury
```

CSV（导出地址库使用相同字段）：

```csv
address,label,category,note
5ColdkeyAddressHere,Team treasury,SEED,verified source
```

应用导出的地址库 CSV 还会包含 `exchange_confirmed` 和 `exchange_rejected` 两列，用于迁移交易所人工判断；JSON 以 `addresses` 数组保存相同字段。

JSON：

```json
{
  "addresses": [
    {
      "address": "5ColdkeyAddressHere",
      "label": "Team treasury",
      "category": "WATCH",
      "note": "source and evidence"
    }
  ]
}
```

地址库分类：`EXCHANGE` 用于已人工确认的交易所地址，`VALIDATOR` 用于验证者地址。CSV/JSON 可以直接写入 `category`；TXT 默认导入为 `WATCH`。关联结果中的“疑似交易所（待确认）”可点击确认，确认后才进入交易所过滤集合。地址库页支持勾选多条记录后批量删除。同步官方交易所地址前，需要先在首页填写 Taostats API key；同步结果会去重、校验 SS58，并标记来源为 `taostats:/v1/exchange-addresses`。

## 索引和查询口径

- 边：仅 `Balances.Transfer`，方向为 `from → to`，金额单位为 TAO。
- 第一跳：用于定位资金邻居，低额转账仅作为探索线索，不展示为保留边；关系仍需达到累计金额门槛。
- 第二、三跳：单边金额必须达到门槛，累计金额必须达到关系门槛。
- 共享对手方、相同时间、质押、swap 和代理调用没有被自动当成资金关联。
- 勾选 Taostats 时，查询只覆盖该 API key、Taostats 服务端分页窗口和本轮成功返回的记录；应用会标明请求分页是否完成。分页完成不等于数据源包含全链历史，也不代表账户计划未限制历史范围。
- 截至 2026-11-01，Taostats 新版转账文档没有把 `/v1/transfers` 概括为“免费档只保留 7 天”。文档公开说明的是分页最多触达 1,000,000 行，超过范围要使用 `block_start/block_end` 或 `timestamp_start/timestamp_end`；因此一两个月没有交易的地址不能仅凭一次短窗口结果判断为交易所。免费计划的实际历史范围、每个端点 credits 消耗和账号权限仍以账户控制台与认证响应为准。
- 关闭 Taostats 时，查询只覆盖本地索引的转账记录，不代表全链历史。
- 一笔转账是行为证据，不是同一控制人、真实身份或共同受益人的证明。
- 已确认交易所地址只作为公共地址终点；“交易所关联疑似中转钱包”只表示当前查询数据中存在直接转账关系。当前查询数据只有 `Balances.Transfer`，未观察到子网交易语义，不能证明该地址在全链历史中绝对没有子网交易。

## 历史数据限制

公开 RPC 的当前节点可以读取最新区块事件，但历史运行时状态会被裁剪。已验证超过一定区间后出现 `State already discarded`。因此：

- 短期增量分析：当前实现可以直接使用。
- 连续更新：按新区块顺序执行“扫描最新”或“继续水位”。
- 全历史三级追溯：须接入保留完整历史状态的归档节点，或可靠的第三方索引服务，并重新验证其历史完整性。
- 本工具不会将稀疏本地索引包装成完整历史结论。

## 发布文件说明

- `VERSION`：当前应用版本。
- `CHANGELOG.md`：版本变更记录。
- 页面底部会显示当前版本号；点击“查看更新日志”或侧边栏“更新日志”，可以查看各版本的发布日期和功能变更。
- `HANDOFF.md`：中文开发交接和运行状态。
- `DESIGN_FEASIBILITY.md`：技术方案和可行性边界。
- `VERIFICATION.md`：实际测试与验收记录。
- `app/`：FastAPI 后端、关联算法、Taostats 客户端和前端页面。
- `tests/`：单元测试和 API 测试。

仓库只保存源代码和文档。`data/`、`logs/`、Python 缓存和测试缓存已加入 `.gitignore`，避免把本地数据库、运行日志或用户地址库直接发布。

## 测试

```powershell
$env:WALLET_LINK_DB = 'G:\bittensor-wallet-link-intel\data\test.db'
& 'G:\jiaoben\.venv\Scripts\python.exe' -m pytest -q tests
```

测试覆盖事件提取、`Withdraw`/`Deposit` 去噪、交易哈希映射、三级上限、低额中转防伪链、金额累计过滤、地址库命中与文件导入；Taostats mock 覆盖 RAO 转换、分页、缓存、限速、鉴权失败、三级扩展和地址库命中。自动化 mock 测试不代表已用用户 API key 验证真实账户权限或历史覆盖。

## 外部来源（MLA）

Taostats. “Get Transfers.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/transfers/get-transfers. Accessed 1 Nov. 2026.

Taostats. “Get Active Validators.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/validators/get-validators-active. Accessed 1 Nov. 2026.

Taostats. “Get Exchange Addresses.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/exchange-addresses/get-exchange-addresses. Accessed 1 Nov. 2026.

Taostats. “Live: Get Free TAO Balance.” *Taostats API Documentation*, https://taostats.io/zh/docs/api-reference/rpc-live/get-live-accounts-address-balance-info. Accessed 1 Nov. 2026.

Taostats. “Taostats Pro.” *Taostats*, https://taostats.io/zh/pro. Accessed 1 Nov. 2026.
