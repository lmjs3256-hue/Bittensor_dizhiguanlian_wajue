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

此前租约锁修复复验记录为完整 pytest **9 passed**；当前最新完整回归为 **27 passed**，具体结果见 `VERIFICATION.md`。

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
- 最新验证：`27 passed`；`compileall` 通过；`node --check app/static/app.js` 通过；8766 服务已重启。
- Playwright 实测：结果区不含交易哈希、区块号、from/to 等交易明细；本地样本查询显示关联地址、层级和金额；深灰主题可以切换；无 key 调用交易所同步接口返回 401 并提示先设置 API key。

Taostats. “Get Active Validators.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/validators/get-validators-active. Accessed 31 Oct. 2026.

Taostats. “Get Exchange Addresses.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/exchange-addresses/get-exchange-addresses. Accessed 31 Oct. 2026.

## 13. 2026-11-01 初始版本发布交接

### 发布信息

- 应用版本：`0.1.0`。
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

打开 `http://127.0.0.1:8766` 后，先在页面输入 Taostats API key，再选择是否使用 Taostats 历史索引。API key 服务重启后需要重新输入。

### 已知风险和后续工作

1. 公共 RPC 历史状态可能被裁剪，本地索引不能代表全链历史。
2. Taostats 免费档历史跨度、接口 credit 消耗和真实账号额度需要使用用户自己的 key 验证。
3. 自动交易所识别目前使用明确地址库标记和当前查询范围内的转账行为特征；完整余额和子网购买记录还需要接入更多数据源。
4. 如果需要多人协作，下一步可以在 GitHub 上补充 Issue 模板、贡献指南和 CI 测试流程。
