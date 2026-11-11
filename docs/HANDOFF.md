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

## 29. 2026-11-05 0.6.25 交接：地址关联明细检索

- 需求：在已知地址库里查「地址 A 与 6NCK 组里哪个地址发生过交易」并看明细；也支持输入两个地址，查它们之间的关联链条。
- 数据现实：本地 `transfers` 只有 34 条（覆盖 9430401–9433299，约几小时），历史明细必须走 Taostats。官方文档确认 `/v1/transfers` 支持 `from` / `to` 精确匹配（还有 `address` 双向匹配、`amount_min`、`timestamp_start` 等），因此「双地址直连」只要 2 次请求就能拿全。
- `app/taostats.py`：`get_transfer_page(address, page)` 改为薄封装，新增 `get_transfer_page_query(address=…, from_address=…, to_address=…, page, limit)`；`TransferPage` 增补 `total_items` / `total_pages` / `per_page`（都有默认值，旧调用不受影响）；缓存 key 改为 `(address, from, to, page, limit)`。
- 新模块 `app/interlink.py`：
  - `InterlinkQuery{source, members, mode, max_hops, max_pages, min_tao, use_local, use_taostats, group_name, group_kind}`。
  - `search()` 流程：① 本地索引查 `source` 与目标的转账（免费）；② Taostats：组模式翻 `source` 的流水按成员交集匹配，地址模式 `from/to` 精确查两个方向；③ 地址模式无直连时 `_search_paths()` 找链路。
  - `_Fetcher` 管预算：`DEFAULT_REQUEST_BUDGET = taostats.LOCAL_REQUESTS_PER_MINUTE`（5），超出或撞限速就停，返回已查到部分并把原因写进 `stats.stop_reason` / `notes`，**不整体报错**。
  - 链路：1 跳用双方各自一页流水的邻居交集；2 跳从 A 的邻居按金额取 `MAX_INTERMEDIATES=3` 个继续展开，与 B 的邻居求交。每跳取金额最大的一笔作为代表，并给 `evidence`（支撑笔数）。
  - 明细统一结构：`{from,to,direction,counterparty,amount_tao,timestamp,block_number,tx_hash,extrinsic_id,edge_type,source}`；`_dedupe_key` 用 `tx_hash + block + amount + from + to` 去重，本地与 Taostats 不重复计。
- `app/service.py`：`LinkService.interlink_search(...)`，校验 SS58、组为空 / 两地址相同 / 都没给都抛 `InputError`；结果附 `source_member`（A 自己在库里的标签）。
- `app/main.py`：`InterlinkInput` + `POST /api/interlink`（422 走 `HTTPException`）。
- 前端（`app/static/app.js`）：`renderInterlinkGroups()`（用 `mergeableGroups()` 口径，签名比对避免重建下拉丢焦点）、`syncInterlinkMode()`、`runInterlink()`、`renderInterlink()`（统计条 / notes / 汇总表 / 明细表 / 链路卡片 / 空态说明）、`interlinkPathCard()`。
  - 坑：复制按钮的属性是 `data-copy-address`（不是 `data-copy`），写错点了没反应。
- 测试：`tests/test_interlink.py` 9 例（组模式 Taostats 交集、本地索引兜底、双向直连、1 跳桥、2 跳链、min_tao 过滤、非法输入、限速优雅降级、接口层）。全量 **167 passed**。
- 实测（真实 Taostats，1 页）：A `5HTL…RTif7` 与「6NCK」标签组（103 条，排除 A 后 102 个对比地址）命中 1 个地址 `5CPF…L5ex`，1 笔流入 5 TAO，2026-04-10 00:06:12，区块 7934019，tx `0x988f9abb14…`；地址模式查同一对，双向各 1 次请求也命中同一笔。
- 探针 `.probe/ui_check_0626.py`：面板存在、组下拉 `6NCK（103 条）`、模式切换字段互换、检索后两张表（6 列 / 7 列）各 1 行、无横向溢出。截图 `.probe/74_interlink_panel.png`、`75_interlink_page.png`。

## 30. 2026-11-05 0.6.26 交接：启动脚本拦截 Git 冲突标记

- 现场：用户把仓库 `git pull` 到另一台电脑（`G:\bittensor-wallet-link-intel`），双击 `启动.cmd` 后窗口报
  `The '<' operator is reserved for future use` / `Missing file specification after redirection operator`，
  定位到 `start.ps1` 第 31 行附近有 `<<<<<<< HEAD` —— `git pull` 时 `start.ps1` 有本地改动、合并冲突没解决。
- 关键判断：`git grep -n "^<<<<<<<\|^>>>>>>>" HEAD -- '*.ps1' '*.cmd'` 在仓库侧**无命中**，工作区 `start.ps1` 与 HEAD 一致（132 行），
  说明不是仓库带脏数据，而是那台电脑本地的工作区状态。
- 报错为何难懂：PowerShell 解析 `<` 为重定向/保留运算符，报的错跟「冲突」毫无关系；日志里 `logs/wallet-link.stderr.log`
  还留着上次成功启动的 `Application startup complete`，更容易误判成服务问题。
- 修复：`启动.cmd`、`安装.cmd` 在调用 `.ps1` 前先 `findstr /m /c:"<<<<" "%~dp0*.ps1" >nul 2>&1`，
  `if errorlevel 1 goto scripts_ok` 走正常流程，否则打印原因 + 修复命令（`git checkout origin/main -- start.ps1 launch_desktop.ps1`）后 `exit /b 1`。
- 两个坑：
  1. **`rem` 注释里不能写 `<`**：cmd 在 `rem` 行同样解析重定向，`rem ... <<<<<<< ...` 会把后半行当命令执行，
     出现「'不懂的语法错误…' is not recognized」这类诡异输出。注释改成「一排小于号加 HEAD」。
  2. **`findstr /c:"<<<<<<<"`（7 个）匹配不稳定**，实测同一文件 `/c:"<<<<"` 稳定命中 0、7 个却返回 1；
     统一用 4 个作为特征——自然代码里不可能出现连续 4 个 `<`，不会误报。
- 编码/换行：`.gitattributes` 已声明 `*.cmd text eol=crlf`，但用 Python 改写 `.cmd` 时 `newline=""` 会把 CRLF 压成 LF，
  批量脚本在 LF 下可能只认半行，改完必须 `raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")` 补回。
- 验证（PowerShell 直接调 `findstr`，绕开 Git Bash 的编码干扰）：
  - 含 `<<<<<<< HEAD` 的文件 → `exit=0`（命中，拦截）；`param([int]$Port = 8766)` 正常脚本 → `exit=1`（放行）。
  - 用 `cmd //c 启动.cmd` 跑分支：有冲突标记时输出「冲突标记」并停下；无标记时继续走到「还没有安装运行环境」。
  - 注意：从 Git Bash 用管道跑 `.cmd` 会因代码页差异把中文拆断（原版 HEAD 脚本同样表现），这是测试环境假象，不是脚本回归。
- 文档：`使用说明.md` 第十章新增「用 GitHub 同步到另一台电脑」（`.venv`/`data`/`logs` 不在仓库、拉完先跑 `安装.cmd`、授权码换机器要重签），
  常见问题补一条对应的 Q；`tools/build_usage_html.py` 重建 `使用说明.html`。
- 纯启动脚本改动，接口/查询逻辑/数据未动；静态资源戳升至 `v=0.6.31`。

## 31. 2026-11-05 0.6.27 交接：批处理必须存 GBK，否则 cmd 解析错位

- **维护铁律（后续改 `.cmd` 前必读）**：`启动.cmd` / `安装.cmd` / `停止.cmd` 一律保存为 **GBK（cp936）编码、CRLF 换行，且不要在里面写 `chcp 65001`**。
  用 UTF-8 编辑工具改完直接提交，会在用户机器上引发随机报错，见下。
- 事故现象：0.6.26 上线后，另一台电脑启动窗口顶部冒出一行
  `'不懂的语法错误，这里先拦下来。' is not recognized as an internal or external command`，
  同时 `if not exist (...)` 块里的 `echo` 也少了一段（显示成 `的 安装.cmd ，装完再启动。`）。程序本身能起来，但启动变慢、报错吓人。
- 定位过程（`.probe/trace_cmd.py` 把 `@echo off` 换成 `@echo on` 跑一遍）：
  ```
  >rem git 合并如果没解决冲突，脚本里会残留冲突标记（一排小于号加 HEAD）。
  >不懂的语法错误，这里先拦下来。          ← 本该是 `rem ` + 整行
  ```
  第 9 行被从中间切开，`rem` 前缀丢失，后半句被当命令执行。
- 根因：**cmd.exe 在 UTF-8 批处理 + `chcp 65001` 下按字节偏移续读脚本文件**。`chcp` 切换前后代码页不同，偏移换算错位；
  UTF-8 汉字 3 字节，一旦跨过 cmd 的读取边界（约 512 字节）就会切在字符中间。0.6.26 新增的两行注释把偏移推过边界，从而触发——
  所以「原版没事、加了注释就坏」，跟注释内容对错无关（`.probe/bisect_cmd.py` 验证过：单纯加 ASCII 填充行不会坏，加长中文行会）。
- 对照实验（`.probe/enc_test.py`，同一份脚本三种编码）：

  | 方案 | 解析 | 中文显示 |
  | --- | --- | --- |
  | UTF-8 + `chcp 65001` | **错位** | 正常 |
  | UTF-8 无 `chcp` | 正常 | 936 控制台下乱码 |
  | **GBK 无 `chcp`** | **正常** | **正常** |

  选第三种。控制台默认就是 936，文件编码与之相同，cmd 无需中途换代码页，偏移稳定。
- 实现：`.probe/fix_cmd_encoding.py` 把三个 `.cmd` 转 GBK、删掉 `chcp 65001 >nul` 行、并把注释替换为 ASCII 英文
  （面向用户的 `echo` 中文提示保留）。写文件时必须 `text.replace("\r\n","\n").replace("\n","\r\n").encode("gbk")`，
  否则 Python 会把 CRLF 压成 LF。
- 验证：`cmd //c 启动.cmd` 三种场景（正常 / 有冲突标记 / 缺 `.venv`）均无 `not recognized`，
  输出按 GBK 解码后中文完全正确；`安装.cmd` 在冲突标记下正确提示并停在 `pause`；`停止.cmd` 解析无误。
- 副作用与取舍：GitHub 网页预览这三个 `.cmd` 会显示乱码（GBK 不是 UTF-8），但文件本身功能正常。
  如果以后要改内容，务必用支持 GBK 的编辑器另存，不要用只认 UTF-8 的工具直接覆盖。
- 静态资源戳升至 `v=0.6.32`。

## 32. 2026-11-06 0.6.28 交接：官方交易所默认导入、只增不改、判定锁定

- **需求**：同步来的官方交易所不能改判；官方名单默认导入；同步只在出现新地址时增加，并提示新增了哪个地址。
- **锁定**：`EXCHANGE` 分类本身不足以区分来源，新增判定来源标记——`source` 以 `taostats:` 开头（或 `exchange_locked=true`）视为官方来源。
  前端 `isOfficialExchangeRow()` 命中后只渲染「官方交易所 · 不可改判」，不再给「改为否定 / 重置」按钮。
- **服务端同样生效**（不能靠前端挡）：`decide_exchange`、`decide_exchange_address`、`update_library_categories`，
  以及文件导入、备份还原、往「人工确认/否定交易所」组移地址，全部拒绝改动官方地址的分类与判定，接口返回明确原因。
- **只增不改**：名单里没见过的地址才新增；已在库的地址保留原主分类、名称、备注、所属地址库，只把 `EXCHANGE` 追加进多分类并锁定
  （官方名单为准，之前的人工否定会被纠正）；名单里消失的地址不删除。
- **开机同步**：`_auto_sync_exchanges()` 在 `lifespan` 里跑一次，30 分钟内不重复请求，需要已保存 Taostats key；
  运行期间不自动发请求。`AUTO_EXCHANGE_SYNC_ENABLED=False` 可整体关闭。
- **新增地址提示**：同步结果写入 `data/settings.json` 的 `exchange_sync` 字段（条数、最近同步时间、待提示的新增地址），
  前端只弹一次，`POST /api/library/exchange-sync/ack` 确认后不再重复。新增接口 `GET /api/library/exchange-sync`。
- **身份判定口径变了**：交易所判定从「只看主分类」改为「看全部分类」，`大V + EXCHANGE` 这种组合也算已确认交易所
  （影响关联终点、过滤、导出排除，`relation.py` / `service.py` 同步调整）。
- 测试：新增 `tests/test_official_exchange_lock.py`（9 例），全量 **176 passed**。

## 33. 2026-11-06 0.6.29 交接：时间统一北京时间，失败查询也留档

- **时区 bug 根因**：库里存的是 UTC naive（`2026-11-05T08:30:15`），`isoformat()` 不带 tz，浏览器按本地时间解析 → 记录比实际早 8 小时，
  用户看到的是「记录对不上」。不是写入错，是读出来少了时区标记。
- **修法**：后端统一走 `db.iso_utc()`（naive 视为 UTC，输出带 `+00:00`）；前端所有时间统一走 `bjTime()`，
  用 `Intl.DateTimeFormat` 固定 `Asia/Shanghai`，不管浏览器时区在哪都显示北京时间（`historyTime()` 转调它）。
- 覆盖位置：查询记录完成时间、地址库创建/更新时间、导入批次时间、本地索引任务时间、两地址关联明细、关联路径每一跳、官方交易所最近同步。
  列头统一标注「（北京）」。查询记录的时间语义是**本次查询结束时刻**，列头由「时间」改为「完成时间（北京）」。
- **失败查询也归档**：原来只在成功/停止时写记录，失败就消失，导致记录条数与实际操作次数对不上；现在异常分支也落库，
  `jobs.py` 补齐 `job` 字段（rounds / requests / stopped），点「查看结果」提示失败原因。
