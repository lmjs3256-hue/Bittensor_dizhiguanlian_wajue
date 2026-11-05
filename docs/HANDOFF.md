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

## 20. 2026-11-04 0.6.x 交接：离线授权、地址库分类、多副本启动

> 0.2.0–0.6.1 的功能细节见 [`CHANGELOG.md`](../CHANGELOG.md)；本节只记录当前状态和交接要点，避免与上面的 0.1.x 历史记录混淆。

**当前状态**

- 应用版本 `0.6.2`；本机 `127.0.0.1:8766` 运行中，`/api/health` 返回 `version`、`licensed`、`team`、`pid`、`root`。
- 回归：`101 passed`；`node --check app/static/app.js` 通过；`start.ps1`／`launch_desktop.ps1`／`stop.ps1` 经 `[Parser]::ParseFile` 校验无语法错误。
- 代码位置：`app/license.py`（授权）、`app/service.py`（地址库/分类）、`tools/issue_license.py`（签发）、`tests/test_license.py`、`tests/test_address_books.py`、`tests/test_categories.py`。

**离线授权（0.6.0 起）**

- 机器码 = Windows `MachineGuid` → SHA-256 → base32 取前 16 位，显示成 `XXXX-XXXX-XXXX-XXXX`；**与安装路径、磁盘、用户名无关**，重装系统才会变。
- 授权码格式 `WL1.<payload>.<sig>`，Ed25519 签名；程序内只有 `PUBLIC_KEY_B64`，私钥在仓库外 `.license-keys/wallet_link_private.pem`，不进 Git、不进安装包。
- 拦截点：`app/main.py` 的 `license_guard` 中间件，非 `PUBLIC_PATHS` 且未授权 → 403 `code=unlicensed`。测试用 `tests/conftest.py` autouse 打桩放行业务用例，`tests/test_license.py` 的 `real_gate` 单独验证真实拦截。
- 授权粒度：**默认一机一码**；同一个码里可以写多个机器码共用（`issue_license.py` 限制 ≤50 个），`--machine "*"` 表示不绑机器。这里的 50 是“单码机器码上限”，不是“一个码自动能发 50 个人”。
- 其它：系统时间回拨超过 24 小时拒绝启动（`data/.license-clock`）；页面右下角、TXT/JSON 导出都带“团队名 · 机器码后 6 位”水印。

**地址库分类（0.5.0 起）**

- 人工分类：大V／群友／其他，可自定义新增（≤12 字），存 `address_categories`；`EXCHANGE`／`VALIDATOR` 是系统分类，只用于查询过滤，不出现在人工下拉。
- 地址库台账 `address_books` + `address_library.book`；解散地址库只清 `book` 标记，不删地址记录。

**启动脚本与同机多副本（0.6.2）**

- `/api/health` 的 `pid`／`root` 用来判断端口上跑的是“本目录的服务”还是“另一个副本”。
- `start.ps1` 发现端口被别人占用时，自动向后找 10 个空闲端口（`TcpListener` 试绑），实际端口写入 `logs/wallet-link.port`。
- `launch_desktop.ps1` 读这个文件打开正确页面；`stop.ps1` 不带参数时优先用这个端口停，避免误停原副本；`启动.cmd` 支持 `启动.cmd 8777`。
- 同一台电脑上的多份副本是两套独立数据（各自的 `data\wallet_link.db`）；机器码相同，`license.key` 拷过去直接可用。

**发布状态**：`6fa8881`（0.6.2）已推送到 <https://github.com/lmjs3256-hue/Bittensor_dizhiguanlian_wajue>。

**后续可做**

1. 需要更强防破解时再考虑 PyInstaller／PyArmor 打包（会改变“拷文件夹即用”的部署方式）。
2. 离线授权无法远程吊销，只能等到期；要即时吊销必须引入联网校验。
3. 单码最多 50 个机器码，团队规模更大时按批次拆码，并在团队名里带成员标识以便溯源。


## 21. 2026-11-05 0.6.13–0.6.17 交接：分页、标签组、预置地址库

**分页与路径定向（0.6.13）**

