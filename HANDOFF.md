# Bittensor 钱包地址关联挖掘工具交接文档

## 1. 项目目标

本项目是一个 Bittensor 钱包地址关联挖掘 MVP。用户输入一个 SS58 地址后，系统从可访问的链上转账记录中查找有转账关系的地址，最多展开三级；系统按最低转账金额过滤低价值关联，并支持维护人工导入的已知地址库。当新挖掘地址命中地址库时，页面同时反馈关联地址和命中的地址库记录。

## 2. 当前实现

- FastAPI 提供页面和 HTTP 接口。
- SQLite 保存地址库、扫描任务、地址关系和交易记录。
- 支持 CSV 导入已知地址库，并对 SS58 地址做格式校验。
- 支持最新区块 RPC 扫描、交易哈希记录和地址关系展示。
- 支持最多三级关联查询；四级请求应被拒绝。
- 页面截图曾生成于 `data/ui.png`。

主要代码位置：

- `app/`：应用、数据库模型、扫描和关联服务。
- `tests/`：数据库、API、扫描及租约锁测试。
- `README.md`：启动和使用说明。
- `DESIGN_FEASIBILITY.md`：架构、可行性和数据覆盖边界。
- `VERIFICATION.md`：验收记录。

## 3. 已验证事实

当前代码已通过完整 pytest 回归；FastAPI 页面、CSV/JSON/TXT 地址库导入、三级路径、金额过滤、地址库命中、交易所/验证者过滤和四级请求拒绝均有测试覆盖。公开 RPC 的历史覆盖仍有裁剪限制；本机服务当前已健康启动。

## 4. 扫描租约锁状态

此前的回归已在本次续接中复现并修复：`Database.initialize()` 缺少建表调用，导致 `scan_lock` 表不存在；原扫描代码的注释提到 `BEGIN IMMEDIATE`，但没有实际执行该 SQL。现在初始化会创建全部 ORM 表，扫描会在租约检查前获取 SQLite 写锁，并在同一事务中创建或接管租约和扫描任务。

Windows 临时数据库测试现已在 `finally` 中调用 `database.engine.dispose()`，保证测试异常后数据库文件可以清理。

此前租约锁修复复验记录为完整 pytest **9 passed**；当前最新完整回归为 **33 passed**，具体结果见 `VERIFICATION.md`。

后续改动应至少执行：

```powershell
python -m pytest -q
```

测试或数据库模型有改动时，应重新运行以上检查，并在 `VERIFICATION.md` 记录实际结果。

## 5. 数据覆盖边界

公开 RPC 的旧历史状态可能被裁剪。Taostats 历史转账接口已作为可选数据源接入，但尚未用已认证 key 验证真实账户权限、计划覆盖范围和历史完整性；当前实现仍不能承诺任意地址的全链历史追溯。如果免费档覆盖不足，再评估归档节点或其他索引服务，并处理分页、限流、重试、缓存和数据一致性。

转账金额过滤应在配置中明确单位和阈值。阈值过滤只影响展示和扩展候选，不能据此推断地址属于同一实体。地址关联结果是链上行为关系，不是身份认定。

## 6. 建议验收顺序

1. 先检查 `app/service.py`、`app/taostats.py`、`app/relation.py` 和对应测试。
2. 运行完整测试、Python 编译检查和前端 JavaScript 语法检查。
3. 启动应用，验证页面、地址库导入/批量删除、交易所同步、金额过滤、三级查询和双过滤项。
4. 用有权限的 Taostats key 做小范围认证查询，并单独记录真实历史覆盖和额度表现。
5. 更新 `VERIFICATION.md`，只记录本轮真实运行结果，并注明 RPC/API 历史覆盖限制。
6. 交付前确认服务健康，避免留下测试 key。

## 7. 启动方式

以 `README.md` 中的命令为准。通常在项目根目录创建虚拟环境并安装依赖，然后启动 FastAPI 服务。不要把 RPC 端点可达等同于历史数据完整；部署前应配置 RPC 地址、扫描范围、最低金额阈值、请求超时和并发限制。

## 8. 交接原则

后续开发以公开可验证信息为依据，事实与推测分开记录。任何外部资料应标注来源；报告和验收记录应写明数据截止时间、RPC 或索引来源，以及无法验证的内容。