- 导出历史地址的 TXT 文件名日期改用北京时间（原来用 UTC 日期，凌晨 0–8 点会显示成前一天）。
- 测试：新增 `tests/test_timezone_display.py`（4 例），全量 **180 passed**。

## 34. 2026-11-06 0.7.0 交接：地址库巡检自动入库、关联强度分级、本地索引自动扫描

- **本地索引定期自动扫描**：`_local_scan_scheduler()` 每 `LOCAL_AUTO_SCAN_SECONDS=300` 扫 `LOCAL_AUTO_SCAN_BLOCKS=16` 块，不耗 Taostats 额度；
  与手动扫描共用同一把 SQLite 扫描锁，不会并发。只补「启动之后」的新块，关机缺口不自动补。
- **巡检（`app/watch.py`，新）**：
  - 表：`watch_state`（每地址基线）、`watch_candidates`（候选累计）、`auto_import_log`（入库日志）、`book_pair_stats`（库间规模）、`watch_runs`（轮次）。
  - **基线机制**：首次检查只写 `WatchState.last_block`，不灌历史对手方；之后只用 `block > baseline` 的边。否则第一次跑就会把整个历史关系灌进库。
  - 阈值：单笔 ≥ N 或与该地址累计 ≥ N 任一命中，默认 30 TAO。归属库取 `(cumulative_tao, tx_count)` 最大者；
    后到更高累计会自动改判 `book` 并更新同一条 active 日志（不是插两条）。
  - 跳过：交易所、验证者、「系统」分类；自动入库的地址默认不追踪（`follow_auto_imported=False`）。
  - 预算：`daily_budget` 默认 700 次/天，`_today_requests()` 汇总当天所有 run 的实际请求数；一轮跑不完下次接着跑（`last_checked_at` 轮转 + `before=run.started_at`，天然收敛）。
  - 调度：`_watch_scheduler()` 每 `WATCH_TICK_SECONDS=30` 调 `tick(1)`，约 4 请求/分钟，卡在 Taostats 5 次/分钟限速内。
  - 撤回：删掉自动写入的地址并把地址拉进 `WatchState` 黑名单，之后不再自动入库。
- **关联强度**：`LinkPolicy` 新增 `medium_tao=30 / strong_tao=100 / extreme_tao=1000`；`_strength_for()` 按累计金额分档，
  `tx_count>=3` 升一档（极强封顶），`depth>=2` 降一档，库间规模同档内 +8 分微调（不改档位）。
  `RelatedAddress` 增加 `strength / strength_score / strength_reason`，前端悬停看依据；老归档没有 strength 字段时由 `fallbackStrength()` 现算。
- **排序改为**「是否交易所 → 关联强度 → 层级 → 累计金额 → 地址」，交易所（官方 + 人工确认）永远第一。
- **坑**：`policy.to_dict()` 里的 `Decimal` 进 `json.dumps` 会抛 `Object of type Decimal is not JSON serializable`，
  被 jobs 吞成 warning（归档静默失败）。新增阈值字段一律 `str()` 化。
- 新增接口：`GET /api/watch/status`、`POST /api/watch/config`、`POST /api/watch/run`、`GET /api/library/auto-import-log`、
  `POST /api/library/auto-import-log/{id}/revoke`、`POST /api/library/auto-import-log/revoke-address`。
- 配置持久化：`settings_store.DEFAULT_WATCH_CONFIG`（enabled / interval_minutes=1440 / threshold_tao=30 / include_stake / daily_budget=700 / auto_import / follow_auto_imported）。
- 测试：`tests/test_watch.py`（11 例），全量 **191 passed**。静态资源戳升至 `v=0.7.0`。
- **做不到的**：巡检不是实时监控（690 个地址一轮约 690~1380 次请求，受每天 700 次预算约束，跨天续跑）；
  库间规模只统计本机已抓数据，不是链上全量；子网代币转移的覆盖度受 Taostats 端点粒度限制。
- **文档同步（同版本补提交）**：`README.md` 版本号升到 0.7.0、「已实现」列表重排为 1–41（0.7.0 三条落在 39–41）；
  `docs/DESIGN_FEASIBILITY.md` 文档版本与实现状态同步；本章与第 32、33 章补齐；`docs/VERIFICATION.md` 补 0.6.28 / 0.6.29 / 0.7.0 验证记录。

## 35. 2026-11-06 0.7.1 交接：全站排版优化（左栏固定 / 字段按内容宽 / 主操作放大）

- **需求原话**：左边列表不随页面滚动；组件宽度按最适合字段宽度调整、字段尽量居中；重要按钮和人工操作位置要突出、放大；
  举例：扫描页起始地址显示不全，而最大层级/单边最低/累计最低的输入框太浪费空间；按此思路所有页面都优化。
- **左栏固定**：`.shell` 用 `grid-template-columns:238px minmax(0,1fr)` + `.sidebar { position:sticky; top:0; height:100vh; overflow-y:auto }`。
  注意 sticky 生效前提是侧栏本身不是滚动容器、且父级 grid 行高足够；`≤950px` 仍沿顶部横条（原规则保留，`height:auto` 覆盖）。
- **扫描区**：`grid-template-columns:minmax(380px,1fr) 96px 112px 112px 138px`。`#seed` 用 12px 等宽字体、左右 padding 10px，
  1440 宽下输入框 390px，48 位 SS58 刚好完整。`≤1300px` 时 `grid-template-columns:110px 122px 122px 152px minmax(0,1fr)`
  且 `.wide.seed-field` 占满整行——四个短字段定宽靠左，剩余空间留白，**不要再改回 `repeat(4,1fr)`**（那会把它们重新拉成大格子）。
- **表格定宽**：关联结果 `#related > table`（注意 `#related` 自身就是 `.table-wrap`，选择器不要写成 `#related > .table-wrap > table`）；
  9 列在 1440px 内容区（1051px）下刚好放下。为放下做过三件事：收窄 `th/td` 左右 padding 到 10px、备注列 textarea 限宽 118px、
  地址列 min-width 220px。改任一列宽后必须用 `.probe/measure_forms.py` 复量，`scroll` 应为 0。
- **数字居中口径**：`.table-wrap thead th, tbody td { text-align:center }`，再单独把地址列（td.address）、备注列、名称列改回 left；
  表头跟内容对齐一致。数值输入框统一 `text-align:center`（按 `inputmode` 选中），扫描区 select 居中（`text-align-last` 也要设）。
- **主操作 `.cta`**：独立变量 `--cta-top/bottom/edge/ink/glow`（四套主题各一份，别直接用 `--accent`：深色主题的 accent 是浅色，
  白字会看不清）。目前用在 `#run-scan`、`#sync-all`、`#create-book`、`#run-interlink`、`#watch-run`、`#start-backfill`。
  新增主操作按钮时优先挂 `.cta`，不要另写一套渐变。
- **表单重排**：地址库录入改 grid（4 字段一行 + `.lib-form-actions` 单独一行右对齐，HTML 里新包了这层 div）；
  巡检/检索/同步/新建库/导出统一「控件左、按钮右」；`≤950px` 全部转 1–2 列。
- **已知取舍**：`≤1180px` 三张表开始横滚（9 列表格信息量摆在那，不强压到看不清）；420px 下页面本身不横向溢出。
- **验收**：`.probe/measure_forms.py`（新）在 1440/1180/1024/760/420 五档量容器宽度、行数、表格列宽、页面 `overflowX` 与元素越界，
  结果存 `.probe/shots/forms-*.json`；`.probe/measure_layout.py` 量侧栏 `position` 与滚动后的 `top`（应恒为 0）。
  全量 `pytest -q` = **191 passed**（纯前端改动，后端未动）；`node --check app/static/app.js` 通过。

## 36. 2026-11-06 0.7.2 交接：地址库「全部/选中」导出 + 「公共地址」终止查询

- **需求原话**：「导出地址库要可以实现全部导出和选中的地址库导出，我刚才试了下选中导出，实际是全部地址库导出。关键地址除了可以标记为交易所，还要增加一个公共地址，公共地址和交易所性质类似会和不确定钱包产生关联，也要终止查询。」
- **根因**：原来只有 `GET /api/library/export?format=`，前端 `exportLibrary(format)` 不带任何范围参数，两个按钮（导出 CSV / 导出 JSON）永远导整库，勾选框只是给「删除所选」用的。
- **导出改造**：新增 `POST /api/library/export`，body `{format, addresses}`；`LinkService.export_library(file_format, addresses=None)` 里 `addresses is None` 才是全库。
  - 前端 `exportLibrary(scope)`：`all` 仍走 GET；`selected` 走 POST，地址从 `[data-library-select]:checked` 的 id 反查 `state.library`。
  - `updateLibrarySelection()` 里同步「导出选中 (N)」的 disabled 与文案——**新增选中类操作时记得在同一个函数里挂上**，否则按钮状态会和勾选不同步。
  - 选中集合为空 / 地址已不在库里 → `InputError`（422），别让它静默退化成导出全库（这正是用户踩到的坑）。
- **公共地址（PUBLIC）**：写进 `SYSTEM_CATEGORIES`（与 EXCHANGE/VALIDATOR 并列），因此分类下拉、`add_category`、`delete_category`、新建地址库、整组改分类、`seed_address` 都会自动把它当系统分类处理，不用逐处改。
  - `SYSTEM_CATEGORY_LABELS` 加 `PUBLIC → 公共地址（系统）`，`RESERVED_CATEGORY_NAMES` 加 `PUBLIC`，前端 `categoryLabel()` 同步。
  - `LinkService.decide_public_address(address, marked)`：**只往多分类追加/移除 PUBLIC，主分类保持用户原样**；新地址标记时主分类才用 PUBLIC。接口 `POST /api/library/mark-public`。
- **终止规则**：`relation.discover()` 的 `terminal_exchange_addresses` 加 `_entry_has(entry, "PUBLIC")`；`LinkService.new_crawl()` 的 `CrawlState.terminal_exchange_addresses` 加 `context.public_addresses`。
  两处都要改——只改一处会出现「本地查询截止、Taostats 分轮还在往下爬」的不一致。
- **过滤**：`prepare_relation_context` 新增 `filter_publics`，与 `filter_systems` 同口径（不勾选=终点保留，勾选=连自己也排除）；链路 main.py 两个 Pydantic 模型 → service → jobs.create → context，一处都不能漏。
- **排序**：`_is_exchange_result` 抽了 `_result_categories()`，新增 `_is_public_result()`，排序键改为 `0 交易所 / 1 公共地址 / 2 其它`。
- **角色标签**：`_role_for_address()` 里 PUBLIC 判断放在 EXCHANGE / VALIDATOR 之后，label 为空时显示「公共地址」。
- **前端**：结果行 `.library-decision-cell` 里「交易所结论 + 公共地址按钮」竖排；地址库行分类单元格里两个控件并排。`decidePublic()` 只在 `#result` 面板可见时才 `renderResult()`，否则在地址库页点按钮会被跳回结果页。
- **样式**：`.public-tag` 用主题变量 `--warn / --warn-soft`（不要再写死 `#b4761a`，深色主题下会看不清）。
- **验收**：`.probe/ui_check_072.py`（导出按钮文案/禁用态随勾选变化、`#filter-publics` 存在、每行都有「标为公共地址」）；接口层实测 707 库里勾 2 条导出就是 2 条、文件名 `-selected-2`；`pytest -q` = **197 passed**（新增 `tests/test_public_address.py` 6 条）。

## 37. 2026-11-06 0.7.3 交接：验证者终止关联、同步验证者名单、内置固定名单

- **需求原话**：「已知地址库同步官方交易所地址边上增加一个同步验证者地址，验证者地址一样需要终止关联。比如 `5HK5…REpeN` 就是验证者地址，出现很多无效关联。人工确定的交易所地址和验证者地址，公共地址都要在今后的版本里直接预置固定。」（附 5 个交易所地址 + 1 个验证者地址）
- **终止规则**：`relation.discover()` 的 `terminal_exchange_addresses` 加 `_entry_has(entry, "VALIDATOR")`；`_role_for_address()` 本来就有 VALIDATOR 分支，不用改。`exchange_balance_candidates()` 原来用 `entry.category == "VALIDATOR"` 只看主分类，改成 `_entry_has()`，否则「大V + VALIDATOR」会漏。
  - **验证者不再参与「待确认交易所」余额探测**，也顺手把 PUBLIC 一起排除了。
  - 注意：`filter_validators`（勾选后连本人一起排除）保持原样，两道开关互不替代：**终止 = 不展开下游，过滤 = 连本人也不显示**。
- **同步验证者**：`LinkService.sync_validator_addresses()` 复用 `taostats.get_active_validator_addresses()`（返回 set，无名称），插入 `category=VALIDATOR`、`source=taostats:/v1/validators/active`；已有地址保留主分类/名称/备注，只追加 VALIDATOR。接口 `POST /api/library/import-validators`，401/429/502 与交易所同步一致。
  - **真实账户一次会新增约 1200 条**（Taostats 名单同时含每个子网的验证者 hotkey 和 coldkey）。这是预期量级，不是 bug；地址库清单会折叠成「验证者（系统）· 未命名」一组，巡检也跳过验证者，不会多花请求额度。
  - `source` 只在当前来源不是 `taostats:` / `builtin:` 时才改写，避免把已锁定的交易所来源冲掉。