- 关联结果分页：`state.page` / `state.pageSize`，每页可选 10 / 20 / 50 / 100（默认 10），首页 / 上一页 / 输入页码跳转 / 下一页 / 末页，页码写进 `localStorage` 记忆；越界自动回末页。实现见 `app/static/app.js` 的 `relatedTable()` / `refreshRelatedTable()`，样式在 `.pagination*`。
- 路径明细定向：`app/relation.py` 的 `_oriented_path(path, seed)` 从发起扫描的地址出发逐跳推导方向，输出 `path_from` / `path_to` / `direction`（`out` 向外转出 / `in` 对方转入），保证每一行明细的起点都是扫描起始地址。
- 交易所终点剪枝：地址库里 `exchange_confirmed` 或带「系统」分类的地址在 discover 中不向下扩展；路径中途经过终点的下游地址整体剔除，并返回 warning「已剪除 N 个只经由交易所/系统地址连通的地址」。结果行上的「标为交易所」会即时隐藏下游关联，刷新后由后端规则彻底移除。

**标签默认值与整组改名（0.6.14 → 0.6.15）**

- `default_address_label(address)` 取地址后四位；`address_dict()` 额外返回 `label_custom` 区分「人工命名」与「默认名」，前端用 `.label-text.is-default` 虚线弱化。
- 0.6.15 起**取消单条改名**：只保留组级改名（`POST /api/library/rename-label`、`POST /api/library/books/{name}/rename`，前端 `startGroupRename(kind, name)` / `saveGroupRename()`），单条地址只改备注。地址库清单按 `label` 归并分组，同组允许不同分类。

**同步合并与撤回（0.6.15）**

- `sync_relation_addresses(..., book_name)` 可把「新关联地址 + 发起地址」去重合并进**已有地址库**（label 默认跟随库名），没命中时才新建库；前端 `sync-label-group` 下拉把自建库、人工标签组、新建三合一，命中时默认选中命中组。
- `ImportBatch`（单槽 `id=1`）+ `_library_row_snapshot()` + `_record_import_batch()` 在 sync / book / import_rows / 解散 / 重新导入五个入口写快照；`undo_last_import()` 删除新插入地址、按快照还原字段、移除本次新建的库。

**预置地址库与解散语义（0.6.16）**

- `app/seed_books/<组名>.txt` 随包分发：一只羊 54、DFV 1、惩罚者 16、DSV 239、埋伏党 38、taoBot 7，共 355 个地址。`app/db.py` 的 `install_seed_books()` 首次启动导入并登记到 `seed_books` 表（幂等，解散后不复活），地址库里已有的地址一律跳过。`main.py` lifespan 调用，`SEED_BOOKS_ENABLED` 可关闭（conftest 关闭以免污染测试）。
- 解散语义变更：`dissolve_address_book()` 从「只清库名」改为**连库带地址一起删除**，并写撤回快照（`deleted` / `removed_book`），可用「撤回上次导入」恢复。
- 组内全选：组标题右侧 `[data-group-select]` + 「已选 N」计数，`updateLibrarySelection()` 同步组内全选 / 半选，与顶部「全选」和单条勾选联动。

**重新导入预置库（0.6.17）**

- `install_seed_books(..., force=True)` 无视 `seed_books` 安装记录，只补当前缺失的地址（解散过的组连库带地址重建），返回 `new_addresses` / `created`；`list_seed_files()`、`seed_book_status()` 提供文件概况。
- 接口：`GET /api/library/seed-books`、`POST /api/library/seed-books/reinstall`；`_record_import_batch` 新增 `created_books` 支持一次记多个新建库，`undo_last_import()` 批量删库。
- 结果写 `kind="seed"` 快照，summary 带每组 +N；无缺失时不写快照，避免覆盖上一条可撤回记录。

**验证状态（0.6.17）**

- `pytest -q`：**142 passed**（1 条 Starlette/httpx 弃用 warning）。
- `node --check app/static/app.js` 通过；`使用说明.html` 由 `tools/build_usage_html.py` 重新生成。
- 真实库实测：解散 DFV → 重新导入补回 1 条并重建库 → 撤回删除 1 条并移除库 → 再导入复原。
- CDP 界面探针实测：工具栏按钮存在、文案与提示正确、无溢出裁剪。
- 外发包 39.6 MB / 79 文件（含 6 个 seed_books 文件）。

