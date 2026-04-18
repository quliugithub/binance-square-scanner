# OpenSpec v2 - Heat + OI Opportunity Scanner

## 北极星目标

这个系统的目标，不是单独做广场抓取，也不是单独做合约指标监控，而是通过**舆情热度发现 + OI 异动时机筛选**，尽早发现高关注、资金正在进入、但价格尚未完全反映的交易机会。

系统的核心问题是：
- 哪些币正在被市场快速关注
- 哪些币的资金在明显流入
- 哪些币的价格尚未完全反映这种流入
- 如何把“热度”与“时机”结合，形成更可交易的备选池

当前阶段的优先级是：
1. 先定义完整系统边界
2. 先打通候选发现与时机过滤两层
3. 再逐步优化权重、信号质量和展示形式

---

## 1. 系统定义

V2 系统由三个子模块组成：

1. **Heat Scanner**
   - 用于发现“哪些币现在最值得关注”

2. **OI Divergence Scanner**
   - 用于发现“哪些币当前更可能存在时机”

3. **Fusion Ranker**
   - 将热度信号与 OI/价格背离信号融合，得到最终优先级

---

## 2. Heat Scanner

### 2.1 目标
Heat Scanner 用于构建一个“候选币热度池”，重点关注市场注意力正在集中的标的。

### 2.2 输入来源
Heat Scanner 的输入来源限定为：

1. **Binance Square**
   - 使用 Playwright 抓取帖子内容
   - 用正则提取 `$TICKER` 形式的 mention
   - 统计 mention 次数

2. **CoinGecko Trending**
   - 提供站外趋势热度参考

3. **Binance 合约 24h 涨跌榜**
   - 提供交易所层面的短期强势/弱势参考

### 2.3 Binance Square 规则
对 Binance Square 的处理规则：
- 必须抓取帖子正文
- 只统计 `$TICKER` 形式 mention
- 允许后续补充 `symbolCandidates`，但 V2 先以 `$TICKER` 为主
- 统计热度时允许按：
  - mention 次数
  - 去重作者数
  - 帖子互动量
  做加权

### 2.4 稳定币过滤
必须排除以下稳定币或稳定币噪音：
- USD
- USDT
- USDC
- BUSD
- FDUSD
- TUSD
- DAI
- USDE
- PYUSD

如果 `$USDT`、`$USDC` 等被提及，不计入有效候选。

### 2.5 Heat Scanner 输出
至少输出以下字段：
- symbol
- square_mentions
- unique_authors
- square_engagement_score
- coingecko_trending_flag / score
- futures_movers_flag / score
- heat_score
- heat_rank

---

## 3. OI Divergence Scanner

### 3.1 目标
OI Divergence Scanner 用于识别“资金明显进入，但价格尚未完全响应”的标的，作为 timing 候选。

### 3.2 输入来源
输入限定为：
- Binance U 本位合约市场
- 全量可监控 symbol

### 3.3 基础指标
V2 必须支持以下基础指标：
- OI 当前值
- OI 变化幅度（相对历史窗口）
- 价格变化幅度（相对同一窗口）
- OI/Price divergence ratio

### 3.4 核心逻辑
示例逻辑：
- OI +20%
- Price +3%
- Divergence Ratio = 20 / 3 ≈ 6.7

V2 允许的核心判断方向：
- **OI 大涨，价格小涨/横盘** → 资金入场但价格未充分反映
- **OI 大跌，价格未明显下跌** → 可作为另一类异动参考（是否纳入排序由后续决定）

V2 的首版重点先做：
- **OI 增长型背离**

### 3.5 OI Scanner 输出
至少输出以下字段：
- symbol
- oi_change_pct
- price_change_pct
- divergence_ratio
- timing_score
- timing_rank

---

## 4. Fusion Ranker

### 4.1 目标
Fusion Ranker 用于把“高热度”与“高背离”结合，生成最终候选池。

### 4.2 融合原则
最终候选不应只依赖单一信号。

至少考虑：
- heat_score
- timing_score
- 市场可交易性
- 市场类型（CEX / DEX / Futures）

### 4.3 V2 最低融合方式
V2 允许先用简单加权：
- final_score = a * heat_score + b * timing_score

后续允许升级，但 V2 不强求复杂模型。

### 4.4 输出字段
至少输出：
- symbol
- heat_score
- timing_score
- final_score
- market_type
- venue
- priority_rank
- reason_summary

---

## 5. 市场范围

V2 的市场范围分为两类：

1. **内容热度覆盖范围**
   - Binance Square 提及的币种
   - CoinGecko Trending
   - Binance 合约涨跌榜

2. **可交易市场范围**
   - Binance Spot
   - DEX 市场
   - Binance U 本位合约（用于 OI 监控）

注意：
- OI 模块只针对 Binance U 本位合约
- Heat 模块可以覆盖链上币和现货币
- Fusion 时要明确区分“有热度但无 OI 数据”的情况

---

## 6. 非目标

V2 明确不做：
- 自动下单
- 自动开仓/平仓
- 盈利承诺
- 完整量化回测框架
- 链上合约安全审计
- 聪明钱地址追踪
- 资金费率/OI/成交量的多因子复杂模型
- 高频实时撮合级监控

---

## 7. 输出形式

V2 最少输出三类结果：

1. **Heat 排行榜**
   - 哪些币被最多提及、最热

2. **OI 背离排行**
   - 哪些币最符合“OI 明显增加、价格未充分反映”

3. **最终机会卡片**
   - 综合热度 + 背离 + 市场信息的候选卡片

允许输出：
- JSON
- Markdown

---

## 8. 风险与解释性

所有输出必须尽量可解释。

至少需要能回答：
- 这个币为什么进榜
- 是因为广场热度高，还是 OI 背离高，还是两者都高
- 它是 CEX 机会、DEX 机会，还是 Futures timing 机会

---

## 9. V2 验收标准

V2 验收通过，至少满足：

1. 能从 Binance Square 提取 `$TICKER` mention
2. 能过滤稳定币噪音
3. 能接入 CoinGecko Trending
4. 能接入 Binance U 本位合约 OI 与价格变化
5. 能输出 heat_score 排名
6. 能输出 divergence_ratio 排名
7. 能输出融合后的最终候选列表
8. 候选列表中的每个 symbol 都有可解释理由

---

## 10. 开发顺序约束

V2 推荐顺序：

### Phase 1
先实现 Heat Scanner：
- Playwright 抓 Binance Square
- `$TICKER` mention 统计
- 稳定币过滤
- CoinGecko Trending / Binance movers 补充
- 输出 heat ranking

### Phase 2
再实现 OI Divergence Scanner：
- Binance U 本位合约 symbol 列表
- OI 变化
- 价格变化
- divergence ratio 排序

### Phase 3
最后实现 Fusion Ranker：
- 热度分 + 时机分融合
- 输出最终优先级机会卡片

---

## 11. 一句话定义

Heat + OI Opportunity Scanner 是一个通过 **舆情热度发现标的**、通过 **OI 背离寻找时机**、最终形成 **交易候选优先级** 的机会发现系统。