- **内置固定名单**：新增 `app/builtin_addresses.py`，三个元组 + `BuiltinAddress`（`source = builtin:<category>`）。`LinkService.ensure_builtin_addresses()` 在 `main.lifespan` 里调用（`BUILTIN_ADDRESSES_ENABLED`），幂等 upsert：只补分类/来源/交易所确认，不删不改用户内容。
  - **新增地址只需在元组里加一行**，不用改数据库、不用改同步逻辑；公共地址元组已预留。
  - `is_official_exchange_source()` 的前缀从 `taostats:` 扩展为 `("taostats:", "builtin:")`（`LOCKED_SOURCE_PREFIXES`），于是 `exchange_locked`、`decide_exchange`、`update_library_categories` 这些既有保护自动覆盖内置记录；新增 `is_builtin_row()` / `locked_row_message()` 用于「内置验证者/公共地址」这种非交易所行的拒绝提示。
  - 删除保护三处：`remove_address()` 抛 `InputError`（接口 409）、`remove_addresses()` 用 `~coalesce(source,"").startswith("builtin:")` 过滤、`dissolve_address_book()` 跳过并返回 `kept_builtin`。
  - `decide_public_address(..., marked=False)` 对内置行拒绝取消；`address_dict()` 下发 `builtin_locked`，前端据此禁用勾选框/删除按钮、显示「内置固定」。
  - `relation.py` 里 `exchange_locked` 的前缀判断也要同步加 `builtin:`，否则结果页会给出可改判按钮。
- **1 级路径明细**：`relatedTable()` 里按钮条件从 `path_transfers.length > 1` 改成 `>= 1`。后端本来就对 1 级返回 1 跳，只是前端藏了按钮。
- **前端**：`#sync-validators` 紧跟 `#sync-exchanges`，`syncValidatorLibrary()` 与交易所同步同结构；`updateLibrarySelection()` 的选择器改成 `[data-library-select]:not(:disabled)`，否则内置行（禁用勾选框）会让「全选」永远达不到全部。
- **验收**：`tests/test_builtin_addresses.py` 8 条；`.probe/ui_check_073.py` 在 1440/1180/1024 三档确认按钮单行、无溢出、内置行锁定、1 级路径可展开；`pytest -q` = **205 passed**。

## 38. 2026-11-06 0.7.4 交接：人工确认地址后回头校正历史查询结果

- **需求原话**：「人工确定交易所地址，验证者地址或者公共地址后，扫描已知地址库和查询记录，对前面地址库和查询结果进行纠正。移除以上三种类型地址产生的无效关联。」
- **问题的本质**：终点地址是「查到就截止」，但判定发生在查询之后。旧快照里，当时还没被认定成交易所/验证者/公共地址的钱包是被当普通钱包继续往下走的，终点下游那一片全留在结果里。所以不是重查，而是拿新的终点名单去**重剪旧快照**。
- **核心函数** `service.prune_archive_payload(payload, terminal)`，判定顺序很重要，别改：
  1. 有逐跳路径时看 `path_transfers[:-1]`——路径前面出现过终点，说明这条关联是从终点继续走出来的，删；终点自己（路径最后一跳落到终点）保留。
  2. 路径缺失（老记录按分轮续查存的，或早期版本没存路径）才退回用 `edges` 做「遇终点即止」的 BFS：终点整片下游只记进 `blocked_by`、不进可达集。
  3. **两者都没证据 → 不删**，计入 `unverified`。这条是踩坑换来的：第一版纯靠可达性判断，把 180 条正常线索当噪音删了。
  4. **边表 `edges` 原样保留**。第一版连边表一起精简过，结果第二次校正时可达图变小、又多删 27 条（不收敛）。现在落库后再预演恒定 `changed=0`。
- **入口** `reconcile_history(dry_run, addresses=None, archive_ids=None, watch=True)`：
  - `needle in row.payload` 先粗筛相关快照，避免全库反序列化；
  - 结果写 `payload.correction = {removed_related, rounds, by_role, at}`（`rounds` 累加），并在 `warnings` 追加「本记录已按当前地址库校正：移除 N 条…」，这样导出和结果页都能看出这条被剪过；
  - `watch=True` 时同步清理地址库侧：`watch_candidates.trigger_address ∈ terminal` 的行删掉，`auto_import_log` 同条件改 `revoked` 并写原因。**地址本身不动**。
  - `addresses=[...]` 用于「只针对刚确认的这一个地址」快速校正，`watch=False`（不必每次单个确认都去扫巡检表）。
- **两套口径必须共用**：`relation.terminal_addresses()` / `terminal_roles()` 抽出来给 `discover()` 和 `prune_archive_payload` 共用，否则「实时查询截止的集合」和「校正用的终点集合」会漂移。终点规则 = EXCHANGE 且 confirmed / VALIDATOR / PUBLIC / 标签或备注含「交易所」（非 rejected）/ 「系统」分类。
- **触发点**（都是「判定一发生就顺手剪」）：`decide_exchange`、`decide_exchange_address`(confirm)、`decide_public_address`(marked)、`update_library_categories`、`update_library_note`（备注含「交易所」）、`update_library_label`（标签含「交易所」）。返回体统一带 `correction` 字段，前端 `correctionText()` 拼成「已顺带校正 N 条历史结果（移除 M 条无效关联）」。同步交易所/同步验证者名单结束后调 `autoCorrectAfterSync()`（一次带进上千个终点，必须自动剪）。
- **接口** `POST /api/library/reconcile`，入参 `{dry_run: bool, archive_ids?: number[]}`，返回 `{archives:{scanned, changed, related_changed, related_removed, unverified}, terminal:{count, by_role}, watch:{candidates_removed, auto_imports_revoked}}`。
- **前端**：地址库清单工具栏 `.library-reconcile-group` 里放 `#reconcile-history`，`reconcileHistory()` 先 `dry_run:true`，`changed=0` 就 toast「历史结果已经是干净的」，否则 `window.confirm` 列出清理量与按角色统计（并单列 unverified 建议重查），确认后才 `dry_run:false`，完成后刷新地址库与历史列表。样式只用 `var(--accent)`（`--accent-ink` 在四套主题里没有定义）。
- **实测**（真实库 1902 条地址）：当前终点 1214（交易所 17 / 验证者 1196 / 系统 1）；35 条相关快照里 `changed=5`、移除 415 条关联（交易所下游 262、系统 153）、`unverified=180`；样本记录 199 → 9；落库后再预演 `changed=0`。备份留在 `data/wallet_link.before-reconcile-0.7.4.db`（`PRAGMA integrity_check` ok）。
- **验收**：`tests/test_reconcile_history.py` 11 条 + `tests/test_builtin_addresses.py` 8 条，`pytest -q` = **216 passed**；`.probe/ui_check_074.py` 1440/1180/1024 三档无溢出无重叠、只发一次 `dry_run:true`。
- **遗留**：`unverified=180` 这批老记录（分轮续查、无路径且边表不连通）只能靠重查补全路径，预演确认框里已单列提示；`data/wallet_link.before-reconcile-0.7.4.db` 是否长期保留待定。

## 39. 2026-11-06 0.7.5 交接：起始地址放大高亮 + 注册日期/交易次数/日均频次/余额

- **需求原话**：「扫描结果位置的起始地址字体放大一点，颜色突出显示。后面加上地址注册日期，历史交易次数，日均交易频次，余额。」
- **数据源（都实测过，别再猜）**：
  - `GET https://api.taostats.io/api/account/latest/v1?network=finney&address=…` —— 一次返回 `created_on_date`、`created_on_network`、`balance_free/staked/staked_alpha_as_tao/reserved/total`（都是 **rao 字符串**）、`rank`。这是拿注册日期的唯一现成来源，不用逐块回溯。注意这个端点在 `/api/` 下，和转账用的 `/v1/transfers` 不是同一套前缀。
  - **历史交易次数** 不用单独接口：转账分页响应里的 `pagination.total_items` 就是该地址的全量笔数。`get_transfer_total()` 用 `limit=1` 只取 1 条数据换这个数字，一次请求搞定。
  - 余额仍保留 RPC 兜底（`seed_balance_tao`），但界面优先显示 Taostats 的 `balance_total`（含质押），因为 RPC 只给自由余额。
- **新增代码**：
  - `taostats.AccountProfile`（frozen dataclass）+ `get_account_profile()` + `get_transfer_total()` + `_rao_to_tao_text()`；`ACCOUNT_LATEST_URL` 常量；`_profile_cache` 走同一个 `CACHE_TTL_SECONDS` 和 `_reserve_request()`。
  - `LinkService._seed_profile(seed, *, transfer_rows, data_source, include, warnings)`：**纯汇总**，四项指标任何一个失败都只 append warning，不抛异常。`include=False`（停止续查）直接返回空画像，不发请求。
  - 结果字典新增 `seed_profile`，字段见 `_seed_profile` 的初始 dict（`registered_on` / `registered_on_network` / `balance_*` / `transfer_count` / `active_days` / `daily_frequency` / `transfer_count_scope` / `source`）。
- **两个易错点**：
  1. `service.py` 用的是 `from .taostats import get_account_profile, get_transfer_total, is_configured as taostats_is_configured` 直接导入。**方法体里必须写导入的名字**（`get_account_profile(seed)`），写成 `taostats.get_account_profile(seed)` 时 monkeypatch 打在模块上不生效，测试会静默拿到 None（这次就是这么踩的，4 条测试失败才发现）。
  2. 原 `get_account_profile` 的缓存分支里用 `AccountProfile(**{**cached[1].__dict__, "from_cache": True})`，frozen dataclass 靠 `__dict__` 复制会带上 `_` 之类内部字段时要留意；目前字段都是普通字段，没问题，但加字段后要同步。
- **「0」和「未获取」必须区分**：`formatTao(0)` 返回 `0.00 TAO`，`formatTao(null)` 返回「未获取」。前端 `renderSeedProfile()` 里每个指标都单独判 `Number.isFinite` / `!== null`，**不能**用 `value || "未获取"`，否则真实的 0 会被吞掉。
- **界面**：`.seed-balance-card` 从 flex 改成 grid（1440 下 `minmax(0,1.6fr) repeat(4,minmax(0,1fr))`），1180 及以下 `repeat(3,…)`、560 及以下 `1fr 1fr`，地址块 `.seed-identity` 在窄屏 `grid-column:1/-1`。地址 17px/700/`var(--accent)` + 3px 左侧竖条。**注意**：原来第 593 行那组 `.seed-balance-card { grid-template-columns:minmax(0,1fr) auto }` 和第 641 行的 `1fr` 覆盖规则会盖掉新布局，已删除——以后改这个卡片先 grep `seed-balance-card` 把旧规则清干净。
- **验收**：`tests/test_seed_profile.py` 8 条，`pytest -q` = **228 passed**；真实账户实测 47 288 笔 / 1 468 天 / 32.21 笔每天 / 119 310.99 TAO；`.probe/ui_check_074_profile.py` 四档宽度均不截断、不重叠、零溢出。
- **遗留**：日均频次用的是「自然天数」而不是「实际活跃天数」（后者需要逐日交易聚合，Taostats 免费档没有现成接口），所以长时间不动的地址会把均值拉低，解读时要说明口径。

## 40. 2026-11-06 0.7.6 交接：同步验证者名单偶发 SSL EOF 失败

- **现象**：点「同步验证者地址」报 `Taostats 验证者名单请求失败：[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`，HTTP 502。重试有时又会成功。
- **别急着怀疑接口或参数**：`GET https://api.taostats.io/v1/validators/active` **不能带任何参数**——带 `network=finney` 报 `unknown field network, expected netuid`，带 `limit` 报 `unknown field limit`。不带参数则一次返回 7439 行（单个 `pagination` 页，`total_pages=1`），去重后 1196 个地址。现有实现本来就是裸请求，参数没问题。
- **根因**：`taostats.py` 里每个请求都是「`_reserve_request()` + 单次 `httpx.get`」，**没有重试**。Taostats 的 TLS 握手偶发被对端截断（`httpx.ConnectError`，底层是 `ssl.SSLEOFError`），一次抖动就直接抛 `TaostatsUnavailableError` → 502 → 界面「同步失败」。
- **修复**：新增 `taostats._request_get(url, api_key=, params=None, timeout=20)`：
  - 只捕 `httpx.TransportError`（SSL EOF / 连接重置 / 读超时），最多 3 次，退避 `TRANSIENT_RETRY_BACKOFF_SECONDS * (attempt+1)` = 1.2s、2.4s；
  - **HTTP 状态码不重试**——401/403 走 `TaostatsAuthError`、429 走 `TaostatsRateLimitError`、其余 ≥400 走 `TaostatsUnavailableError`，逻辑与原来完全一致，避免把限流当网络抖动反复打；
  - 重试**不额外调 `_reserve_request()`**，只在进入前占一次本地 5 次/分钟额度——失败的请求没有消耗上游配额，重复占用会把本地额度烧光。
- **五处请求全部改用它**：验证者名单、交易所名单、转账分页（`API_URL`）、质押转账（`STAKE_EVENTS_URL`）、账户资料（`ACCOUNT_LATEST_URL`）。grep 一下确认文件里只剩 `_request_get` 内部那一处 `httpx.get`。
- **测试要注意**：`tests/test_taostats.py` 是通过 `monkeypatch.setattr(taostats.httpx, "get", ...)` 打桩的，所以 `_request_get` 内部必须写成 `httpx.get(...)`（模块属性），不能改成 `from httpx import get`。重试里用了 `time.sleep`，测试同时打了 `taostats.time.sleep` 的桩以免真的等 3.6 秒。
- **实测**：修复后同一接口 200，`official_total=1196`、`inserted=0/updated=0/skipped=1196`（已经全在库里，属正常幂等），`synced_at=2026-11-06T12:13:53Z`。
- **验收**：`pytest -q` = **230 passed**（新增 2 条重试用例）。
- **遗留**：如果 Taostats 长时间不可达，3 次重试（约 3.6s）后仍会返回 502；界面提示已是原始错误文本，可以按「稍后重试」处理，无需改代码。

## 41. 2026-11-06 0.7.7 交接：远端 405、预置验证者名单、地址库分页、结果地址单行