## 9. 2026-10-31 首次续接记录

首次续接时终端执行服务在创建进程前返回 `helper_unknown_error: setup refresh had errors`；Node 文件接口也因 Windows 沙箱路径映射失败而退出。当时没有读取源代码、修改应用实现或重跑测试。这是当时的工具启动状态；后续注销重启后，已通过获批的 PowerShell 执行入口恢复项目访问并完成修复。

## 10. 2026-10-31 修复交接

本次续接修复并通过测试的内容见第 4 节及 `VERIFICATION.md`。本轮没有重新扫描公开 RPC，因此 RPC 实际连通和区块采集仍以 0.1.0 验收记录为历史结果。公开 RPC 的旧历史状态仍可能被裁剪，不能据此承诺全链追溯。

## 11. 2026-10-31 Taostats 免费档接入交接

- 查询页默认勾选 Taostats 历史索引；可以取消勾选，仅查询 SQLite 本地索引。API 请求的默认值仍保持 `local`，避免旧调用方无意访问外部服务。
- 页眉主题选择器提供跟随系统、护眼暖色、护眼深灰三档；选择保存在当前浏览器，刷新后恢复。旧版暖色选择会自动迁移。
- 新客户端在 `app/taostats.py`，按官方 `/v1/transfers` schema 读取 `from`、`to`、`amount`、`block_number`、`transaction_hash`、`id` 和分页元数据。`amount` 是 u64 RAO 字符串，除以 10^9 后作为 TAO 进入图遍历。
- 关联层级通过 BFS 逐地址查询，最多 3 级；一次查询最多 5 个 API 请求。客户端进程最多 5 次/分钟，地址页缓存 5 分钟。请求预算、服务端分页窗口、坏记录、鉴权和限流错误都会让结果标记为部分/不可用。
- 设置 key：在首页“Taostats API key”密码框粘贴后点击“保存 API key”。服务只把它保存在当前进程内存，不写入文件、数据库或浏览器存储，也不会回显；重启服务后需要重新输入。点击“清除”会立即清除内存 key。
- 免费套餐公开页当前显示 $0、5 API credits/分钟、10,000/月。应用没有统计账号月度 credits；每次 API 调用具体扣点和 API 历史跨度必须用已认证 key 实测。
- `python -m pytest -q`：**20 passed**；模块编译通过。无 key 的真实官方端点请求返回 HTTP 401，确认需要 Authorization；没有做认证查询。详见 `VERIFICATION.md`。
- 下一步：通过页面设置 key 后，用已知公开交易抽样校验地址、RAO 金额、区块、交易哈希和分页；实测之前不能宣称免费档覆盖完整历史。官方历史时间控制项不一定等同 API 端点的确切保留上限。

### 当前 8766 进程状态

2026-10-31 已重启 `127.0.0.1:8766` 并加载新版本。`/api/overview` 返回 `taostats_configured: false`。Playwright 用一次性测试字符串验证密码框保存后立即清空、状态变为已配置且响应不回显 key；随后点击“清除”，状态恢复未配置。测试字符串已清除，服务没有配置真实 key。真实 key 仍待用户通过页面输入；目前没有认证请求。

Taostats. “Get Transfers.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/transfers/get-transfers. Accessed 31 Oct. 2026.

Taostats. “Taostats Pro.” *Taostats*, https://taostats.io/zh/pro. Accessed 31 Oct. 2026.

## 12. 2026-10-31 关联结果与地址库功能完成

