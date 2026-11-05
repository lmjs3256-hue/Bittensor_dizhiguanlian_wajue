# 地址关联工具设计、可行性与验收记录

**文档版本：** 0.6.19
**验证日期：** 2026-11-05（Asia/Shanghai）
**数据网络：** Bittensor mainnet / Finney
**验证时最新高度（抽样）：** 约 9,401,465
**实现状态：** 核心链路、三级查询、Taostats 客户端、离线授权、地址库（分类/标签组/自建库/预置库）均已实现并有自动化测试；Taostats 真实账户可用性与历史覆盖仍待 API key 实测

## 一、结论

### 事实

1. 输入地址、最多三级关联、金额过滤和已知地址库命中反馈，皆已通过确定性程序测试。
2. 公开 RPC 返回的事件中，`Balances.Transfer` 带有 `from`、`to`、`amount`，可构建普通 TAO 转账资金图。
3. 最新区块链段可读取；实测单块读取并写入 SQLite 后，8 条 Transfer 成功入库，后续单块测试 1 条 Transfer 成功取得交易哈希。
4. 地址库预先登记公开链路的目标地址后，查询正确返回关联地址、第 1 级路径、`5.0440955000 TAO` 累计金额和库内标签。
5. 当前公开入口节点对旧区块返回 `State already discarded for ...`；历史状态不是完整归档。因此“任意历史时间、任意地址全链三级追溯”不能由该公开 RPC 单独保证。

### 推测（明确标注）

1. 接入保留完整区块与运行时历史的归档节点或第三方全链索引后，历史追溯有较大机会满足需求；具体覆盖范围和成本须在采购或接入前实测。
2. 随着索引大小增长，本地 SQLite 需逐步做按地址、区块和交易哈希的分区/索引优化；当前 MVP 足以做小规模连续增量验证，未证明能支撑全链级数据量。

## 二、系统边界

### 纳入自动关系

| 类型 | 链上事件 | 是否成为资金边 | 口径 |
| --- | --- | --- | --- |
| 普通转账 | `Balances.Transfer` | 是 | `from → to`，金额按 1 TAO = 10^9 RAO 转换 |
| 手续费扣除 | `Balances.Withdraw` | 否 | 同一交易可能伴随 Transfer，纳入会重复计算 |
| 余额增加 | `Balances.Deposit` | 否 | 不是独立转账来源 |
| 子网 stake 转移（钱包之间） | `SubtensorModule.StakeTransferred` | 是（0.1.6 起） | `origin_coldkey → destination_coldkey`，金额取事件的 TAO 等值，单独标注边类型与源子网 ID |
| 质押、解质押、购买、跨子网移动 | `SubtensorModule` 等 | 暂不自动纳入 | 需另建资产语义 |
| 代理身份关系 | `Proxy.ProxyExecuted` 等 | MVP 暂不自动纳入 | 在该事件中交给人别关系需来源链与规则核验 |

### 重要语义

一次转账只证明两地址出现资金流动，不证明两地址属于同一人、同一机构、共同控制或同一受益人。工具标签分为“链上行为关系”和“用户导入的地址库标签”，不把前者升级为真实身份结论。

## 三、处理流程

```text
用户输入起始地址
        ↓
打开本地 SQLite 中的转账索引
        ↓
按 from / to 邻接索引执行 1—3 级遍历
        ↓
第一跳作为候选探索线索
第二、三跳按单边金额过滤
        ↓
关系累计金额过滤
        ↓
命中已知地址库
        ↓
返回关联地址 + 路径边 + 库内标签 + 索引截止块
```

### 金额过滤策略

默认单边门槛和累计门槛均为 `0.1 TAO`，均可由用户修改。

- **第一跳**：支持低于单边门槛的金额，以避免小额试探资金藏匿后续关系。
- **第二、三跳**：低于单边门槛的转账不成为可展示边，也不承载后续层级路径。
- **最终关系**：关系累计金额低于累计门槛时不返回。
- **去重**：同一交易哈希、两端地址、金额和区块完全相同的数据只保留一次。同一交易内多个真实 Transfer 事件分别保留。
- **系统账户**：默认拒绝前缀为 `5EYCAe5` 的公共系统账户。它是可配置规则，不是对所有交易所、池子或合约地址的身份标注。

### 为什么不是“同一地址”

两条边指向同一对手方、金额相同或时间接近，都不能单独证明操作身份证同。MVP 没有自动把共享对手方聚合成实体，以免把地址库命中误读为同一人命中。

## 四、数据模型