- **远端「同步验证者失败」不是网络问题，是 405。** 用户在原话里贴的提示是 `Method Not Allowed`，不是 SSL。远端浏览器发的是 **GET** `/api/library/import-validators`，而这个路径会被注册更早的 `@app.delete("/api/library/{row_id}")` / `@app.patch("/api/library/{row_id}")` 抢走匹配（`{row_id}` 没写 `:int`，字符串也能匹配），方法不符 → 405。本地一直发 POST，所以从来没复现过，别再以为是 key 或 TLS。
  - 修法：① 两个同步接口改 `@app.api_route(..., methods=["POST","GET"])`；② 所有行级动态路由写成 `{row_id:int}`，这样 `/api/library/<单词>` 直接 404，不再误导。新增测试锁死这两条。
  - 教训：**遇到 405 先怀疑路径被动态路由抢占**，不要顺着前一个 SSL 报错误导。0.7.6 的传输层重试仍然保留（本地确实遇到过 TLS EOF），两者是不同故障。
- **预置验证者快照怎么维护**：数据在 `app/data/builtin_validators.txt`，一行一个 SS58，`#` 开头为注释；`builtin_addresses.load_validator_snapshot()` 读取，`builtin_entries()` 把它接在人工验证者之后并**按地址去重**（人工优先）。要更新名单：重新同步一次 → 导出 VALIDATOR 分类地址 → 覆盖该文件 → 升版本。因为 `source` 是 `builtin:validator`，这些行会被锁定（不可删除、不可改分类），这正是「预置固定」的含义。
- **已有库不会被覆盖**：`ensure_builtin_addresses` 只在该行来源不是官方同步（`taostats:`）时改成内置。所以「本机已同步过」的地址保留 `taostats:/v1/validators/active` 来源、仍可删除；「远端从没有过」的地址才会以内置形式插入并锁定。这是有意的：不抹掉真实来源。
- **性能**：内置条目从 6 条涨到 1201 条，`ensure_builtin_addresses` 原来逐条 `select`，改成进函数时一次 `select(AddressLibrary)` 建字典。启动总耗时没明显变化。
- **地址库分页的实现要点**：
  - 每组 50 条（`LIBRARY_GROUP_PAGE_SIZE`），页码存在 `state.libraryGroupPages`（Map，groupId → page）；
  - 分组还是原来的 `libraryTable()`，只在渲染时 `slice`，**组标题里的条数仍然是整组**，不是当页；
  - `state.libraryGroupIndex` 每次渲染重建（groupId → 该组全部行 ID），「全选本组」按它跨页选；
  - **勾选状态迁移到 `state.librarySelection`（Set，存字符串 ID）**：原来靠 DOM 查询，分页后翻页就丢；全选、导出选中、批量删除三处都已改成读这个集合。加新的选中相关功能时不要再去查 `[data-library-select]:checked`。
- **结果地址单行的坑**：不能给 `.address-main` 整体 `flex-wrap:nowrap`。试过，表格被撑宽 137px（1181 vs 1044，横向滚动条出现）。正确做法是只给 `.related-address-cell .addr-link` 加 `white-space:nowrap; word-break:normal`，让「复制 / 移除 / 角色标签」自己换到下一行。验证方法：量 `.addr-link` 的高度是否小于 `line-height`（17 < 18 即单行）。
- **验收**：`pytest -q` = 234 passed（新增 4 条）；GET/POST 均 200、未知子路径 404；预置验证者 1194 条 24 页、末页 44、翻页保持展开；结果表 1440/1180/1024 三档地址零折行。

## 42. 2026-11-06 0.7.8 交接：升级不重启的根因、启动自动重启、环境自检

- **0.7.7 的两个修复都是对的，远端就是没跑到。** 用户报「同步 0.7.7 下来，还是没有预置验证者、同步还是老报错」，本地怎么测都是 234 passed、GET/POST 全 200，说明问题在部署侧。真正的根因是 `start.ps1`：探到同目录已有服务在跑时，第 51 行附近直接 `Service is already running` + `exit 0`。`git pull` 之后用户双击的正是这个脚本，于是**旧进程一直活着**，新代码一行没执行。
- **为什么这个坑极难自查**：`/api/version` 走 `read_version_info()`，读的是**磁盘上的 `VERSION` 文件**，跟运行中的进程毫无关系。`git pull` 改完文件，页脚立刻显示 `当前版本：0.7.7`，用户以为升级成功了，只是「功能没生效」——实际上跑的还是 0.7.6。以后凡是「版本号对但行为不对」，第一反应必须是**确认进程重启过**。
- **修法一：`start.ps1` 自动重启。** 服务已在同目录运行时不再无脑退出，改为比对 `health.version`（进程启动时读的 `VERSION_INFO`，见 `app/main.py` 顶部）与磁盘 `VERSION` 文件：
  - 不一致 → 打印 `检测到代码已更新：运行中 X → 磁盘 Y` / `正在重启服务以加载新版本…`，停旧进程，等端口释放，再走正常启动流程；
  - 一致 → 保持原来的 `exit 0`。
  - **坑：不要 `& stop.ps1`。** 那个脚本里到处是 `exit 0`，被调用时会把当前启动会话一起结束（实测直接退到 `Test-PortFree` 之外）。停止逻辑必须就地写：pid 文件优先、`Get-NetTCPConnection` 按端口兜底、只处理 `python*` 进程，然后轮询 `Test-PortFree` 最多 20 秒。
- **修法二：把「磁盘版本 vs 进程版本」暴露到接口。** `/api/version` 新增 `running_version`、`stale` 两个字段；前端 `renderReleaseNotes()` 在 `stale` 时把页脚改成 `运行中版本：0.7.7（磁盘已是 0.7.8，需重启）` 并加 `.version-stale` 类，同时在更新日志页顶部显示 `#version-stale-banner` 横幅（带「重启后再自检」按钮）。**不要再让页脚只显示磁盘版本**，那是这次误判的根源。
- **新增 `app/diagnostics.py`**：一次算清五件事——版本（磁盘/进程）、预置名单文件（存在/条数/mtime）、落库情况（验证者分类数、预置标签数、内置来源数）、本次启动写入条数、API key。每项返回 `是 / pass / warn / fail + detail + hint`，`hint` 必须写成可执行动作（「双击停止.cmd 再启动.cmd」），不能是「请检查配置」。
  - 三个入口共用同一个 `build_report()`：`GET /api/diagnose`（界面，`cache_ttl=5.0`）、`python run.py --doctor`（命令行，实时、失败退出码 1）、测试。
  - `library_status()` 按数据库路径做短 TTL 缓存，避免界面反复点造成上千行 count。
- **落库判定口径的坑**：初版写的是 `source LIKE 'builtin:%'` 计数，结果本机只有 6 条——因为 1194 个验证者早已通过 Taostats 同步入过库，`ensure_builtin_addresses` 按规则**不覆盖官方来源**（保留 `taostats:`），只补分类和标签。必须改成 `max(验证者分类数, 预置标签数)` 对文件条数算覆盖率，<90% 才判 fail，否则老用户一律误报。
- **启动日志加了一行**：`[builtin] 版本 0.7.8 预置验证者 1196 条；本次启动新增 N 条、补标记 M 条`（打到 stderr，即 `logs\wallet-link.stderr.log`）。远端排查时这一行是「新版本真的跑起来了」的最直接证据。
- **实测**：进程 0.7.7 + 磁盘 0.7.8 时运行 `start.ps1` 正确触发重启，重启后 `/api/health` 为 0.7.8 且带 `validator_snapshot:1196`；`/api/diagnose` 五项全 pass；失败态（模拟旧进程 + 空库）横幅/页脚/三条建议均正确，1440 与 1080 两档零横向溢出；深灰主题下 fail 卡片 `rgb(73,56,54)` + 浅字可读。
- **验收**：`pytest -q` = **246 passed**（新增 `tests/test_diagnostics.py` 12 条）；`使用说明.html` 已重新生成（77,806 bytes）。
- **给下一任的话**：这个项目已经连续三次栽在「看起来升级了其实没生效」上（0.7.6 以为 SSL、0.7.7 以为 405、0.7.8 才发现是没重启）。以后收到「XX 修复没起作用」，**先让用户跑 `run.py --doctor` 或看环境自检截图**，别急着改代码。

## 43. 2026-11-06 0.7.9 交接：起始地址画像移入左栏、地址脱敏 + 复制

- **需求**：起始地址画像那一排（地址、注册日期、历史交易、日均频次、余额）原本横在结果区最上面，占掉整行宽度，用户要求挪到**左侧栏导航下方的空白处**，地址只显示头尾 4 位、后面带复制按钮。
- **结构改法**：结果区原来的 `<section class="seed-balance-card">` 五列卡片整段删掉，换成 `<div id="seed-card-slot"></div>` 占位；画像块改写成 `<section id="side-seed" class="side-seed hidden">` 插在 `.sidebar` 的 `<nav>` 与 `.aside-status` 之间。**不要删 slot**——窄屏时画像要搬进去。
- **四个 id 全部复用**（`#seed-result-address/-registered/-txcount/-daily/-balance` 及其 `-note`），所以 `renderSeedProfile()` 逻辑一行没改，只是 DOM 位置变了。新增 `setSeedAddress()` 负责「头尾 4 位 + `data-full` + `title`」。结果副标题里的起始地址也改成 `shortAddr()`。
- **响应式关键**：侧栏在 `max-width:950px` 会变成顶部横条（`flex-direction:row`），此时画像挤在横条里会被压成一条极窄竖列。做法是 `placeSideSeed()`：窄屏 `appendChild` 到 `#seed-card-slot` 并加 `.in-slot`（内部网格变四列、≤950px 两列），宽屏则 `insertBefore(status)` 搬回侧栏并去掉 class。用 `window.matchMedia("(max-width: 950px)")` 的 `change` 事件驱动，**不要用 resize 事件**（会被动画刷爆）。
- **排版取值**（238px 侧栏内实宽 205px）：卡片 padding `13px`、圆角 12px、底色 `--bg`（侧栏本身是 `--panel`，靠底色反差分块）；地址 13px/700 + 3px 左竖条 + `--accent`；指标行用 `dl` + 两列网格（`dt auto` / `dd 1fr`），标签 11px 靠左、数值 12.5px 等宽数字靠右、说明 10px 右对齐（`small:empty { display:none }` 避免空行撑高）；复制按钮 10.5px / `padding:4px 8px`，实测 `btnRight 207 ≤ cardRight 221` 不出框。
- **踩坑**：一开始把地址写成 `white-space:nowrap; overflow:hidden; text-overflow:ellipsis` —— 但现在地址本身已经截成 `5Hiv…N8kS`（12 字符），13px 下宽约 78px，永远不触发省略号，**保留这两行是给超短侧栏兜底的**，别以为没生效就删掉。
- **验收**：1440/1180/1080 三档 `fitsSidebar=true`、零横向溢出；复制回显「已复制」且写入剪贴板的 URL 为完整 48 位；900px 搬进 slot 变两列，回到 1440 自动搬回；深灰主题下 `cardBg rgb(41,43,47)` + 数值 `rgb(226,226,222)` 可读。全量 **246 passed**（纯前端改动，后端无变化）。
- **给下一任**：改这块只动 `app/static/{app.js,styles.css}` 与 `app/templates/index.html`，**改完必须升 `VERSION` 并同步 `?v=` 资源戳**（`styles.css?v=` / `app.js?v=`），否则远端浏览器拿缓存看不到改动；0.7.8 起的自动重启只解决 Python 进程，静态资源还是靠这个戳。

## 44. 2026-11-06 0.8.0 交接：质押次数口径替换转账笔数

- **需求**：起始地址的「历史交易」「日均频次」不该数转账笔数，要数**子网质押买卖次数，质押和解质押都算**。
- **接口事实（实测，别再猜）**：`GET /v1/subnets/stake-events` 的 `action` 合法值只有 `stake` / `unstake` / `all`（大写会报 `unknown variant`，错误信息里直接把这三个值列出来）。`action=all` 的 `total_items` **精确等于** `stake + unstake`：地址 `5CCo5n…TFX` 906 = 475 + 431，地址 `5CQBKc…MQ4` 9466 = 5302 + 4164。
- **千万别加 `is_transfer=true`**：那是「质押转移」（coldkey→coldkey 转 stake），加了会把正常质押过滤掉——同一地址 `is_transfer=true` 只有 1 条，`action=all` 是 906 条。原代码 `get_stake_transfer_page` 里带这个参数是为了找**转账边**，跟计数是两回事。
- **一次请求拿全量**：`limit=1` 与 `limit=200` 的 `total_items` 完全一致，所以计数用 `{coldkey, action:"all", page:1, limit:1}`，只读 `pagination.total_items`，不翻页。
- **实现落点**：`app/taostats.py` 新增 `get_stake_event_total(address) -> (int, bool)`，独立缓存 `_stake_total_cache`（别复用 `_stake_cache`，那个 key 是 `(address, page)`，会互相污染），并记得在 `reset_client_state_for_tests()` 里 `clear()`，否则测试之间串数据。`_request_get` + `_reserve_request` 的现成模式照抄，401/429/5xx 分别抛 `TaostatsAuthError` / `TaostatsRateLimitError` / `TaostatsUnavailableError`。
- **字段命名**：新增 `stake_event_count` / `stake_event_scope`。**`transfer_count` 保留但改成同值**——前端老版本读 `profile.transfer_count`，直接删会显示「未获取」。前端 `renderSeedProfile` 优先读 `stake_event_count`，回退 `transfer_count`。
- **文案**：标签「历史交易」→「质押次数」；单位「笔」→「次」；副行「Taostats 全量」→「质押 + 解质押」，「按注册至今 N 天」→「按 N 天算」（原来那句在 205px 侧栏里太长）；本地索引态副行写「本地索引无质押事件，改为转账笔数」——**不要只写「仅本地索引内」**，用户会以为那也是质押数。
- **验证数据**：真实接口 `9266` 那类大数照实显示，别截断；本机实测 `5CQBKc…MQ4` = 9466 次 / 224 天 / 42.26 次/天。
- **踩坑**：探测接口时连发会被本地限速（5 次/分钟）挡住，`429` 不是接口坏了。间隔 13–14 秒再发，或直接 `sleep 60`。另外**测试用的地址必须是真实存在的 SS58**，随便编的会返回 `coldkey is not a valid SS58 address`（400），别误判成参数错误。
- **验收**：**251 passed**（`tests/test_seed_profile.py` +5 条：参数必须 `action=all&limit=1` 且不含 `is_transfer`、缓存第二次不发请求、429 抛限流、无 key 抛鉴权、汇总与日均）。UI 三态（正常 / 本地索引 / 缺失）文案与对齐均正确，1440px 零溢出。