- 查询主结果页已收敛为一张关联地址表，只展示关联钱包地址、层级、流入 TAO、流出 TAO、累计 TAO 和地址库标签；交易哈希、区块号、from/to 等交易明细不在页面渲染。
- 关联地址表新增备注列；备注保存到地址库，未在地址库中的关联地址第一次保存备注时会自动创建为 `WATCH`。
- “导出 JSON”也只导出简化关联结果，不再导出交易边明细。
- 地址库按标签折叠展示。同一个标签默认只显示标签和地址数量，点击标签后展开地址明细；地址明细的备注可以直接编辑保存。当前导入的 `DSV.txt` 和 `DSV_normalized.txt` 会显示为 `DSV` 标签。
- 地址库分类新增 `EXCHANGE` 和 `VALIDATOR`；手动添加、TXT/CSV/JSON 导入和批量删除均可使用这两类。地址库手动添加也支持初始备注。
- 新增“同步官方交易所地址”按钮和 `POST /api/library/import-exchanges`。服务调用 Taostats `/v1/exchange-addresses`，读取 `coldkey`、`name`、`icon`，去重、SS58 校验后导入 `EXCHANGE`；重复同步不会重复插入。
- 查询页新增“过滤交易所地址”和“过滤验证者地址”。交易所过滤读取地址库 `EXCHANGE`；验证者过滤合并地址库 `VALIDATOR` 与 Taostats `/v1/validators/active` 返回的明确地址。过滤地址不会继续作为下一层中转节点；起始地址命中过滤集合时直接停止扩展。
- 新增保守角色标记：地址库明确标记的交易所/验证者会显示对应标签；在当前数据范围内达到较高转账次数、对手方数量和累计金额的地址显示“疑似交易所”；只观察到转账记录且没有接入子网购买记录的地址显示“疑似中转钱包”。角色说明带有数据范围提示，不等同于身份判断。
- 用户指定地址 `5Gr1Ab7LSV4eT6RdWmVeoxnTpDBWAZFLXHHrcaWNDdjK22pp` 已加入当前数据库，分类 `EXCHANGE`，标签 `OKX交易所`，并可通过交易所过滤。
- 最新验证：`33 passed`；`compileall` 通过；`node --check app/static/app.js` 通过；8766 服务已重启。
- Playwright 实测：结果区不含交易哈希、区块号、from/to 等交易明细；本地样本查询显示关联地址、层级和金额；深灰主题可以切换；无 key 调用交易所同步接口返回 401 并提示先设置 API key。

Taostats. “Get Active Validators.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/validators/get-validators-active. Accessed 31 Oct. 2026.

Taostats. “Get Exchange Addresses.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/exchange-addresses/get-exchange-addresses. Accessed 31 Oct. 2026.

## 13. 2026-11-01 初始版本发布交接

### 发布信息

- 应用版本：`0.1.1`（初始版本为 `0.1.0`）。
- 发布仓库：<https://github.com/lmjs3256-hue/Bittensor_dizhiguanlian_wajue>。
- 发布内容：源代码、测试、中文使用说明、设计可行性、验证记录和本交接文档。
- 本地数据库、地址库、日志、缓存和运行产物不提交到仓库；克隆后首次启动会自动创建新的 SQLite 数据库。

### 当前功能基线

- 用户可以输入 SS58 地址，按方向、层级和金额阈值查找最多三级转账关联。
- 查询结果展示关联钱包地址、层级、流入金额、流出金额、累计金额、地址库标签和备注。
- 地址库按标签分组折叠；点击标签后显示该标签下的地址明细。
- 支持手动添加、TXT/CSV/JSON 导入、备注编辑和批量删除。
- 支持交易所、验证者分类和查询过滤；支持同步 Taostats 官方交易所地址目录。
- 当前运行数据库已经登记用户指定的 OKX 地址：
  `5Gr1Ab7LSV4eT6RdWmVeoxnTpDBWAZFLXHHrcaWNDdjK22pp`，分类为 `EXCHANGE`，标签为 `OKX交易所`。该记录属于本机运行数据，不会随源码仓库发布。
- 自动角色提示包括明确交易所、疑似交易所和疑似中转钱包。自动提示只反映当前可见数据，不构成身份或所有权判断。

### 交接后的启动检查

```powershell
Set-Location G:\bittensor-wallet-link-intel
& 'G:\jiaoben\.venv\Scripts\python.exe' -m pytest -q
& 'G:\jiaoben\.venv\Scripts\python.exe' -m compileall -q app tests
node --check app\static\app.js
.\start.ps1
```

## 14. 2026-11-01 结果筛选与交易所判定改进交接

### 已完成

