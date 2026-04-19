# OpenSpec v1 - Trend Monitor

## 北极星目标

Trend Monitor 的目标，是发现当前市场中**热度正在上升、且具备初步可交易性的币种候选**。

它不是为了回答“这个币值不值得长期持有”，而是为了回答：
- 哪些币正在被更多人讨论
- 哪些币热度带有方向性
- 哪些币有基础市场条件支持继续观察
- 哪些币只是纯噪音或崩盘热度

一句话定义：
**Trend Monitor V1 是一个偏向交易线索发现的热度监控模块。**

---

## 1. 模块目标

Trend Monitor 负责：
- 从内容和市场数据中提取当前热点币候选
- 区分偏多热度、偏空热度和中性噪音
- 输出买入候选、强资金确认候选、观察候选

Trend Monitor 不负责：
- 自动下单
- 完整叙事判断
- 地址级链上分析
- 深度基本面研究

---

## 2. 输入来源

### 2.1 内容来源
- Binance Square 帖子

### 2.2 市场来源
- Binance Spot 24h ticker
- Binance Futures 24h ticker
- Binance Futures OI 历史
- CoinGecko Trending（可选）
- Binance Futures movers（可选）

---

## 3. 核心流程

Trend Monitor 的固定处理流程为：

1. 抓取帖子
2. 提取 symbol candidates
3. 对帖子进行信号评分
4. 聚合同币种帖子为币种级热度
5. 查询 Binance Spot / Futures 市场快照
6. 计算 OI 变化、价格变化、成交额等时机指标
7. 计算热度分、时机分、综合分
8. 输出：
   - 热点候选
   - 买入候选
   - 强资金确认买入候选

不得跳过市场确认步骤直接把热帖当成买入机会。

---

## 4. 允许使用的评分因子

### 4.1 热度维度
允许使用：
- 帖子数
- 去重作者数
- 互动量
- 平均 signal_score
- CoinGecko trending flag
- Binance futures movers flag

### 4.2 时机维度
允许使用：
- OI 变化幅度
- OI value 变化幅度
- 价格 24h 变化
- 成交额
- VWAP 动量
- divergence ratio

### 4.3 方向维度
允许使用：
- 帖子多空 bias
- 时机方向 bias
- 是否过热
- 是否更像崩盘噪音

---

## 5. 输出字段

Trend Monitor 输出至少要包含：
- `symbol`
- `heat_score`
- `timing_score`
- `final_score`
- `stage`
- `is_buy_candidate`
- `strong_capital_confirmed`
- `reason_summary`
- `square_heat`
- `timing_signal`
- `spot_market_snapshot`

---

## 6. 候选分类要求

Trend Monitor 至少区分以下几类：

1. **观察**
   - 有热度，但不足以当成候选

2. **热度偏多**
   - 内容热度偏多，值得继续看

3. **资金异动观察**
   - 市场数据出现轻微资金异动，但还不够强

4. **可关注买入**
   - 热度、方向、时机都比较健康

5. **强资金确认买入**
   - 热度偏多
   - OI 增长
   - OI value 增长
   - 成交额达到最低质量门槛
   - 综合分通过阈值

6. **回避 / 过热观察**
   - 明显崩盘热度
   - 明显过热追高
   - 偏空噪音

---

## 7. 风险约束

Trend Monitor 必须显式防止：
- 崩盘型热度霸榜
- 单帖极端情绪误导总榜
- 低流动性标的被误判为买入机会
- OI 增长但价格表现极差时被误判为偏多

---

## 8. 验收标准

V1 验收通过，至少满足：

1. 能稳定抓取并聚合 Binance Square 帖子
2. 能输出热度分和时机分
3. 能区分热度候选和强资金确认候选
4. 能解释为什么一个币进榜
5. 能解释为什么一个币被降权或回避
6. 结果里不存在明显崩盘热度长期霸榜的问题

---

## 9. 一句话定义

Trend Monitor V1 是一个将“帖子热度 + 市场时机 + 资金初步确认”结合起来的热点机会扫描模块，用于筛选值得盯盘的候选币。