## 45. 2026-11-06 0.8.1 交接：扫描先查画像 + 本地索引也走 API 取质押次数

- **用户的两个问题，答案记这里**：
  1. **质押/解质押不是只能本地索引**——恰恰相反，**本地索引里根本没有**。本地库只有 `stake_transfer` 边（子网 stake 在 coldkey 之间的转移），没有 `stake` / `unstake` 操作事件，所以拿本地数据数出来的永远不是质押次数。真实次数只能走 `GET /v1/subnets/stake-events?coldkey=<地址>&action=all` 的 `pagination.total_items`。
  2. **API 调用次数：固定 2 次**。`/api/account/latest/v1`（注册日期 + 余额构成）1 次，`/v1/subnets/stake-events?limit=1`（只读分页总量、不翻页）1 次。都吃 5 分钟缓存，命中即 0 次。
- **0.8.0 留下的口径 bug（本轮修掉）**：`_seed_profile` 里原写 `if data_source == "taostats" and taostats_is_configured()`，选本地索引时直接跳过 API，退回「本地索引转账笔数」。更糟的是**配了 key 但被限流**时也会退回，于是「本地转账 1 笔」被显示成「质押 1 次 / 日均 0.0 次/天」，看起来像这个地址从没质押过。现在：只要配了 key 就调 API；失败就如实「未获取」；只有**完全没配 key** 才退回本地笔数并标注口径，且此时**不算日均频次**（两种口径相除是假数字）。
- **新流程（用户要求的「先显示左栏、再继续扫描」）**：
  - 新增 `POST /api/seed-profile`（`SeedProfileInput{seed, data_source}`）→ `{seed, seed_profile, api_requests, warnings}`。服务层是 `LinkService.seed_profile_snapshot()`，只查画像不跑关联，2.6 秒返回。
  - 前端 `runRelations()` 第一步 `await prefetchSeedProfile(seed, source)`：先把卡片显示出来（四项写「查询中…」），拿到数据 `renderSeedProfile()` 填真实值，**返回本次真实花掉的请求数**；随后才走 `/api/relations` 或 `/api/relations/jobs`，把这个数放进 `prefetched_requests`。
  - 后端 `RelationContext.prefetched_requests` → `relations()` 的爬取预算 `5 - validator_api_requests - prefetched_requests`；`RelationManager.create(..., prefetched_requests=)` → `_run_round_locked` 里**只在 `rounds_done == 0` 时扣**（后续轮次间隔 62 秒，限速窗口已重置，再扣就白白少查）。
  - 结果新增 `seed_profile_requests`（画像那几次）与 `api_requests_total`（含画像总计），`job.payload()` 暴露 `prefetched_requests`。副标题显示「+ 起始地址画像 N 次」，分轮进度显示「起始地址画像已用 N 次」。
- **别把画像请求算成 0 或 2 硬编码**：`get_account_profile` / `get_stake_event_total` 都返回/带 `from_cache` / `cached`，`_seed_profile` 按真实命中情况累加 `api_requests`，只有真的发了网络请求才 `_record_api_use()`（新增的封装，包了 try/except，统计失败不影响主流程）。硬编码会让「缓存命中却扣额度」或「实际发了却没记用量」。
- **时序实测数据（下次改这块拿这个对比）**：3.1 秒左栏出现「查询中…」→ 5.5 秒填真实数字 → 11.9 秒关联结束渲染。CDP 探针 `.probe/chk_seed_first_081.py` 直接点按钮、不 mock 前端，用 `performance.now()` 记 `/api/seed-profile` 与 `/api/relations` 的完成时刻，外加每 100ms 采一次左栏文本。
- **探针两个坑（踩过，别再踩）**：
  - 卡片搬到 `#seed-card-slot` 后，如果 `#result` 还是 `hidden`，量出来是 `0×0`、`overlap` 全 `true`——必须先在探针里 `classList.remove('hidden')`。
  - Chrome 的 `--user-data-dir` **必须给绝对路径**。给相对路径（`.probe/xxx`）在 Git Bash 下会静默失败，`/json/list` 拿不到 `type=page`，报 `scheme isn't ws or wss`。另外机器上常驻的用户 Chrome 会抢 profile，`--disable-extensions` 也建议带上，否则列表里第一个是扩展的 background_page。
- **验收**：**264 passed**（新增 `tests/test_seed_profile_endpoint.py` 8 条 + `tests/test_seed_profile.py` 5 条）。真实端到端：`5CQBKc…MQ4` 画像 9,468 次 / 42.27 次/天 / 余额 37.45（可用 16.51 + 质押 20.94）；随后带 `prefetched_requests=2` 扫描 → `api_requests=3`、15 个关联、无 429。排版 1440/1180/1080 三档 `fitsSidebar=true`、右边缘统一 207px；窄屏 950/900/760/560/480 `inSlot=true`、≤760px 降两列、零溢出。
- **给下一任**：改动画像相关的任何请求路径，都先想清楚「这次请求会不会和本轮 5 次额度打架」。画像跑在扫描之前，是唯一一个**在用户按下按钮后、关联开始前**就消耗额度的东西，`prefetched_requests` 这条链路断了就会稳定出现 429。

## 46. 2026-11-07 0.8.2 交接：路径明细重排 + 两个 CSS 作用域坑

- **用户报的问题**：路径明细排版不合理、不美观，能排一行就排一行；「收起明细」字体颜色不够突出，展开后不好找。
- **改前实测数据（下次改这块拿这个对比）**：1440 宽 / 内容区 1164px，六列宽分别是 `跳数 582 / 方向 174 / 类型 92 / 金额 69 / 时间 159 / 区块 88`。每跳行高 **62px**（地址折成两行）。
- **改后**：`跳 44 / 路径 632 / 流向类型 118 / 金额 104 / 时间 150 / 区块 88`，行高 **34–35px**，2 跳与 6 跳都 `allOneLine=true`、`wayTruncated=0`。
- **三个必须记住的坑**：
  1. **`#related > table th:nth-child(n)` 会打到内层表格**。路径明细是嵌在外层关联表 `<td colspan=9>` 里的**第二张表**，后代选择器把 9 列宽度套到 6 列上，「跳」列被撑到 712px。收窄成 `#related > table > thead > tr > th` 才对。改任何 `#related` 表格样式都要先想「这会不会命中内层 path-table」。
  2. **`table-layout:fixed` 下 `<colgroup><col style="width">` 会被忽略**。第一次就是写 colgroup，`getComputedStyle(col)` 读到 84/169/226/200/288/169 原样不动。宽度必须写在 `thead th` 上（本次给 th 加了 `c-hop/c-way/c-kind/c-amount/c-time/c-block` 类）。
  3. **深灰主题的 `--accent` 是浅色**（#a9b5a6），实心按钮用 `color:#fff` 对比度只有 2.13，肉眼几乎看不见字。新增 `--on-accent` 变量：默认/护眼 `#fff`，系统暗色 `#0d1418`，深灰 `#151815`。以后凡是「--accent 底 + 文字」的按钮都用它，不要再写死 `#fff`。四个主题实测对比度 6.05 / 8.39。
- **明细行的 HTML 结构**：`.path-detail-row > td > .path-detail > (.path-detail-head + .path-scroll > table.path-table + .path-detail-foot)`。外层 `td` 必须 `padding:0`（写成 `.table-wrap tbody tr.path-detail-row > td` 提权，否则被后面同权重的基础表格规则盖掉），留白交给 `.path-detail` 的 padding。
- **收起入口给了两个**：头部右侧常驻 + `hops.length > 3` 时底部再一个。两个按钮都带 `data-path-toggle`，事件用 `closest()` 委托，所以点哪个都能收；收起时只回填行内那个按钮（明细整块被 remove 掉，不需要复位）。
- **顺带修的时区口径**：日均频次天数原来是 `utcnow().date()`，在北京时间 00:00–08:00 之间比实际少一天。`tests/test_seed_profile.py::test_profile_combines_account_and_transfer_total` 就是凌晨跑才失败的（用 `date.today()` 对照）。已改 `to_beijing(utcnow()).date()`。
- **CHANGELOG 标题别用两个 `·`**：`version_info.ENTRY_PATTERN` 是 `^##\s+(version)\s+·\s+(title)\s+·\s+(date)$`，标题里再出现 `·` 会解析失败、`entries[0]["version"]` 变成上一个版本，`test_version` 直接红。用「、」分隔。
- **验收**：全量 **268 passed**（新增 `tests/test_path_detail_layout.py` 4 条，锁列宽写法与 `--on-accent`）。探针 `.probe/chk_path_082.py`（真实历史结果展开/收起/再展开）、`.probe/chk_path_long_082.py`（注入 6 跳测单行与底部按钮）、`.probe/chk_path_theme_082.py`（亮/深灰主题对比度 + 外层表未受影响）。
- **Chrome 探针仍然要注意**：`--user-data-dir` 必须绝对路径 + `--disable-extensions`，且每次换端口/换 profile 目录，否则前一次的残留进程会占住目录导致拿不到 `type=page`。

## 47. 2026-11-07 0.8.3 交接：关联层级放到 10 级 + 扫描阈值本地保存

- **用户两条需求**：①「地址关联层级提高到 10 级，默认层级仍然为 3 级……层级提升所有规则都和前面的一样」；②「单边最低和累计最低 TAO 增加本地保存按钮，让挖掘层级数较高时我会把单边最低 TAO 提升」。
- **层级上限只留一个真源**：`app/relation_config.py` 的 `MAX_RELATION_DEPTH = 10` / `DEFAULT_RELATION_DEPTH = 3`。改层级时**必须**同步检查这五处引用，任何一处漏改都会出现「后端放开了、前端选不到」或「选到了、导出 422」：
  1. `relation.discover()` 的 `max_depth` 参数与 `if max_depth < 0 or max_depth > MAX_RELATION_DEPTH` 校验；
  2. `main.RelationInput` / `RelationJobInput` 的 `le=`；
  3. `main.export_relation_history()` 的 `depth: int | None = Query(...)`；
  4. `service.export_relation_history()` 里 `1 <= depth <= MAX_RELATION_DEPTH`；
  5. 前端三处层级下拉——**它们现在由 `applyDepthOptions()` 按 `/api/overview` 的 `max_depth` 动态生成**，模板里只剩空的 `<select data-depth-options>` 壳子。要在模板里加新的层级下拉，给它加 `data-depth-options`（可选 `data-depth-label`）即可自动跟着上限走，**不要再写死 `<option>`**。
- **规则一致性**：层级只是 BFS 的 `depth >= max_depth` 截止条件，没有为深层另写一套逻辑。第 2 跳及更深仍然要过 `min_edge_tao`，最终地址仍然要过 `min_total_tao`，交易所/验证者/公共地址/「系统」仍然查到即截止（`depth >= 2` 时小额边直接 `continue`，不会成为路径）。测试 `test_deeper_levels_use_the_same_threshold_rules` 和 `test_deeper_levels_stop_at_terminal_addresses` 就是锁这两条的。
- **一个不显眼但很重要的连带改动**：分轮作业原来固定 `DEFAULT_MAX_ADDRESSES = 150`。层级提到 6 级以上时，第 4 层没走完就会命中「发现地址已超过 150 个上限」，用户会误判成「没有更多关联」。现在 `address_cap_for_depth(depth, base) = max(base, depth × 50)`（3 级仍 150、10 级 500，可用 `BITTENSOR_JOB_ADDRESSES_PER_DEPTH` 调），并在 `payload.max_addresses` 里回传实际生效值。**以后再加层级或改扇出，记得一起看这个护栏。**
- **阈值保存的关键决策**：
  - 存在 `data/settings.json` 的 `scan_thresholds`，和 API key 同文件。`settings_store` 的 `_read()` / `_write()` 是整文件读改写，`save_scan_thresholds()` 会先 `_read()` 再写，所以**不会**冲掉 `taostats_api_key` 和 `address_watch`（`test_thresholds_do_not_clobber_api_key` 锁这条）。加新字段照这个模式写。
  - 用 `Decimal` 规整成字符串，不用 float：`10.00 → 10`、`0.50 → 0.5`。阈值最终要喂给 `LinkPolicy` 的 `Decimal`，走 float 会出现 `0.10000000000000001` 写回页面。
  - **非法输入用"保持上次值"而不是"回落出厂值"**：用户误触保存时，把辛苦调的 10/50 悄悄改回 1/1 比报错更糟。`_clean_threshold(value, fallback)` 的 `fallback` 传的是 `load_scan_thresholds()` 读到的当前值。
  - 按钮三态：`""`（与已保存一致）/ `is-dirty`（改过，文案「保存阈值 *」）/ `is-saved`（保存成功，「已保存」，1.8 秒后自动回到 `syncThresholdSaveState()`）。输入框 `input` 事件和回车都接上了。
