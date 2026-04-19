# OpenSpec v1 - Opportunity Discovery System

## 北极星目标

这个系统的目标，不是单纯抓帖子、抓行情、抓链上数据，而是构建一个**面向交易机会发现的多模块监控系统**。

它要回答的核心问题是：
- 现在市场最热的币和叙事是什么
- 哪些热点背后真的有资金在跟
- 哪些链上标的出现了强买盘特征
- 哪些叙事正在从讨论走向资金确认
- 如何把这些线索整理成可解释、可筛选、可复核的候选池

V1 阶段的优先级是：
1. 先构建稳定可跑的发现层
2. 先把热度、叙事、资金三个视角分开做清楚
3. 再做模块融合，而不是一开始就做自动交易

一句话定义：
**Opportunity Discovery System V1 是一个“多源热度 + 叙事理解 + 资金确认”的机会发现系统，不是自动交易系统。**

---

## 1. 系统边界

### 1.1 系统包含的模块

V1 系统由以下模块构成：

1. **Trend Monitor**
   - 发现当前热度上升的币和方向
   - 结合内容、交易所行情、资金异动判断是否值得关注

2. **Narrative Monitor**
   - 识别市场当前主叙事
   - 判断叙事阶段、情绪方向和确认状态

3. **On-chain Strong Buy Monitor**
   - 识别链上强买盘
   - 过滤流动性不足、假池、过热等噪音

4. **Fusion Layer（后续阶段）**
   - 将热度、叙事、链上资金统一映射到候选池
   - V1 允许预留结构，但不要求完全实现

### 1.2 系统不包含的能力

V1 明确不做：
- 自动下单
- 自动连接交易所账户执行交易
- 自动资金管理
- 超高频套利
- 链上合约安全审计
- 钱包归因和地址级聚类情报系统
- 完整量化回测平台

---

## 2. 输入来源

### 2.1 内容类输入
- Binance Square
- 后续可扩展 Twitter/X、Telegram、Discord，但不属于 V1 必做

### 2.2 市场类输入
- Binance Spot API
- Binance Futures API
- DexScreener API
- 后续可扩展 CoinGecko、GeckoTerminal、Birdeye、DEXTools

### 2.3 链上资金类输入
V1 默认优先采用聚合数据源，而不是原始链上日志：
- DexScreener pair 数据
- 交易笔数、成交额、流动性、价格变化

---

## 3. 输出结构

系统最终需要形成统一的结构化输出层。

V1 至少要支持以下输出类别：
- `trend_candidates`
- `buy_candidates`
- `strong_capital_buy_candidates`
- `top_narratives`
- `symbol_narrative_view`
- `onchain_strong_buy_candidates`

每个候选结果都必须尽量可解释，至少回答：
- 为什么上榜
- 热度来自哪里
- 是叙事驱动还是资金驱动
- 是否存在明显风险或噪音

---

## 4. 模块职责划分

### 4.1 Trend Monitor
负责：
- 发现“谁在热”
- 判断热度是否带有方向
- 通过市场和 OI 等信号做初步确认

### 4.2 Narrative Monitor
负责：
- 发现“市场在讲什么故事”
- 判断叙事处于早期发酵、中期扩散、末端过热还是观察期
- 给出偏多/偏空/中性的主观判断

### 4.3 On-chain Strong Buy Monitor
负责：
- 发现“链上是否有真钱在推动”
- 识别强买盘、买卖失衡、量能爆发、流动性改善
- 排除明显不可交易或过热的池子

### 4.4 Fusion Layer
负责：
- 把多个模块的候选统一成一个优先级候选池
- 但 V1 只要求设计接口，不要求自动融合为唯一总榜

---

## 5. 可解释性要求

所有模块输出必须满足以下原则：

1. **保留原因字段**
   - 如 `reason_summary`、`price_filter_reasons`、`funds_confirmation_mix`

2. **保留关键指标**
   - 热度、交易量、流动性、OI、阶段、情绪等

3. **明确模块性质**
   - 结果必须明确属于：
     - 热点发现
     - 叙事判断
     - 链上买盘
     - 强资金确认

4. **避免黑盒输出**
   - 不允许只返回单一神秘分数，而无解释字段

---

## 6. 开发顺序约束

V1 推荐开发顺序：

### Phase 1
- 完成 Trend Monitor V1
- 完成 Narrative Monitor V1

### Phase 2
- 完成 On-chain Strong Buy Monitor V1

### Phase 3
- 对接统一候选池结构
- 输出多榜单视图

### Phase 4（后续）
- 加入执行层适配，如 OKX Agent Trade Kit
- 但执行层不属于本 spec 的 V1 范围

---

## 7. 风险原则

系统必须区分以下几类情况：
- 热度很高，但可能是崩盘热度
- 叙事很热，但没有资金跟随
- 链上买盘很强，但流动性不足
- 热度、叙事、资金三者共振

V1 允许输出“观察”、“回避”、“末端过热”等标签，避免所有结果都被包装成可买机会。

---

## 8. 验收标准

V1 通过，至少满足：

1. 系统存在总 OpenSpec，并明确约束模块边界
2. Trend Monitor 有独立 spec
3. Narrative Monitor 有独立 spec 或在总 spec 中明确说明其边界
4. On-chain Strong Buy Monitor 有独立 spec
5. 各模块能输出结构化结果
6. 输出中保留关键解释字段
7. 不把发现系统误定义为自动交易系统

---

## 9. 一句话定义

Opportunity Discovery System V1 是一个“热度、叙事、资金三视角联动”的交易机会发现框架，目标是筛出值得盯盘和进一步研判的候选，而不是直接替人下单。