## 22. 2026-11-05 0.6.18 交接：改分类也是整组操作

- 单条地址不再能改分类（`data-edit-categories` 已从行模板移除），入口统一到标签组标题右侧的「改分类」，与「改名」并列；未命名的分类兜底组不显示该入口。
- 新方法 `LinkService.update_group_categories(categories, book=|label=)`：按 `book` 或 `label` 定位标签组，整组改 `category` / `categories`；交易所/验证者地址（系统分类或 `exchange_confirmed`/`exchange_rejected`）自动跳过，全组都是这类地址时抛 `InputError`。
- 改动写 `ImportBatch(kind="category")` 快照（`updated` 里存 `_library_row_snapshot` 旧值），`undo_last_import()` 可整组还原。
- 新接口 `POST /api/library/group-categories`（`GroupCategoriesInput`：`categories` + `book` / `label`）。单条 `PATCH /api/library/{id}/categories` 保留但界面不再调用。
- 前端：`state.groupCategory = {kind, name}` 取代 `state.categoryEditorId`；编辑器整行渲染在组标题下（`.group-category-editor`），默认只勾该组主分类（`group.primaryCategory`），避免"其他 + 群友"这种误叠加。

## 23. 2026-11-05 0.6.19 交接：删除类操作的前置确认

- 本次只改前端确认文案与一处缺失确认，后端接口与数据模型未变，无需迁移。
- 新增三个辅助函数（`app/static/app.js` 地址库区块）：`libraryGroupOf(row)`（按 `book` → `label_custom` → 分类兜底组算组名）、`selectionSummary(ids)`（批量选择按组汇总，最多列 6 组，其余折叠为“其余 N 个组共 M 条”）、`deleteAddressConfirmText(row)`（单条删除显示地址前后各四位 / 名称 / 所属库 / 分类 / 备注摘要）。
- `deleteLibraryAddress` 先用 `state.library` 找到该行再拼确认文案；`#delete-selected` 用 `selectionSummary` 生成分组清单，并提示“批量删除不可撤回，建议先导出备份”。
- `deleteCategory` 补上该分类当前地址数；`clearTaostatsKey` 补上确认框（此前点击即清除）。
- 已有确认（解散地址库、清空/删除单条查询记录、撤回上次导入、整组改分类）保持不变。
- 回归：`pytest -q` 全部通过；`node --check app/static/app.js` 通过。`app/templates/index.html` 资源戳升到 `v=0.6.24`，避免浏览器沿用旧 JS。

## 24. 2026-11-05 0.6.20 交接：关联地址「金额筛选」

- 目标：关联地址太多太杂时，除层级筛选外还能按流入/流出 TAO 滤掉金额很小的“轻微关联”。纯前端过滤已有结果，不改后端接口、不重新查询。
- `state.amountFilter = {mode, min}`（`mode` ∈ `off|either|incoming|outgoing|total|both`），持久化在 `localStorage["wb.relatedAmountFilter"]`；`loadAmountFilter()` / `saveAmountFilter()`。
- 判定函数 `rowPassesAmountFilter(row)`：`either` 取 `max(in, out) >= min`；`both` 要求两个方向都达标；`total` 用 `total_tao`（缺失时退化为 in+out）；阈值 ≤ 0 或 `mode="off"` 视为不筛。
- `relatedVisibleRows()` 拆成 `relatedBaseRows()`（层级/手动移除/交易所剪枝）+ 金额过滤，`refreshRelatedTable()` 用两者差值渲染提示 `#related-filter-hint`（隐藏条数 + 显示 X / Y）。
- 控件：`#related-amount-mode`（下拉）、`#related-amount-min`（数字，250ms 防抖）、`#related-amount-reset`；层级或金额变化都会把页码重置到第 1 页。
- 导出：「导出地址 TXT」按当前金额筛选过滤并 toast 说明隐藏条数；「导出 JSON」保持全量快照（有意为之，便于回看被筛掉的地址）。
- 界面探针 `.probe/ui_check_0620.py`：8 条构造数据下 either/incoming/outgoing/total/both 分别 4/4/1/5/1 条，层级叠加（2 级 + either≥10）= 1/3，重置回 8 条，localStorage 记忆正确；`.probe/layout_0620.py`：1422px 下四个控件同一行、ySpread=0、无重叠、右边界 1331 < 面板 1355。