- **前端一个边界坑**：层级下拉在 `/api/overview` 返回前是空的，此时点扫描 `Number("") = 0`，后端 `ge=1` 直接 422。`scanPayload()` 现在写 `Number($("#depth").value) || state.defaultDepth` 兜底。`state.defaultDepth` 初始值就是 3，不依赖接口返回。
- **路径明细支持 10 跳**：`.path-scroll` 从 `overflow-x:auto` 改成 `overflow:auto` + `max-height:380px`，表头 `position:sticky; top:0; z-index:1`。2～3 跳完全放得下不出现滚动条，10 跳在明细内部滚，不会把关联地址列表推出屏幕。
- **验收**：全量 **293 passed**（新增 `tests/test_depth_and_thresholds.py` 25 条）。原 `tests/test_relation_history.py` 里 `depth=9` 越界断言已改成 `depth=11`（9 级现在是合法值，这条不改会红）。
- **探针**：`.probe/chk_depth_083.py` —— 检查 `#depth` 选项 1..10 / 默认 3、三处下拉各 11 项、阈值出厂回填、改值后按钮变 `is-dirty`、保存后 toast 与 `is-saved`、刷新后回填、`scanPayload()` 的 depth、1440 与 900 宽的行内对齐与溢出。`.probe/job6_083.py` —— 真实跑 6 级分轮作业。**探针会写真实的 `data/settings.json`**（走的是同一个接口），跑完记得 `POST /api/settings/scan-thresholds {"min_edge_tao":"1","min_total_tao":"1"}` 复原，别把测试值留给用户。
- **踩过的坑**：验证时用了一个从截图里截断的 45 位地址 `5CQBKcMbMrvGkdDkMgGmFqRhKmZnFqCwHqSPmZqPjyMQ4`，Taostats 一直返回 `HTTP 400 / Invalid SS58 address: Length is bad`，一度以为是限流或接口改动。**先用 `is_valid_ss58()` 确认长度 48 再排查网络**，能省半小时。

## 48. 2026-11-07 0.8.4 交接：关联明细检索的跳数 / 翻页上限跟层级对齐

- **用户需求**：「已知地址库的地址关联明细检索，最多跳数和翻页上限要和最大层级一致。」即 0.8.3 把关联层级放到 10 级之后，地址库页那个检索面板还卡在 3 跳 / 20 页。
- **改之前必须先知道的事**：`interlink.py` 的路径搜索**不是**通用 BFS。原 `_search_paths()` 只写了两段——「1 跳查共同对手方」和「2 跳从邻居里挑中转」，最后一段还有 `if paths or query.max_hops < 3: return`。也就是说**光把 `le=3` 改成 `le=10` 是没用的**，4 跳以上永远返回空。所以这次是先把搜索重写成 `_walk_paths()`（双向分层 BFS），再放开参数。以后谁要再动跳数上限，先确认搜索本身是否支持那个深度。
- **上限的真源**：`interlink.MAX_PAGES = MAX_RELATION_DEPTH`（写在 `interlink.py` 顶部，注释已说明为什么两个数要绑一起）。`InterlinkQuery` 两个字段的默认值都用 `DEFAULT_RELATION_DEPTH`。三处同步点漏一个就会「参数收下了但没用」：
  1. `main.InterlinkInput` 的 `max_hops` / `max_pages`（`ge=1, le=MAX_RELATION_DEPTH`）；
  2. `service.interlink_search()` 里的 `max(1, min(MAX_RELATION_DEPTH, …))`（这是第二道夹子，绕过接口直调 service 也拦得住）；
  3. 前端 `applyDepthOptions()` 里生成 `#interlink-hops` 选项和写 `#interlink-max-pages` 的 `max`。模板里只留 `data-hop-options` 的空壳，**不要再写死 `<option value="2">`**。
- **跳数下拉从 2 起不是笔误**：1 跳意味着起始地址和对方地址直接相连，那就是「直连」而不是「链路」，所以选项是 `2..10`，文案「中间最多 N-1 个地址」。默认值取 `min(preset, max)`，`preset` 就是层级默认值 3。
- **双向 BFS 的两个坑**（都在代码注释里写了，别改回去）：
  1. **两岸必须交替发请求**。第一版是 `expand(source)` 走完再 `expand(target)`，而 `_Fetcher` 预算固定 5 次/分钟 —— 左岸在第 4 跳就把额度吃光，右岸 `layers` 是空的，于是 `if not left_layers or not right_layers` 直接返回「没能取到双方转账流水」。表现是 4 跳以上 100% 失败，但**看起来像数据不够**。改成 `for side in (left, right)` 交替后 5 次预算能稳定展开到 3 跳。
  2. **逐跳方向要按 `from`/`to` 校正**（`_oriented_step`）。从 target 侧回溯出来的那一半，邻居堆是按 target 视角建的，直接取 `_best()` 会把箭头画反。现在两个方向各传一堆，优先取 `from == 当前节点` 的那笔。
- **`_Side` 的深度跟踪用 `(地址, 距离)` 队列**，不要用「当前层号 + advance()」那套。中间试过一版 `depth` / `advance()` 的写法，在两岸交替推进时层号会算错，深路径回溯会断。现在 `dist` 在入队时就定死，`deepest` 直接取 `max(dist.values())`。
- **提示语是被测试锁住的**：「已取到的分页会缓存 5 分钟…再点一次会接着往深处走」不是安慰话，`test_second_pass_uses_cache_and_finishes` 会跑 4 轮验证 8 跳链路真的能凑出来（第 3 轮），并且断言第一轮 `cache_hits == 0`、最后一轮 `cache_hits > 0`。**如果以后改了缓存策略或预算逻辑，这条会红，别直接删断言。**
- **测试里造数据要带旁支**：`build_chain(hops)` 的 `branches` 参数。纯链在 5 次预算内就能走完（每层只有 1 个候选），测不出「预算不足」分支；`branches=2` 才是真实地址的样子。我第一次写这条测试就是因为用了纯链而失败，误以为实现有问题。
- **验收**：全量 **310 passed**（新增 `tests/test_interlink_depth.py` 17 条）。前端探针 `.probe/chk_interlink_depth_084.py`。**探针要注意 `#view-library` 默认 `hidden`**，不先 `classList.remove("hidden")` 量出来全是 0×0 —— 这个坑 0.8.2 踩过一次，这次又踩了。
- **遗留**：`--headless=new` 下探针里 `#interlink-group-field` 因为切到了地址模式而被隐藏，尺寸为 0 属预期，不是布局 bug。要量组模式得把 `#interlink-mode` 改回 `group` 再量。
## 49. 2026-11-07 0.8.5 交接：路径明细地址改回头尾各 4 位

- **用户需求**：「路径明细的地址显示只需要头尾各4位，当前是头尾各6位。查询记录和扫描关联结果都要同步改。」
- **改动只有一处**：`app/static/app.js` 的 `pathAddr()` 改成直接 `return shortAddr(value)`。原来它自己写了一套 `${text.slice(0, 6)}…${text.slice(-6)}`（0.8.2 重排明细表时为了让「人工比对」多显示两位），而主表走 `shortAddr()` 的头4尾4，于是同一屏出现两种长度，同一个地址看着像两个。
- **「查询记录也要同步改」不需要改两遍**：扫描结果与查询记录回看共用同一条渲染链 —— `renderResult()` → `relatedRowsTable()` / `pathDetailRowHtml()` → `pathAddr()`。查历史时 `viewHistoryRecord()` 只是把归档的 `result` 交给 `renderResult()`，所以改 `pathAddr` 两边同时生效。**以后遇到「主结果和历史记录都要改」的需求，先确认是不是同一条链，别去写两份。**
- **别再把截断规则分散**：全站地址截断现在只有 `shortAddr()` 一处。`pathAddr()` 保留成一层薄封装是为了让明细表的调用点语义清楚（万一以后真要对明细特殊处理），但它内部不许再出现 `slice(`。`test_path_detail_address_uses_the_same_truncation_as_main_table` 锁住了这一点：既断言 `shortAddr` 的 4/4 写法还在，也断言 `pathAddr` 函数体内没有自己的 `slice(`。
- **文案要跟着改**：地址位数在文档里是**明写**的，改代码不改文档会自相矛盾。这次动了 `使用说明.md`（「头 6 + 尾 6（比主表多两位，方便人工比对）」→「头 4 + 尾 4（和主表同一套规则）」）并用 `tools/build_usage_html.py` 重建 HTML；`README.md` 新增第 53 条；`docs/VERIFICATION.md` 里 0.8.2 那条「够放头6尾6两个地址」是当时的实测事实，**不改原句，只在后面加一句「0.8.5 已统一成头4尾4」**，保留历史可追溯。
- **验收**：全量 **311 passed**（新增 1 条）。探针 `.probe/chk_path_addr_085.py`，真实历史记录 #59 逐跳地址 `5Gy8…jEbt / 5HDo…D7QQ / 5FUR…tzHe / 5GBn…iEUi` 全部 9 字符、`anyLegacy13=false`、主表与明细同地址文本一致、路径列无折行。
- **探针踩的坑**：手搓 `renderResult({related, seed})` 会崩在 `data.warnings is not iterable`（`renderResult` 还要读 `warnings` / `limitations` / `depth_distribution` 等一批字段）。正确做法是直接调页面自己的 `viewHistoryRecord(id)` 走完整流程，别自己拼一个半成品 result 对象。


---

# 第二部分：0.8.6 – 0.8.19（2026-11-07 ～ 11-08）

> 这一批版本围绕三件事展开：**地址库归属正确性**、**多设备/远程访问**、**网络出站可靠性**。
> 每条都记录了「为什么这么做」和「改坏了会怎样」，接手时优先看这些而不是看代码。

## 50. 0.8.6 地址库清单搜索 / 角色筛选，以及"标记即归系统分组"

- **用户现场**：把一条 Thor 结尾的地址标为公共地址，去「已知地址库」按公共地址找却找不到；更糟的是当时新建的库（6NCK）把这条公共地址也收进去了。
- **两层根因**（以后遇到"标记了但找不到"先查这两条）：
  1. **可发现性**：清单只有一个「按地址库」下拉，上千条里定位一条标记过的地址只能肉眼翻。→ 新增搜索框（匹配地址/名称/地址库/备注/角色）+ 角色筛选（公共地址/交易所/验证者），与「按地址库」可叠加。
  2. **归属**：`decide_public_address` 从来不清理 `book`，标记后地址仍挂在用户自建库下；而「新建地址库并导入」的排除逻辑**误用了系统分类常量 `"系统"`**，公共地址用 `PUBLIC` 永远匹配不上，于是被误收进库。
- **修复口径**：系统角色集合统一为 `{EXCHANGE, VALIDATOR, PUBLIC}`。标记公共地址 / 确认交易所时清空 `book`，归入系统身份分组；新建库、导出 TXT 一律按这个集合排除。
- **别改回去**：`SYSTEM_CATEGORIES` 曾同时承担"系统分类"和"排除名单"两个语义，是这次误收的根源。凡是要"排除系统角色"，用角色集合，不要用单个分类常量去比。

## 51. 0.8.7 / 0.8.8 地址掩码统一 + 跨机同步包 LWW

- **0.8.7**：地址库清单的地址列从完整 48 位改成 `头4…尾4`（`shortAddr()`），旁边配「复制」按钮复制完整地址，整行 `nowrap` 不再折行。全站地址截断**只有 `shortAddr()` 一处真源**，别在别处再写 `slice(`。
- **0.8.8**：导出同步包 `schema_version` 升到 **2**，每条地址带 `created_at` / `updated_at`，导入走**最后写入优先**：
  - `book` 允许被较新空值清空（专治"某台把公共地址移出用户库后，另一台残留的旧 book 清不掉"）；
  - `label` / `note` 取较新一方，但**空值不覆盖非空**（防止误清人工备注）；
  - `categories` 取并集（分类是附加信息，不丢）。
- **向后兼容**：无时间戳的旧包（TXT / 老 CSV / v1 JSON）仍走原保守合并。这条必须保住——用户手里有大量旧包。

## 52. 0.8.9 「整理地址库」一键修复污染

- 跨机同步、多次导入之后会残留三类脏数据，工具栏「整理地址库」一次修干净：
  1. 多分类里的**孤立分类名**（不在分类表、也不是系统分类）；
  2. **系统角色误挂用户库**（交易所/验证者/公共地址残留 `book`）；
  3. **残留的交易所锁定标记**（`exchange_confirmed` 为真但已不是交易所）。
- **只做安全规整**：不删地址、不改人工标签/备注；官方名单与内置固定名单的结论**一律不动**。
- **接口** `POST /api/library/tidy`，`dry_run` 先预览再落库；落库写 `kind="tidy"` 快照，复用「撤回上次导入」还原。
- **一个差点误伤的点**：第一版把"空 `categories`"也当污染，真实库报出 **370 条**要改——全是空字段误报。收紧为**只比较有效分类集合**后才 `changed=0`。以后加规整规则，先在真实库上 `dry_run` 看数量，别信单元测试。

## 53. 0.8.10 「扫描最新块」必然超时的真凶

- **现象**：重启服务后点扫描连续失败，界面只显示 `RPC scan failed:`（冒号后是空的，因为 `TimeoutError.__str__()` 是空串）。
- **根因**：`RPCCollector.collect_range()` 用**同一个 25 秒超时**去等两件量级完全不同的事——读一个块（1~6 秒）和 `init_runtime()` 拉 runtime WASM/metadata（冷启动实测 **25~133 秒**）。所以每次都在读第一块之前就超时。
- **为什么是重启触发的**：runtime 缓存随进程消失，冷启动必然重拉。改地址库功能本身没碰扫描代码，但**重启让这个长期潜伏的设置暴露了**。
- **修复**：拆成独立预热阶段（`warmup_timeout_seconds` 默认 240 秒）+ 单块读取仍 25 秒；结果回传 `warmup_seconds`；租约 3 分钟→8 分钟；错误信息带异常类型。
- **顺带修第二个坑**：被 `taskkill` 强杀后，`ScanJob` 残留 `running` + `ScanLock` 租约还在，新进程一点扫描就 409 卡 8 分钟。现在 `Database.initialize()` 调 `recover_interrupted_scans()` 清理。
- **教训（重要）**：用户当时质疑"你改坏了什么"，查 `git log -S "timeout_seconds: float = 25.0"` 证明该超时来自 **0.1.0**，0.8.9 没碰 collector.py——**是重启暴露的旧缺陷，不是新引入的**。遇到"升级后坏了"，先查这个值来自哪个版本。