- 应用版本更新为 `0.1.1`；当前变更仍在本地工作树，尚未提交 GitHub。
- 关联地址行增加“复制”按钮，复制完整 SS58 地址；浏览器剪贴板不可用时显示提示。
- 关联结果增加“全部层级、只看 1 级、只看 2 级、只看 3 级”筛选。筛选在浏览器内执行，不重复消耗 Taostats 请求。
- 交易所人工状态增加 `exchange_rejected`：关联结果和地址库均可确认、否定或重置；只有 `exchange_confirmed=true` 才可过滤，否定记录不会被自动重新识别。
- SQLite 启动迁移会为旧库补充 `exchange_rejected` 列。人工状态不写入用户 API key，也不外发地址库内容。
- 自动疑似交易所判定改为近 30 天带时间戳转账、高频对手方、金额和当前余额联合条件。余额通过公开 Bittensor RPC 的 `System.Account` 查询；低于 100 TAO或余额不可读时不会自动标记为交易所。每次最多检查 5 个候选。
- 已只读核验用户举例地址当前余额：`10000 Rao = 0.00001 TAO`，因此不满足交易所疑似余额门槛。该值是 2026-11-01 的当前快照，不代表历史余额。

### 当前事实与限制

- Taostats 新版 `/v1/transfers` 文档公开的是分页窗口和区块/时间范围参数，没有证实“免费 API 固定只保存 7 天”。一两个月低频交易不能因短查询窗口直接判定交易所；免费账户历史范围必须用用户自己的认证响应实测。
- 当前工具没有完整的子网质押、解质押、购买和代理事件语义；“疑似中转钱包”只是当前可见转账行为提示，不能证明该地址没有其他链上活动。
- 当前余额为查询时点 free balance，不能代表历史余额；交易所最终结论仍由人工确认或否定决定。

### 最新验收

- `38 passed, 1 warning`。
- `compileall` 和 `node --check app/static/app.js` 通过。
- 服务已重启并在 `http://127.0.0.1:8766` 健康运行；无 JavaScript error。
- 人工否定接口已实测返回 `exchange_confirmed=false`、`exchange_rejected=true`，随后已恢复测试记录的原确认状态。
- 外部资料来源和 MLA 条目已同步到 `README.md`、`VERIFICATION.md`。

## 15. 桌面快捷启动与发布状态

- 已新增 `launch_desktop.ps1`：调用 `start.ps1` 启动或复用 8766 服务，然后打开 `http://127.0.0.1:8766`。
- 已新增 `create_desktop_shortcut.ps1`：创建或更新桌面快捷方式。
- 当前桌面快捷方式已创建并验证：
  `C:\Users\gzx\Desktop\Bittensor 地址关联挖掘.lnk`
- 快捷方式工作目录为 `G:\bittensor-wallet-link-intel`。项目移动后应重新执行：

```powershell
Set-Location G:\bittensor-wallet-link-intel
.\create_desktop_shortcut.ps1
```

- 当前工作树包含本轮代码、文档和启动脚本改动，远程为 `origin/main`。
- GitHub 发布使用本机已有 Git Credential Manager 认证，通过 `git push` 发布；不要求安装 GitHub CLI。

## 16. 交易所公共地址终点与个人中转识别

- 应用版本更新为 `0.1.2`。
- 地址库中 `EXCHANGE` 且 `exchange_confirmed=true` 的地址现在同时具有“可过滤地址”和“关联终点”两个属性。即使没有勾选交易所过滤，扫描也会保留交易所公共地址本身，但不会查询或扩展它的下游转账。
- 与已确认交易所公共地址直接发生转账的普通地址会显示“疑似中转钱包（交易所关联）”。该判断不要求转账次数达到阈值，也不要求余额高于交易所余额门槛，因此一笔转账、低余额地址也能显示这个线索。
- 该角色的证据范围是当前查询数据：本项目当前转账数据源只解析 `Balances.Transfer`，所以“未观察到子网交易语义”不能替代全链历史子网交易核验。
- 新增回归覆盖：本地关系发现的交易所终点、单笔交易的交易所关联中转地址，以及 Taostats 查询不继续请求交易所公共地址。

打开 `http://127.0.0.1:8766` 后，先在页面输入 Taostats API key，再选择是否使用 Taostats 历史索引。API key 服务重启后需要重新输入。

## 17. 页面版本显示与更新日志

- 应用版本更新为 `0.1.3`。
- 页面底部固定显示当前版本号，并提供“查看更新日志”快捷入口。
- 侧边栏新增“更新日志”页面，展示 `CHANGELOG.md` 中的版本、日期和功能变更。
- 后端新增 `/api/version`，统一读取项目根目录的 `VERSION` 和 `CHANGELOG.md`；健康接口、页面版本号和更新日志使用同一来源。
- 新增 `tests/test_version.py`；本轮验证为 `43 passed`，另有 1 条既有 Starlette/httpx 弃用 warning。