## 25. 2026-11-05 0.6.21 交接：交易所改判 + 组内手动加地址

- **改判**：`LinkService.decide_exchange_address(address, decision, label="")` 的 `label` 默认值从「人工确认交易所」改为空串——前端不再强行传名字，人工起的组名不会被冲掉。仅当 `label` 有值，或现有 label ∈ {"", "人工确认交易所", "人工否定交易所"} 时才替换。
- `reset` 现在会把 `source` 记为 `manual:exchange-reset`，前端把 `address_role` 复位成「疑似交易所（待确认）」并给出对应 toast。
- 前端 `exchangeDecisionControl(row)` 统一渲染三态控制：已确认（改为否定/重置为待确认）、已否定（改为交易所/重置为待确认）、待确认（确认/否定）、未入库（标为交易所）；`relatedTable` 与地址库表格共用同一套说法。
- **组内加地址**：`LinkService.add_group_addresses(addresses, book=, label=, category=, note=)`——按 `[\s,，;；、]+` 切分、去重、逐个 SS58 校验；**仅新增**，已存在的地址跳过且不覆盖分类/组名/备注；写 `kind="import"` 撤回快照。接口 `POST /api/library/group-addresses`（`GroupAddressInput`）。
- 前端：`state.groupAdd` 打开输入框，按钮 `[data-group-add]` 渲染在 `.library-group-details` 顶部（未展开不可见）；`saveGroupAdd` 按组类型拼 payload——自建库带 `book`+`label=库名`，命名标签组带 `label`，未命名分类组只带 `category`。
- 测试：`tests/test_group_add_and_exchange.py`（6 个）覆盖多分隔符/去重、跳过已存在不动原记录、无效地址报错、撤回、改判来回切换、保留人工组名、接口层。全量 `pytest -q` = **150 passed**。
- 界面探针：`.probe/ui_check_0621.py` 建临时库 → 展开 → 添加（1 好 1 坏）→ toast「已加到「临时添加测试」1 个地址，1 个格式不对」→ 组内 1→2 条、备注写入 → 解散清理；`.probe/ui_check_0622.py` 结果表三态按钮 & 真实库中两条被否定的地址（5EiXej…、5DunDr…）在 rrik 组里均显示「改为交易所」「重置」。

## 26. 2026-11-05 0.6.22 交接：组内添加支持「转入本组」+ 交易所结论组同步标志

- 起因：用户把两个已被否定的地址（5EiXej…、5DunDr…，当时在 `rrik` 自建库）用「＋ 手动添加地址」加到「人工确认交易所」组，界面上看不到新增——`add_group_addresses` 原本「已存在一律跳过」，只在 toast 里报一句“跳过已存在 N 个”，既没说是哪个组，也没有转移手段。
- 后端（`app/service.py`）：新增模块级 `library_group_key(row) -> (kind, name)`，与前端 `libraryTable` 归组口径一致（book → 人工命名 label → 分类兜底）。`add_group_addresses(..., move_existing=False)`：
  - 已存在且**就在本组** → `skipped`（重复粘贴不算错）；
  - 已存在但在别组且 `move_existing=False` → `skipped` + `conflicts[{address, group, kind}]`；
  - `move_existing=True` → 改归属（book / label / category 三选一，前端会把组的主分类一起传上来，所以传了 `category` 就同步分类），**备注保留**，写 `updated` 快照，`moved` 计数；
  - 目标组名是「人工确认交易所 / 人工否定交易所」时，新增和转入都会同步写 `exchange_confirmed / exchange_rejected` 与对应 `source`（结论组名就是结论，否则“加进去了但扫描不当交易所”）。
  - 返回新增 `moved` / `conflicts` / `conflict_count`；`_record_import_batch` summary 形如「手动添加到「X」· 新增 N 个 / 转入 M 个」。