## 54. 0.8.11 巡检给手动查询让路（用户自定规则）

- **背景**：Taostats 每分钟 5 次，巡检和手动查询共用**同一个进程级滑动窗口**，先到先得、满了抛错不排队。巡检每 30 秒固定推进 1 个地址（约 4 次/分钟），用户点查询时往往只剩 1 次配额。
- **规则（用户原话定的口径，别自行简化）**：
  - 判定不是"有没有 running"，而是**任务有没有收尾**：`pending / running / waiting / finalizing` 全算占用，**轮间 62 秒的 waiting 也算**（否则会趁这 62 秒插进去抢配额）。
  - 收尾后再静默 **120 秒**才恢复（覆盖"用户紧接着再点一次"），冷却从**最后一条** `finished_at` 起算。
  - 锚点用 `finished_at` 而不是"tick 时看没看到"：tick 是 30 秒一次，秒回的查询会在两次 tick 之间溜过去。
- **僵死兜底**：用**进度签名**（状态/轮次/请求数/待查队列）判定，连续 **10 分钟**毫无变化才按僵死放行；合法长任务（补录跑几小时）照常让路。
- **顺带修掉一个吞地址的 bug**：`_check_address()` 在 `rate_limited` 时仍写 `last_checked_at` 和 `checked_count += 1`，等于把"一个数据都没拿到"的地址标成已巡检，要等约 2 天才会再查。现在限速时游标原地不动、不入库。
- **参数**：`BITTENSOR_WATCH_QUIET_SECONDS`(120) / `BITTENSOR_WATCH_STALL_SECONDS`(600)。

## 55. 0.8.12 / 0.8.13 多设备与远程访问（当前形态）

- **结论先行（甲方案）**：**一台主机跑服务与巡检，其余设备只开浏览器**。不要每台各跑一份再同步库。
- **为什么**（三条硬理由，别再试第二种）：
  1. 限速器 `_reserve_request()` 是**进程级** deque，两台各跑一份 = 同一把 key 合计 10 次/分钟 → 429，两边都显示"被限流"；
  2. 巡检每地址 2 请求 × 685 地址，双跑 = 同一批查两遍，额度翻倍、产出为零；
  3. SQLite 单写，想"看到同一个库"只能访问同一个进程；git 同步包是定期对账，不是协同。
- **`run.py --remote`**：host 自动改 `0.0.0.0`，打印本机/Tailscale(`100.64.0.0/10`)/局域网地址 + 访问口令。
- **访问口令**（`app/access.py`）：16 位（去掉易混的 I/O/0/1），存 `data/settings.json` 的 `access_token`；回环免验证，其余凭 HttpOnly Cookie(90 天) 或 `X-Access-Token`；`hmac.compare_digest` 防时序试探。
- **中间件顺序不能反**：`request_guard` 先口令后授权。反了会把"有没有激活、机器码是多少"漏给同网段任何人。
- **0.8.13 公网隧道**：`启动-公网隧道.cmd` → `tunnel.ps1` 下载并运行 cloudflared，主机**主动出站**连 Cloudflare，不开端口、不需公网 IP、客户端零安装。
- **同时修掉一个必须修的越权洞**：隧道把所有外部请求的对端都变成 `127.0.0.1`，原来的"回环即本机"判定会让公网访客**免口令进站**、甚至 `GET /api/access/token` 把口令读走。现在只在**TCP 对端本身是回环**时才采信 `CF-Connecting-IP` / `X-Forwarded-For`，局域网设备伪造同样的头无效（`WALLET_LINK_TRUST_PROXY` 仅 `--tunnel` 打开）。
- **实测抓到 uvicorn 的默认值坑**：`client` 已显示真实公网 IP 但 `via_tunnel=False`，说明显式解析没生效——是 **uvicorn 默认 `proxy_headers=True`** 抢先按 XFF 改写了 `scope["client"]`。等于把判定权交给 `forwarded_allow_ips`（可被环境变量放宽成 `*`）。已显式 `proxy_headers=False`。

## 56. 0.8.14 手机排版：整页空白的真凶是 `height:100vh`

- **现象**：手机打开近乎全空白，只有一条导航横条**悬在屏幕中央**，内容要再滚一整屏。
- **根因**：基础样式 `.sidebar { height:100vh }` 只该属于桌面端。≤950px 断点把侧栏改成横向条，但**高度没重置**，仍占满一屏；`align-items:center` 把导航垂直居中，`main` 从一整屏之下才开始。
- **另外三处**（共性都是 flex 不换行 + 中文按字符断行）：`.section-title` 标题被压成一字一行；`.library-actions` 不换行 → 撤销/合并/整理/导出被切出屏外（实测右缘 410 > 视口 390）；地址库分组组名被挤成竖排。
- **取证方法（以后排查前端直接复用，别靠肉眼看）**：桌面 Chrome 的 `--window-size` **最小宽度是 500px**，直接传 390 会被钳住、量出来全是假象。必须用无头 Chromium + CDP `Emulation.setDeviceMetricsOverride`（width/height/deviceScaleFactor/mobile）。脚本模板见 `.probe/overflow_probe.js`（node 原生 WebSocket，无第三方依赖）。
- **验证口径**：三个标签页 `overflowX` 全部 false；1400px 桌面回归截图与修复前一致。

## 57. 0.8.15 说明文档结构（内容在但看不到）

- 远程访问/隧道内容 0.8.12 就写进《使用说明》了，但挂在 `## 十、拷贝到另一台电脑` 下面的 `### 三级标题`，而离线 HTML 目录（`build_usage_html.py` 用 markdown toc）**只收二级标题**——用户翻目录找不到，反馈"文档没更新"。
- 已提为 `## 十一、多设备访问`，并纠正做法编号错序（原"做法三 公网隧道"排在做二之前）。
- **文档结构原则**：会被读者"按目录找"的内容，必须是 `##` 级。三级标题只用于同一主题下的细分。

## 58. 0.8.16 查询失败被误报成"没有 API key"——出站必须走代理

- **这是本项目最隐蔽的一个坑，务必读完**。
- **现象**：查询失败提示"没有可用的 API key"，但 key 明明配着（`/api/overview` 返回 `taostats_configured=true`）。
- **真因**：这台机器**直连 taostats.io 超时**（同一时刻实测：直连 `ReadTimeout 8.5s`；经本机 Clash `127.0.0.1:7890` **HTTP 200 / 1.2s**）。而 **httpx 只认环境变量、不读 Windows 系统代理设置**，服务一直在直连 → 全部超时 → 查询静默退回本地索引（`api_requests=0`、`LOCAL_FALLBACK`）→ 前端把"0 次请求 + 退回本地"判成"没有可用的 API key"，**把排查方向带偏了整整一轮**。
- **修复三层**：
  1. `app/taostats.py` 新增出站决策层：`WALLET_LINK_HTTP_PROXY`（`none`=强制直连）→ **WinINET 注册表系统代理** → `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY` → 直连；失败**拉黑 600 秒自动换路**并打 `[taostats] 出站请求走 …：原因`；`trust_env=False`。所有出站收敛到 `_http_get()` 单一接缝（测试只需替换这一个函数）。
  2. `api_error_kind`（`auth` / `network` / 空）从 crawl → service → jobs → 前端贯通，两种失败给完全不同的提示。
  3. `/api/diagnose` 新增「出站网络（taostats.io）」检查项，逐条 HEAD 探测（不带 key、不占额度，60 秒缓存）。
- **测试迁移坑**：改 `httpx.get` → `_http_get` 后 13 处旧桩失效，统一迁到 `tests/http_stub.py::as_http_get`。**用正则批量插 import 会插进多行 import 中间**，必须按括号深度定位。
- **并发陷阱**：某用例单独跑过、和其他用例一起跑就卡 90 秒超时——前一个用例把 key 留成已配置，后一个真发外网。而且**只桩 `service_module.get_transfer_page` 不够**，`get_stake_transfer_page` 是另一个独立引用。定位手法：给 `socket.socket.connect` 打 traceback 钩子（见 `tests/_net_trace.py` 的写法），直接看到调用栈。

## 59. 0.8.17 – 0.8.19 地址库归属的最终规则（**当前生效，改前必须读**）

- **根因（一句话）**：`create_address_book()` 建库时把库名**同时**写进 `book` 和 `label`（`label=book_name`）。而地址库清单的分组（`library_group_key`）和扫描结果的库名徽标**读的都是 `label`**。所以"只清 `book`"是不够的。
- **判据（极其重要，别改错）**：只有 **`label == book`** 才算"库名复制品"，且必须在清 `book` **之前**判断。仅凭"label 有值"会把**人工命名**（用户自己起的名字）和**官方交易所名单**（Kraken/Binance 的 label 就是交易所名）一起误伤。真实库验证：12 条候选里 11 条是官方名单（`is_book_name=0`），只有 1 条命中。
- **五种操作的统一行为（0.8.18 定型，用户明确要求）**：

  | 操作 | 移出库 | 标签归位 |
  | --- | --- | --- |
  | 确认交易所 | ✅ | 人工确认交易所 |
  | 否定交易所 | ✅ | 人工否定交易所 |
  | 重置为待确认 | ✅ | 清成未命名，按分类归组 |
  | 标记公共地址 | ✅ | 公共地址 |
  | 取消公共标记 | ✅ | 清成未命名，按分类归组 |

  口径是"**只要下了人工结论就移出库**，以后关联到别的库再单独加回去"。
- **两条入口必须一致**：`decide_exchange_address()`（扫描结果按地址）与 `decide_exchange(row_id)`（地址库清单按行）都走同一个 `move_out_of_book()`。历史上这两条出现过不一致（后者漏清 `book`），**加任何新结论入口都要走这个函数**。
- **顺序坑**：必须**带原 label** 调用 `move_out_of_book`（内部靠 `label == book` 判断是否复制品），**然后**才把该清掉的自动标签清成未命名。反过来先清 label 会导致判据失效、库归属残留。
- **库搬空要清台账**：`prune_empty_book()` 否则「按地址库筛选」里会留一个"N 个地址"实际为 0 的空壳。
- **历史数据修复工具**：`tools/fix_exchange_label_residue.py`，只处理"已确认/已标公共 + 已不在库 + label 恰为现存库名"，**默认只预览**，`--apply` 才写库。
- **0.8.19**：启动面板直接印出公网网址。以前只印"见隧道程序打印的 https://…"，而 cloudflared 是**隐藏窗口的后台进程**——用户看不到那个打印，等于没给地址。现在从 `logs/cloudflared.url` 读并直接显示，且**只在 `cloudflared.exe` 存活时显示**（进程退了文件还留着，不能拿死链接骗人）。

---

## 60. 0.8.33 巡检：范围是动态的、进度要看得见、断点能续（**改巡检前必读**）

用户原话：「巡检的地址是固定的，还是会随着地址库增加自动增加」「巡检被中断能不能断点继续。巡检需要一个动态进度窗口」。

**结论：范围动态（每轮现读地址库），断点本来就能续，但界面上完全看不出来，而且查出三个真 bug。**

### 三个真 bug（都已修）

| # | 症状 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 巡检每轮「瞬间完成」，界面永远 0 个已查、0 次请求，长期被当成"正常运行" | `next_targets` 先按「最久没查优先」取前 60 行、**再**过滤交易所/验证者。库里 1194 条验证者大多 `last_checked_at IS NULL`，恰好排最前，60 个名额被占满后全被过滤 → 返回空 → `_advance` 立刻判 `complete` | **先过滤再排序**：在内存里先算可巡检集合，再按「从未查过优先 → 最久没查优先」排 |
| 2 | 公共地址一直被巡检（拿额度换噪音） | `watch.SKIP_CATEGORIES` 只有 `EXCHANGE/VALIDATOR/系统/SYSTEM`，漏了 `PUBLIC`，而 `service.SYSTEM_CATEGORIES` 早就含它 | 补进 `SKIP_CATEGORIES`（可巡检 758 → 754） |
| 3 | 「立即巡检一轮」**从来点不动**，只有 stderr 一行 | `WatchService.run()` 是 **keyword-only**，`_watch_manual_run` 按位置传 `(True, limit, None)` → `TypeError` 被 `except` 吞掉 | 改关键字传参；并把推进量限成 `per_tick*2` |

**第 3 条的连带修正**：原来手动传 `max_addresses=None` 等于无视每日上限跑完整轮（约 1500 次请求、占锁几小时）。现在手动只推一小段，剩下的交回调度器；**每日上限对手动轮次同样生效**——以前 `manual=True` 会被后续所有 tick 继承，等于永久绕过上限。

### 进度窗口（前端）

- HTML：`#watch-progress`（`title / percent / bar / meta / hint` 五件套），`aria-live="polite"`。
- 渲染：`renderWatchProgress(data)`；轮询：`scheduleWatchPolling()`——停在地址库页 5 秒一次，**切走就 `stopWatchPolling()`**，页面隐藏时降到 30 秒。
- 接口：`GET /api/watch/status` 新增 `progress` 字段（`total / checked / remaining / checked_once / never_checked / per_tick / seconds_per_address / eta_seconds / percent / running / resumed / idle_seconds / stale`）。
- 心跳：`WATCH.mark_tick()` 由调度器每 tick 调用（**让路时也要调**，标记 `paused=True`），超过 90 秒无推进且非让路才判 `stale`。
- 断点标记：`PROCESS_STARTED_AT`（模块导入时刻）与 `run.started_at` 比较，早于它且已有进度 → `resumed=True`。

### 一个坑：进度不能读最新那条 run 的计数器