## 18. 地址库导出与设备迁移

- 应用版本更新为 `0.1.4`。
- “已知地址库”页面新增“导出 CSV”和“导出 JSON”按钮。导出内容包含地址、标签、分类、备注、交易所人工确认和人工否定状态，不包含本机数据库 ID 和时间戳。
- 新设备启动应用后，在“已知地址库”页面点击“导入文件”即可导入导出的 CSV/JSON；导入逻辑会恢复交易所人工状态。
- 本轮新增地址库备份格式回归测试。

## 19. 余额和地址显示优化

- 应用版本更新为 `0.1.5`。
- 关联查询会读取起始地址和最终关联地址的当前 `System.Account` free balance；页面余额显示保留两位小数，读取失败显示“未获取”。
- 关联表的流入、流出、累计转账 TAO 统一显示两位小数。
- 关联地址显示为前四位和后四位，中间使用省略号隐藏；复制按钮保存完整地址并复制完整 SS58 字符串。
- 当前余额查询是只读 RPC 查询，不计入 Taostats API 请求次数；如果 RPC 无法读取，页面会保留关联结果并提示余额未获取。

### 已知风险和后续工作

1. 公共 RPC 历史状态可能被裁剪，本地索引不能代表全链历史。
2. Taostats 免费档历史跨度、接口 credit 消耗和真实账号额度需要使用用户自己的 key 验证。
3. 自动交易所识别目前使用明确地址库标记和当前查询范围内的转账行为特征；完整余额和子网购买记录还需要接入更多数据源。
4. 如果需要多人协作，下一步可以在 GitHub 上补充 Issue 模板、贡献指南和 CI 测试流程。

## 14. 2026-11-01 新规则交接

- 默认单边和累计金额阈值均为 `1` TAO；接口默认值、页面输入框和 `LinkPolicy` 已统一。
- 自动交易所识别只产生“疑似交易所（待确认）”线索。当前转账索引使用高转账次数、高对手方数量和高可观察转账总额作为候选条件；余额与子网质押/解质押事件尚未接入，因此页面明确显示证据边界。
- 只有地址库中 `EXCHANGE` 且 `exchange_confirmed=true` 的地址才会进入过滤集合。扫描结果提供“确认交易所并允许过滤”按钮，确认后写入 `manual:exchange-confirmation`；未确认候选即使勾选过滤也会保留。
- `/api/relations` 增加 `seed_library_matches`，起始地址命中时页面高亮具体标签，例如 `DSV地址库`；关联表的地址库命中也显示为“标签地址库”。
- SQLite 增加 `api_requests` 表，持久化本机实际发起的 Taostats 网络请求。`/api/overview` 和关联结果返回今日、本月、累计、本月参考上限（10,000）及剩余参考值；缓存命中不计数，账户实际 credits 以 Taostats 控制台为准。
- `start.ps1` 在项目没有 `.venv` 时会优先使用 `BITTENSOR_PYTHON`，否则使用当前机器的 `G:\jiaoben\.venv\Scripts\python.exe`，再回退到系统 Python。
- 本轮验证：`35 passed`；`compileall` 和 `node --check app\static\app.js` 通过；重启后的 8766 服务返回健康状态，首页显示默认 `1` TAO 和查询统计，人工确认接口返回 HTTP 200。

## 20. 钱包总价值与质押估值

- 应用版本更新为 `0.1.6`。
- `app/collector.py` 新增钱包总价值读取：先读取 `System.Account.data.free`，再调用官方 `StakeInfoRuntimeApi.get_stake_info_for_coldkey`，并按 `SwapRuntimeApi.current_alpha_price` 将各子网 Alpha 折算为 TAO。
- 页面起始地址显示钱包总价值、可用 TAO 和已质押折合 TAO；关联地址表的余额列改为钱包总价值，悬停可查看可用和已质押组成。
- `balance_status` 使用 `complete`、`partial`、`unavailable` 三种状态。质押查询失败时不把质押值当作零，页面显示“部分获取”或“未获取”。
- 总价值是当前链上估值快照，受当前 Alpha/TAO 价格和 RPC 可用性影响，不是历史平均价格或法币价值。