- 接口：`GroupAddressInput.move_existing: bool = False`，透传给服务。
- 前端（`app/static/app.js`）：编辑器加 `<label class="group-add-move"><input type=checkbox data-group-add-move> 已在别的组的地址也移到本组（改它的组名/分类，备注保留）</label>`；`saveGroupAdd` 读勾选、拼 `move_existing`，toast 分项报“已加入 N / 从别的组转入 M / K 个格式不对 / J 个已在「rrik」组，已跳过——勾选…再点一次”；`isExchangeGroupLabel()` 给结论组在编辑器下方加一行提示。注意「没有新增：本来就在本组」这句只在**没有 conflicts** 时才说（第一版写错了，探针抓出来后修掉）。样式 `.group-add-move`（14px 勾选框）。
- 数据修复（已执行，非代码）：把 `5EiXej3AwjKqb9mjQAf29JG5HJf9Dwtt8CjvDqA8biprWTiN`、`5DunDrFV6BPbzsgLZUtMQtCq7w9fcUEbM59BUwx5E7N9Bn5E` 用 `move_existing=true` 从 `rrik` 转入「人工确认交易所」组，`book` 清空、`label=人工确认交易所`、`category=EXCHANGE`、`exchange_confirmed=1`。rrik 由 199 → 197，确认组 3 → 5。
- 测试：`tests/test_group_add_and_exchange.py` 增 2 例（冲突→转入→撤回还原；结论组同步标志、普通组不写标志），全量 **152 passed**。
- 探针：`.probe/ui_check_0623.py` 用 rrik 的一条普通地址走「不勾→提示在 rrik→勾选→转入 1 个→组内 6 条」，两次 undo 还原，实测 toast 文案两版对比（第一版含错误前提，已修）。截图 `.probe/69_group_move.png`。

## 27. 2026-11-05 0.6.23 交接：合并群组

- 需求：同一个种子扫出来的地址分散在几个库里交叉命中，希望把几个组并成一个，可起新名字或继承其中一个原组名。
- 后端 `LinkService.merge_address_groups(sources, target_name)`（`app/service.py`）：
  - `sources` 为 `[{kind: "book"|"label", name}]`，去重后必须 ≥2；`book` 按 `AddressLibrary.book` 取，`label` 按 `label == name AND book == ""` 取（不会把自建库里的同名标签算进来）。
  - 目标名走 `_clean_book_name`（≤60 字）；若目标名已被**不在来源里**的库占用 → `InputError`，避免吞并别人的库；若目标名就是某个来源库名 → 视为继承，不新建库。
  - 合并只改 `book` / `label` 为 `target`，**分类、备注、交易所结论不动**；每条写 `updated` 快照。
  - 库表：目标库不存在则新建（`note="由群组合并创建"`，记 `created_book`）；来源里的其他库删除并记入 `removed_books`。
  - 快照 `kind="merge"`，summary 形如「合并 2 个组到「X」· 甲、乙」。
- `_record_import_batch` 新增 `removed_books` 参数（payload 同名字段）；`undo_last_import` 会把这些库名重建（原来只支持单个 `removed_book`），`last_import_batch` 也返回 `removed_books`。
- 接口 `POST /api/library/merge-groups`（`MergeGroupsInput{sources, target}`）。
- 前端（`app/static/app.js`）：`state.merge = {open, picked:Set, mode, target, inheritName}`；`mergeableGroups()` 按 `libraryTable` 口径归组（跳过未命名分类组）；`renderMergePanel()` 渲染面板，`refreshMergePanelState()` 只刷新计数/按钮/下拉（**勾选不重建 DOM**，否则输入框失焦、滚动丢失）；`applyMerge()` 带确认框。
  - 坑：`mergeKey` 最初用 `\u0000` 分隔 kind 和 name，写进 HTML 属性会被浏览器替换成 U+FFFD，勾选状态永远对不上（表现为“已选 0 组”）。改成 `kind:name`。
  - 模板：工具栏 `#merge-groups` 按钮 + `<div id="merge-panel" class="merge-panel hidden">`；样式 `.merge-panel` 系列（组列表 `auto-fill minmax(230px,1fr)`，最高 230px 可滚动）。