一轮会被拆成多条 `watch_runs`（撞上限停一条、重启再起一条，每条从 0 计数）。直接读 `last_run.addresses_checked` 会让进度**每次被打断就归零**，用户看到「明明查了几百个又变回 0%」。

`_cycle_start()` 的做法：从最新 run 往回走，遇到第一条 `complete` 就把它的 `started_at` 当周期起点；`checked` 则用 `WatchState.last_checked_at >= cycle_start` 在**可巡检集合内**计数。这样进度单调递增，完整跑完一轮后新周期自然重置。

### 基线与验证

- 全量 **486 passed / 0 failed**。
- 相关测试：`tests/test_watch.py`（21 条，含 `test_next_targets_survives_many_never_checked_skipped_rows`、`test_public_address_is_skipped`、`test_progress_does_not_reset_when_run_is_split_by_budget`、`test_interrupted_run_resumes_from_cursor`）、`tests/test_watch_manual_run.py`（3 条，钉死入口签名与推进量上限）。
- 布局回归工具：`tools/check_watch_layout.py`（无头 Chrome + CDP 读 `getBoundingClientRect`，检测横向溢出与统计项重叠；桌面 1400px 与手机 390px 均通过）。

---

## 61. 0.8.36 巡检改本地索引（为什么现在收益是 0%）+ 一次查询 500 的教训

### 61.1 「后续改本地查询」的真实可行性

用户思路：巡检每轮每个地址 2 次请求、754 个地址约 1500 次/轮，太浪费额度，
能否第一遍走 API、之后改本地索引。

**方向对**（本地索引由链上 RPC 直采，完全不消耗 Taostats 额度），但这个库现在的
收益是 **0/1528 次**。三个硬障碍，改代码解决不了，属于数据前提：

| 障碍 | 实测数字 | 为什么致命 |
| --- | --- | --- |
| 索引连续性不足 | 名义 18,254 块，实扫 9,575 块（**52.4%**），缺 8,679 块，含一段 7,715 块的整段空洞 | 用它替代 API 会静默漏转账，漏了无法补 |
| 地址覆盖不足 | 764 个可巡检地址里只有 **54** 个在本地出现过 | 本地无记录 ≠ 没有新转账 |
| 游标过早 | **667** 个地址的 baseline 早于本地索引起点 | 中间那段本地没有数据 |

所以实现的是**开关 + 严格判据**，等在索引追上来后自动生效：

- `WatchService.coverage()`：从 `scan_jobs`（只算 success 且 scanned_blocks>0）
  反推真实扫描区间，再从游标往下回溯找**连续无缝**的一段。**绝不能用
  `[min(block), max(block)]`** —— 那正是 52% 连续性的来源。
- `_local_covers(address, baseline, window)`：三条全满足才算覆盖
  （区间存在、`baseline >= safe_from`、该地址在本地出现过）。
- `local_window()` 返回可信任区间；本地命中时游标推进到 `window[1]`，
  否则没新转账时会反复重扫同一段。
- 配置项 `prefer_local_index`（默认 True），关掉即回到全部走 API。

**另修一个更严重的问题：本地扫描一直追不上链。** 原配置每 300 秒扫 16 块，
而链每 300 秒长 25 块 → **每轮净落后 9 块**，实测已落后 7,541 块（约 25 小时）。
实测采集器 0.32 块/秒 vs 链速 0.083 块/秒（余量 4 倍），纯属配置太低。
现改为 `LOCAL_AUTO_SCAN_BLOCKS = 75`、`MAX_AUTO_SCAN_BLOCKS = 2000`
（`scan(..., catch_up=True)` 才放开，手动接口仍限 16）。

**后续若要真正吃到收益**，需要一次性历史回填（把可巡检地址的转账补进本地
索引）。评估点：按 0.32 块/秒，补 25 小时跨度约 15 分钟；但要让「每个地址都
有记录」，可能需要往回扫几万块，需先确认时间成本可接受。

### 61.2 `/api/relations` + `/api/jobs` 全部 500 的教训（必读）

**症状**：地址扫描完全查不了，服务端 500。

**根因**：`app/service.py` 的 `from decimal import Decimal` 在 0.3.1 之后某次
重构中丢失（`git log -S "from decimal import Decimal"` 可复现），而
`prepare_relation_context()` 里用它构造 `BookPairStat` —— 这是**每一次查询**的
必经之路，于是整条主链路瘫痪。`Decimal` 在该文件只有 2 处引用，平时执行不到。

**为什么测试全绿也没拦住**：`prepare_relation_context` 此前**零测试覆盖**。
513 条测试没有一条打过 `/api/relations`。

**已加的守卫**（`tests/test_relations_endpoint.py`，三条）：
1. 端点冒烟：`POST /api/relations` 返回 200 且 `related` 是 list；
2. 直调 `prepare_relation_context`，断言 `book_pair_stats` 里 `Decimal` 值正确；
3. 模块级 `hasattr(service_module, "Decimal")`。

实测把导入删掉，三条立刻红（2 failed + 1 error）——不是空壳测试。

**教训**：基础主链路的冒烟测试比细节测试更值钱。以后新增/改动
`prepare_*`、`service.*` 这类「所有请求都会经过」的函数，必须有直接调用它的测试。

### 61.3 排查这类问题的顺序（实用）

1. `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8766/api/health` —— 服务活着吗；
2. `tail -40 logs/wallet-link.stderr.log` —— **FastAPI 的异常栈就在这里**，
   直接看最后几行的 `XXXError`，比猜快得多；
3. 复现同一个请求（用 `curl` 打同一个端点），确认不是偶发；
4. 修完先跑针对该端点的测试，再跑全量。

---

## 附：当前基线（0.8.36）

| 项 | 值 |
| --- | --- |
| 版本 | 0.8.36 |
| 测试 | **516 passed / 0 failed**（46 个测试文件） |
| Python | 3.12（`installers/` 内置安装器 + wheels，可离线装） |
| 依赖 | FastAPI / SQLAlchemy 2.x / httpx / async-substrate-interface / cryptography |
| 入口 | `run.py`（`--remote` / `--tunnel` / `--doctor`）、`start.ps1`、`tunnel.ps1` |
| app 模块 | access / builtin_addresses / collector / config / crawl / db / diagnostics / inputs / interlink / jobs / license / main / relation / relation_config / service / settings_store / taostats / validation / version_info / watch |
| 数据 | `data/wallet_link.db`（SQLite）+ `data/settings.json`（key DPAPI 加密 + 访问口令 + 阈值 + 巡检配置） |

**跑测试的正确姿势**（退出码经常不可信）：

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
  .venv/Scripts/python.exe -m pytest -q --junitxml=.pytest_full.xml
```

**pytest 退出码 1 常常是 safe-delete 钩子拦截临时目录，不是测试失败**——以 JUnit 里 `failures=0` 为准。

## 62. 2026-11-11 0.8.45 交接：hotkey-swap 假关联、隧道网址顺序、远程访问按钮

### 一、假关联是怎么来的（**改 stake 事件解析前必读**）

- 用户发现归档 #154 里 `5CVS9d1Nc…` 被判成起始地址的 1 级「强」关联，但关联明细检索说两地址没有直接往来。**用户是对的**。
- 直接调 Taostats `/v1/subnets/stake-events?coldkey=…&is_transfer=true` 拿到原始 2 行，`id` 为
  `hotkey-swap-<block>-<netuid>-<old>-<new>-<coldkey>-in/out`：两行 `coldkey` 都是查询地址，
  `transfer_address` 与 `hotkey` 互为对方、`amount='0'`。这是**同一 coldkey 下两个 hotkey 互换**，
  `transfer_address` 装的是 hotkey 不是对手方 coldkey，没有 TAO 转账。
- `app/crawl.py` 旧逻辑只判 `is_transfer`，`stake_counterparty()` 对 `transfer_address` 只校验
  `is_valid_ss58`（hotkey 也合法），方向按 `action` 推断（代码注释自己承认「不是已验证契约」），
  于是造出两条 0 元直连边。第 1 跳不设金额门槛 → 假边保留 → depth=1 → 避开「≥2 跳降一档」→ 强关联。
- **三道拦截**：`id` 前缀 `hotkey-swap-` / `amount_rao <= 0` / `transfer_address == hotkey`。
  三道都要留：只认 id 前缀，服务端换命名就漏；只认 0 金额，非零的 hotkey 腿还会混进来。
- **别用「本地索引里没有这个区块」否定事件**：本地 `transfers` 只覆盖 9,430,401 起，
  8,181,682 这类老区块本来就不在覆盖范围。这是上一轮误判的来源。
- **两套功能的口径差异**：`app/interlink.py`（关联明细检索）只查普通 transfers 分页 + 本地表，
  根本不读 stake-events，所以它的结论才是「按资金往来」的正确口径。以后两边结论打架，
  先查各自的数据源，别急着说用户看错。
- `8181682-0015` 是 Taostats 的 `extrinsic_id`，不是交易哈希，也不是程序合成键。
- 历史污染：8 个归档 30 条 0 金额假边、29 个地址；**不自动改写已归档 payload**，重查即干净。

### 二、公网隧道网址为什么「尚未取得」

- `tunnel.ps1` 旧顺序：先 `start.ps1 -Tunnel` 启服务 → 再建 cloudflared 隧道 → 最后写 `logs/cloudflared.url`。
  但 `run.py` 启动时只读一次 url 文件（等 6 秒），那时文件还没有新网址，启动面板只能印「尚未取得」。
- 新顺序：**删旧 url 文件 + 停旧 cloudflared → 建隧道 `Wait-TunnelUrl` → 落盘 → 最后启服务**。
  删旧网址同样重要：`netinfo.read_tunnel_url()` 虽会检查 cloudflared 进程，但隧道崩了以后残留文件仍会误导。
- 测试 `test_tunnel_script_writes_url_before_starting_service` 用文本顺序锁住这条，别再调换回去。

### 三、按钮与启动器（**改远程访问前必读**）

- uvicorn 的 host 在启动时定，服务**不能运行时改监听地址**，所以「切换模式」必然要重启服务。
  按钮不是摆设：点了会重启，页面短暂断开，必须先 `window.confirm`。
- 链路：`POST /api/access/launch`（只认 `access_mod.is_local()`，远程 403）→ `app/remote_launch.start_launch()`
  用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` spawn `launch_mode.ps1` → 立刻返回；
  启动器先 `Start-Sleep 3` 等 HTTP 响应发完 → `stop.ps1` → （tunnel 时建隧道）→ `start.ps1 -Remote/-Tunnel`
  → 把结果写 `logs/remote-launch.json`；前端每 2 秒轮询 `/api/access/status` 的 `launch` 字段，
  `ready` 自动刷新、`error` 给日志路径、120 秒超时给双击脚本兜底。
- `launch_mode.ps1` 必须 **UTF-8 BOM**（中文 + PS 5.1）；写状态文件必须**无 BOM**
  （用 `[System.IO.File]::WriteAllText` + `UTF8Encoding($false)`），否则 Python `json.loads` 会被 `\ufeff` 卡住。
- `start.ps1` / `stop.ps1` 内部有 `exit`，**必须用子进程 `Start-Process -Wait` 调用**，直接 `&` 会把启动器自己结束掉。
- 按钮只在主机本机出现（整块 `#hero-panel` 已由 `applyLocalOnlyVisibility()` 控制），后端也有 403 兜底。
- 工具会话里起的服务不持久（多次验证），让按钮 spawn 的启动器去跑才是正路；别再试图由 agent 后台拉长期服务。

### 四、本次交付

- 新增 `app/remote_launch.py`、`launch_mode.ps1`、`tests/test_remote_launch.py`（13 条）；
  `app/crawl.py` 三道拦截 + `tests/test_stake_transfer.py` 3 条；`tunnel.ps1` 顺序；
  `app/main.py` 接口与 status 字段；`app/static/app.js` 按钮/轮询；`app/templates/index.html` 按钮；
  `app/static/styles.css` 样式。
- 版本 0.8.45，两处 `?v=` 同步；CHANGELOG / README 第 77 条 / 使用说明 md+html /
  VERIFICATION 0.8.45 节 / 本条均已更新。

## 63. 2026-11-11 0.8.46 交接：远程访问按钮「一直转圈」的根因与修复（**改启动器前必读**）

- 用户点按钮后服务其实 09:14:30 就恢复了，但状态文件永远停在 `launching`、
  消息还是初始句，启动器进程消失。前端把「状态文件写回 ready」当唯一成功判据
  → 无限轮询 + 按钮永久禁用 → 用户看到「页面直接挂了，没反应很久」。
- **三条铁律**：
  1. 判断「服务有没有重启」用服务自己上报的 `server_started_at`（`GET /api/access/status`），
     **不要用启动器写回的状态文件当唯一依据**。启动器可能被系统终止、被 Job 连带杀掉、
     或卡在任何一步，它写的 ready 不可靠。
  2. `remote_launch.read_state()` 对超过 90 秒的 `launching` 自动判 error，
     前端 120 秒超时兜底。任何「等待外部进程回报」的状态机都要有超时自愈。
  3. **PS 5.1 的 `Start-Process -Wait` 会等待该进程及其所有后代进程**。
     `start.ps1` 拉起的是常驻服务，用 `-Wait` 启动器就永远不返回。
     必须 `-PassThru` + `WaitForExit(ms)`（只等脚本本身）并带超时。
- `remote_launch.start_launch()` 加 `CREATE_BREAKAWAY_FROM_JOB`，失败回退 `creationflags=0`；
  服务 spawn 的启动器若留在同一 Job，停旧服务时可能被连带终止。
- `launch_mode.ps1` 新增 `logs/remote-launch.log`，从第一行起逐步记录
  （启动、每个脚本步骤与 exit code、状态变更、失败堆栈）。以后再出问题先看它。
- 现场处置：把卡住的 `launching` 手工写成 `error` 即可立刻解除页面转圈，
  不必重启服务。状态文件是 `logs/remote-launch.json`（无 BOM UTF-8）。