| 数据表 | 核心字段 | 作用 |
| --- | --- | --- |
| `address_library` | address、label、category、categories、note、source、book、exchange_confirmed、exchange_rejected | 人工地址库；`categories` 支持一个地址多分类，`book` 是所属自建库 |
| `address_books` | name、note、created_at | 自建地址库台账（库名列表） |
| `address_categories` | name | 人工分类（大V/群友/其他/自定义） |
| `transfers` | block_number、block_hash、tx_hash、event_index、from、to、amount_rao、amount_tao、edge_type、netuid、hotkey | 本地资金边索引 |
| `scan_jobs` | start/end、head、status、error、scanned_blocks | 扫描任务审计 |
| `scan_lock` | 租约与持有者信息 | 保证同一时刻只有一个扫描在写库 |
| `chain_cursors` | rpc_url、last_block、last_hash | 连续增量水位 |
| `relation_archives` | seed、status、rounds、requests、related_count、payload、created_at | 每次关联查询的结果快照，供查询记录回看与重新导出；最多保留 500 条，总体积超过 200 MB 时淘汰最旧记录 |
| `import_batches` | kind、summary、payload（单槽 id=1） | 「撤回上次导入」的快照：新增地址、被改写字段原值、本次新建/删除的库 |
| `seed_books` | name、addresses、created_at | 外发预置地址库的安装记录（幂等，解散后不自动复活） |

索引的唯一约束来自区块与事件序号，并另外对事件字段建立唯一键；导入重复不会重复累加金额。

关联查询的归档不参与关联计算，只是结果的只读快照：`payload` 保存去掉用量统计后的完整 JSON（超过 8 MB 时丢弃逐笔 `edges` 明细），历史页据此还原表格和导出地址。同步地址库时按地址去重，已存在的行不覆盖，保证人工维护的分类与备注不会被自动流程改写。

撤回机制：任何批量写入（同步地址库 / 新建地址库 / 导入文件 / 解散地址库 / 重新导入预置库）都会写一条 `import_batches` 快照，只保留最新一条。`undo_last_import()` 删除该批新插入的地址、按快照还原被改写的字段、并移除本次新建的库；解散则按 `deleted` 快照重建地址与库名。

标签与分类语义：`label` 为空时对外展示地址后四位（`label_custom=false`），人工命名过的为自定标签（`label_custom=true`）。**改名和改分类都只能整组进行**（按 label 或 book 批量改），单条地址只改备注，避免把同一组拆散；整组改分类时交易所/验证者地址自动跳过，并写撤回快照。

## 五、已验证证据

### 测试 1：隔离程序测试

`pytest` 5 项全部通过，覆盖：

1. 只抽取 `Balances.Transfer`，排除 `Withdraw` 和 `Deposit`。
2. `ApplyExtrinsic` 事件成功关联到真实交易哈希。
3. 三级该截止、第四级不返回。
4. 低额第一跳保留；低额间接边不会被用来串联后续大额伪关系。
5. 地址库命中返回关系地址、层级和标签。
6. CSV / TXT 导入格式和错误校验。
7. JSON 金额字段可序列化。

### 测试 2：公开 RPC 增量入库

一次真实扫描：

- 起始/结束区块：`9,401,464`
- 扫描块数：1
- 采集 `Balances.Transfer`：8 条
- 索引写入：8 条
- 区块哈希：`0xdb905b122d86d74398c5081bca1359db7bf29ef3730a85b34e320ae15fcf761c`
- 实测耗时：约 19.5 秒（含连接初始化）

修复交易哈希映射后，另一公共区块扫描：

- 起始/结束区块：`9,401,478`
- 采集 Transfer：1 条
- 交易哈希：`0x2011bde147...`（已验证存在）
- 交易哈希关联率：1/1

### 测试 3：命中反馈端到端

以真实 RPC 采集的公开转账两端地址做虚拟标签：

- 关系地址：1
- 地址库命中：1
- 保留边：1
- 第 1 级
- 累计 `5.0440955000 TAO`
- 地址库类别 `WATCH`

这里只验证“命中能随关系返回”的机制，不表示该公开地址真实属于任何组织；标签为测试用途。

### 测试 4：API 端到端

隔离数据库下验证：

- 首页返回 200。
- 健康检查返回服务名称。
- `A → B → C → D` 查询返回 `B、C、D`，第四级不出现。
- 地址 B 地址库命中，标签随结果返回。
- CSV 上传成功导入。
- `depth=4` 返回 HTTP 422。

## 六、性能与容量

### 实测

- 公开 RPC 抽样 120 个区块：21,528 个事件、324 个 `Balances.Transfer`、耗时约 213 秒。
- 这等于事件逐个拉取和本地解码的近似实测，不适合把“每次地址查询”变成全链实时扫描。
- MVP 采用“先增量入库，再本地按地址邻接索引查询”的架构，符合该数据特征。

### 尚未验证

