# OpenSpec v1 - On-chain Strong Buy Monitor

## 北极星目标

On-chain Strong Buy Monitor 的目标，是在链上市场中尽早识别**强买盘、资金集中、且具备最低可交易性**的标的。

它要回答的问题是：
- 哪些链上交易对正在出现明显买盘倾斜
- 哪些池子正在放量
- 哪些币的流动性和成交活跃度同时改善
- 哪些热点只是小池子噪音或末端情绪

一句话定义：
**On-chain Strong Buy Monitor V1 是一个面向链上 DEX 市场的强买盘发现模块。**

---

## 1. 模块目标

该模块负责：
- 发现链上强买盘
- 判断是否属于量能放大和买卖失衡共振
- 对假池、低流动性、小样本噪音做过滤
- 输出链上强买盘榜

该模块不负责：
- 智能合约安全审计
- Honeypot 检测
- 地址级别的鲸鱼归因
- 自动交易

---

## 2. 输入来源

### 2.1 V1 默认来源
- DexScreener API

### 2.2 V1 可读取的字段
至少支持以下字段：
- `chainId`
- `dexId`
- `pairAddress`
- `baseToken.symbol`
- `quoteToken.symbol`
- `priceUsd`
- `priceChange.m5 / h1 / h6 / h24`
- `volume.m5 / h1 / h6 / h24`
- `liquidity.usd`
- `txns.m5 / h1 / h6 / h24`
- `pairCreatedAt`（若可用）

---

## 3. 核心流程

V1 固定流程：

1. 获取候选交易对
2. 过滤无效价格和无效池
3. 计算买卖失衡
4. 计算短周期放量程度
5. 检查流动性是否足够
6. 检查短周期价格变化是否已过热
7. 计算链上强买盘分数
8. 输出链上强买盘候选列表

---

## 4. 允许使用的因子

### 4.1 买卖失衡因子
允许使用：
- buys > sells 的差值
- buys / sells 比值
- m5 / h1 / h6 窗口内的买卖失衡

### 4.2 放量因子
允许使用：
- m5 相对 h1 的成交额放大
- h1 相对 h6 / h24 的成交额放大
- 交易笔数提升

### 4.3 市场质量因子
允许使用：
- liquidity.usd
- 24h volume
- m5 / h1 txns 数
- 是否为主池

### 4.4 过热与风险因子
允许使用：
- m5 / h1 / h24 价格涨幅
- 是否已经极端拉升
- 是否流动性过浅
- 是否交易笔数太少

---

## 5. 禁止引入的逻辑

V1 禁止直接引入：
- 主观叙事评分
- 合约安全评分
- 钱包聪明钱画像
- 地址聚类归因
- 自动交易决策

这些能力如果要加入，必须通过后续 spec 升级。

---

## 6. 输出字段

每个链上强买盘候选至少包含：
- `symbol`
- `chain_id`
- `dex_id`
- `pair_address`
- `price_usd`
- `price_change_pct_m5`
- `price_change_pct_h1`
- `volume_m5`
- `volume_h1`
- `liquidity_usd`
- `txns_buys_m5`
- `txns_sells_m5`
- `buy_sell_imbalance`
- `volume_spike_score`
- `onchain_buy_score`
- `risk_flags`
- `reason_summary`

---

## 7. 候选分类

至少分为：

1. **观察池**
   - 有轻微买盘，但不足以列为强买盘

2. **强买盘候选**
   - 买卖失衡明显
   - 成交额和 txns 有改善
   - 流动性足够
   - 未明显过热

3. **末端过热**
   - 已经拉升过大
   - 可能不再适合追

4. **低质量噪音**
   - 池子太浅
   - 交易笔数太少
   - 价格或成交数据异常

---

## 8. 可解释性要求

所有结果必须回答：
- 为什么它是强买盘
- 是因为买单多、放量、还是流动性改善
- 风险点是什么
- 是可继续关注，还是已经过热

因此必须保留：
- `risk_flags`
- `reason_summary`
- 原始关键市场指标

---

## 9. 验收标准

V1 验收通过，至少满足：

1. 能从 DexScreener 拉到结构化 pair 数据
2. 能计算买卖失衡
3. 能计算放量和流动性质量
4. 能输出链上强买盘榜
5. 能输出风险标记
6. 不把明显低流动性噪音池排到前列

---

## 10. 一句话定义

On-chain Strong Buy Monitor V1 是一个从 DEX 聚合市场数据中识别“强买盘 + 放量 + 最低质量达标”交易对的链上资金监控模块。
