# 钱包总价值口径

## 计算方式

工具把钱包总价值定义为：

```text
钱包总价值 TAO = 可用 TAO + 各子网质押 Alpha × 对应子网当前 Alpha/TAO 价格之和
```

根子网的质押使用 1 TAO/TAO 计价。普通子网的 Alpha 不直接相加，因为不同子网的 Alpha 不是同一个可比单位。

## 链上读取

- 可用部分读取 `System.Account.data.free`。
- 质押位置读取 `StakeInfoRuntimeApi.get_stake_info_for_coldkey`。
- 普通子网价格读取 `SwapRuntimeApi.current_alpha_price`。
- 页面同时返回 `available_tao`、`staked_tao`、`total_value_tao` 和 `balance_status`。

`complete` 表示可用余额和质押估值都成功读取；`partial` 表示只拿到可用余额；`unavailable` 表示当前地址没有可用的完整读取结果。质押读取失败不会被当作 0 TAO。

## 事实边界

这是当前区块的链上估值快照。它不包含法币价格，也不代表历史平均持仓；Alpha 价格变化会导致总价值变化。公共 RPC 未暴露质押运行时接口、连接超时或返回无法解析时，页面会明确标记部分获取或未获取。

## 外部来源（MLA）

OpenTensor. “subtensor: StakeInfoRuntimeApi and SwapRuntimeApi.” *Bittensor Subtensor*, GitHub, https://github.com/opentensor/subtensor. Accessed 1 Nov. 2026.

OpenTensor. “Subtensor.get_stake_info_for_coldkey and get_subnet_price.” *Bittensor Python SDK*, GitHub, https://github.com/opentensor/bittensor/blob/master/bittensor/core/subtensor.py. Accessed 1 Nov. 2026.