- 全链 940 万区块的完整回填耗时。
- 全链转账规模、索引体积、SQLite 在数十亿行时的表现。
- 多个并发查询与扫描同时运行时的吞吐。
- 第三方归档节点或索引服务的 SLA、费用、历史完整性和授权边界。

这些必须在大规模索引前通过独立容量实测确认，不能承诺为已完成的性能。

## 七、当前可行范围与缺口

| 需求 | 状态 | 证据/边界 |
| --- | --- | --- |
| 输入地址 | 已验证 | 页面和 API 输入校验 |
| 最多三级关联 | 已验证 | 确定性测试 + API 测试 |
| 转账记录挖掘 | 最新增量范围已验证 | SQLite + RPC 实测 |
| 低额关联过滤 | 已验证 | 单边与累计门槛测试 |
| 人工地址库导入 | 已验证 | TXT/CSV/JSON |
| 命中反馈关联地址和库标签 | 已验证 | 关系结果附带命中项 |
| Taostats 历史转账 API 接入 | 已实现，mock 与隔离 API 测试通过 | 已核对 `/v1/transfers` 字段、RAO 数量格式及分页契约；尚未用本项目 API key 实测真实权限和返回数据 |
| 任意历史时间全链追溯 | **未满足** | Taostats 接口已接入，但本项目尚未用已认证 key 验证历史上限和覆盖完整性；公开 RPC 旧状态也会被裁剪 |
| 后台持续索引 | **未完成** | MVP 有“继续水位”手动增量，尚无 24×7 调度器 |
| 关联结果分页与整组管理 | 已验证 | 每页 10/20/50/100（默认 10）、页码跳转与记忆；标签组整组改名、整组全选、按库筛选 |
| 预置地址库随包分发 | 已验证 | `app/seed_books/*.txt` 首次启动导入（6 组 / 355 地址），支持「重新导入预置库」补齐缺失地址 |

## 八、后续开发顺序

### P0：历史数据源实测验收（代码接入已完成）

Taostats 客户端现已接入 `GET /v1/transfers`。创建并在本机配置免费档 API key 后，仍需使用已知公开转账进行真实查询验收：

1. 验证当前免费档 key 可访问端点、每次 API credit 成本和账户历史范围。
2. 用已知交易哈希或区块对照地址、金额、区块和交易哈希。
3. 验证该地址及其邻居的分页返回、接口限流和服务端历史窗口行为。
4. 记录缺失区间与 provider 计划限制；分页结束不代表全链历史完整。
5. 如果免费档范围不满足需求，再评估归档节点或其他索引服务的价格、服务水平和数据许可。

### P1：自动增量索引

- 为后台任务提供启动/暂停/水位和失败重试。
- 按最终确定区块推进，处理节点重组与重复写入。
- 记录来源、连续区间、缺口和错误，不把部分覆盖写成全链完成。
- 将数据库从单机 SQLite 平滑迁移到可支撑全链规模的结构。

### P2：增强关联语义

- 预留质押、swap、代理关系的独立边类型。
- 加入时间窗、交易次数、对手方多样性门槛。
- 增加路径解释、导出、地址库命中审计。
- 按规则输出关联置信度与替代解释。

## 九、验收标准

下一阶段“历史完整”必须同时满足以下条件，才可按全链历史对外宣称：

1. 声明的区块范围和来源有可复现记录。
2. 缺口为 0，或缺口明确展示且不把缺口内“无命中”作为否定证据。
3. 随机抽样区块可重取且事件、交易哈希、金额一致。
4. 地址库命中同时返回完整路径和库标签来源。
5. 历史回填、查询延迟和索引体积在真实容量测试中达标。
6. 隐私和外传边界明确：本地库内容不经用户确认不外发。

## 十、来源

Bittensor Contributors. “Bittensor: Decentralized Machine Learning Protocol.” *GitHub*, github.com/opentensor/bittensor, accessed 31 Oct. 2026.
OpenTensor Foundation. “Finney Entrypoint RPC.” *Bittensor mainnet*, entrypoint-finney.opentensor.ai:443, accessed 31 Oct. 2026.
OpenTensor. “Async Substrate Interface.” *PyPI*, version 2.2.1, pypi.org/project/async-substrate-interface/, accessed 31 Oct. 2026.
Parity Technologies. “Substrate JSON-RPC Specification.” *Substrate Documentation*, docs.substrate.io, accessed 31 Oct. 2026.

Taostats. “Get Transfers.” *Taostats API Documentation*, https://taostats.io/zh/docs/new/transfers/get-transfers. Accessed 31 Oct. 2026.

Taostats. “Taostats Pro.” *Taostats*, https://taostats.io/zh/pro. Accessed 31 Oct. 2026.