- 测试：`tests/test_merge_groups.py` 6 例（新名字合并保留分类/备注/交易所结论并删旧库、继承组名、自建库+标签组混合、撤回还原库名、四类拒绝、接口层）。全量 **158 passed**。
- 探针：`.probe/ui_check_0624.py`（新名字合并 3 个地址 → 新组出现/旧组消失/面板收起 → 撤回 → 沿用组名合并到「合并测乙」）；`.probe/layout_0624.py`（1422px：工具栏 8 个控件仍单行、面板宽 1025 无右溢出、组列表首行同排、命名行单行）。截图 `.probe/70_merge_panel.png`。探针数据已清理（总地址 672、库表 7 个不变）。

## 28. 2026-11-05 0.6.24 交接：刷新按钮挪到关联地址面板

- 需求：手动移除关联地址后要「刷新结果」，但按钮原来在顶部 `.actions`（导出 JSON / TXT 那一行），离结果表远，找不到；同时希望字号更大、颜色更突出、按下有手感。
- 模板 `app/templates/index.html`：`#refresh-result` 从 `.actions` 移到 `.related-panel > .section-title > .related-toolbar`，排在 `.result-filters` 之前；按钮内容为 SVG 图标 + `<span id="refresh-result-label">刷新结果</span>`。
- 不能再直接写 `button.textContent`（会抹掉图标和 span）：`updateRefreshButton()` 改成写 `#refresh-result-label` 的文本，并 toggle `is-armed` 类、改写 `title`。
- 样式 `app/static/styles.css`：新增 `--refresh-top/bottom/edge/ink/glow` 五组变量，分别给默认亮色、`prefers-color-scheme: dark`、`[data-theme="eye"]`、`[data-theme="darkgray"]` 覆盖；按钮 15px / 700+ 字重 / 12px 圆角 / `linear-gradient(180deg, top, bottom)`，`box-shadow: 0 4px 0 edge, 0 10px 20px rgba(0,0,0,.15)` 做出凸起下沿。
  - 按下：`:active` → `translateY(4px)` + 下沿阴影压平 + `inset 0 3px 9px rgba(0,0,0,.42)`，图标同步 `rotate(-80deg)`；`:not(:disabled):active` 才生效，禁用态只有 `0 2px 0` 浅下沿。
  - 坑：`is-armed` 最初用 `animation` 改 `box-shadow` 做呼吸，**动画优先级高于普通声明**，把 `:active` 的 inset 阴影整个盖掉，实测按下只有位移没有内阴影。改为伪元素 `::after` 画外圈涟漪（`inset:-4px`、`border:2px solid --refresh-glow`、`refreshPulse`），与 `box-shadow` 解耦。
  - `@media (prefers-reduced-motion: reduce)` 关动画保留静态外圈；`@media (max-width:560px)` 按钮 `width:100%`、与筛选组换行。
- 测试：`tests/test_version.py` 改为从 `VERSION` 文件读当前版本（原来硬编码 "0.6.23"，每次升版都要改）；全量 **158 passed**。
- 探针 `.probe/ui_check_0625.py`：1422px 下 `#refresh-result` 在 `.related-panel` 内、不在 `.actions`、15px/46px 高、与筛选项同排、面板内无溢出；移除一条后文案「刷新结果（已移除 1）」、`is-armed` 为真；`Input.dispatchMouseEvent` 按住时 `transform: translateY(4px)`、`boxShadow` 为 `inset 0 3px 9px rgba(0,0,0,.42)`；420px 下按钮 337px 宽独占一行（`setDeviceMetricsOverride` 需要 `mobile=True` 才会生效，否则 `innerWidth` 不变）。截图 `.probe/71_refresh_button.png`、`72_refresh_pressed.png`、`73_refresh_narrow.png`。
