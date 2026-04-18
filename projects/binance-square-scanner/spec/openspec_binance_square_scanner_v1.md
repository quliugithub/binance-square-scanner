# OpenSpec v1 - Binance Square Scanner

## 北极星目标

这个项目的核心目标，不是做一个会抓帖子的数据脚本，而是做一个**面向交易机会发现的内容驱动扫描器**。

它要解决的问题是：
- 尽早从 Binance Square 内容中发现值得关注的交易线索
- 不把帖子热度直接误当成交易机会
- 把 CEX 与 DEX 的真实可交易性纳入判断
- 产出可解释、可复核、可继续迭代的候选结果

当前阶段的优先级是：
1. 先拉通主链路，做到稳定能跑
2. 先验证这条路线是否真能发现有效候选
3. 在边界清晰的前提下逐步提高信号质量

换句话说，V1 是一个**交易机会发现原型**，不是完整投研平台，也不是自动交易系统。

## 1. 背景

当前原型已经具备以下能力：
- 抓取 Binance Square 帖子
- 对帖子进行情绪和信号评分
- 聚合为币种级别候选
- 对 Binance 现货和链上 DEX 市场做基础可交易性过滤

但当前实现偏原型驱动，边界不够稳定，容易出现：
- 为了提高命中率持续扩范围
- 市场类型之间混用同一套阈值
- 输出结构随着实现细节波动
- 新增逻辑缺乏明确验收标准

本 OpenSpec 用于约束 Binance Square Scanner 的 V1 开发边界。

---

## 2. 产品目标

构建一个“帖子驱动的候选标的扫描器”，从 Binance Square 内容中提取潜在交易线索，并输出结构化候选结果，供人工进一步研判。

Scanner 的目标是：
- 从帖子中发现值得关注的标的
- 结合真实市场数据过滤掉明显不可交易或质量过差的候选
- 输出可解释的候选列表，而不是黑盒分数

---

## 3. 非目标

V1 明确不做以下事情：
- 不自动下单
- 不直接连接交易账户
- 不生成“买入建议”或“收益承诺”
- 不做链上合约安全审计
- 不识别庄控、貔貅、授权风险等深度链上安全风险
- 不做多平台社媒融合分析（只看 Binance Square）
- 不纳入 Binance 合约市场
- 不做实时流式扫描，只支持手动触发

---

## 4. 输入范围

### 4.1 内容输入
来源限定为 Binance Square 帖子。

输入内容包括：
- 帖子正文
- 作者信息
- 标签/hashtag
- 币对/交易对提示
- 帖子互动数据（浏览、评论、点赞、转发等）

### 4.2 市场输入
V1 支持以下市场数据来源：

1. Binance Spot API
   - 24h ticker
   - book ticker
   - 15m klines

2. DexScreener API
   - symbol 搜索
   - pair 市场快照

---

## 5. 输出范围

Scanner 输出一个结构化 JSON 结果，至少包含以下字段：

- `generated_at`
- `scan_config`
- `summary`
- `trade_candidates`
- `passed_trade_candidates`
- `watch_posts`
- `top_coin_signals`

### 5.1 trade_candidates
表示所有达到帖子级基础门槛的候选币。

每个 candidate 至少包含：
- symbol
- bias
- conviction_score
- avg_signal_score
- post_count
- unique_authors
- bullish_posts
- bearish_posts
- total_engagement
- sample_post_ids
- market_snapshot
- price_filter_passed
- price_filter_reasons
- scanner_note

### 5.2 passed_trade_candidates
表示通过市场过滤的候选币，是 `trade_candidates` 的子集。

### 5.3 watch_posts
表示高分帖子列表，用于人工复核。

---

## 6. 核心处理流程

V1 处理流程固定为：

1. 抓取帖子
2. 提取 symbol candidates
3. 帖子打分
4. 聚合币种信号
5. 识别市场
   - 先查 Binance Spot
   - Spot 不支持时，再查 DEX
6. 执行市场过滤
7. 输出结构化结果

不得跳过“市场识别”直接把帖子热度视为交易机会。

---

## 7. 帖子评分边界

帖子评分用于排序和候选提纯，不用于直接给出交易建议。

允许使用的因子：
- 互动量
- 正文长度
- 是否包含明确 symbol
- 是否包含 entry/tp/sl 等结构化术语
- 作者信息是否完整
- 文本情绪（bullish / bearish / neutral）

V1 不引入：
- 作者历史胜率画像
- 社交网络传播图
- 跨平台联动情绪

---

## 8. 市场识别规则

### 8.1 CEX 优先
对于 `XXXUSDT` 等标准 symbol：
- 优先查询 Binance Spot
- 如果可查询到有效市场快照，则视为 `market_type = cex_spot`

### 8.2 DEX 兜底
如果 Binance Spot 不支持：
- 使用 DexScreener 搜索 symbol
- 仅允许选择 baseToken symbol 匹配的 pair
- 在候选 pair 中选择“质量最高”的交易池

### 8.3 DEX 主池选择原则
DEX pair 选择必须考虑：
- symbol 一致性
- 24h volume
- liquidity
- 24h txn count
- priceUsd 是否有效
- 排除明显异常价格和异常涨跌幅池

---

## 9. 过滤规则边界

V1 允许做“可交易性过滤”，不做“投资价值判断”。

### 9.1 CEX 过滤维度
允许使用：
- 24h 成交额
- 买卖盘价差
- 相对 VWAP 动量
- 15m 微趋势
- 24h 涨跌幅

### 9.2 DEX 过滤维度
允许使用：
- 24h 成交额
- liquidity
- 24h 买卖笔数
- 1h / 5m / 24h 价格变化
- 是否存在明显异常池

### 9.3 禁止加入的过滤逻辑
V1 不允许加入以下高主观逻辑，除非先更新 spec：
- 主观基本面评分
- 叙事等级判断
- KOL 人设信誉评分
- 链上持仓分布和聪明钱追踪
- 合约开平仓比、资金费率、OI 数据

---

## 10. 市场类型差异化原则

V1 明确要求：
- CEX 和 DEX 不能共用完全相同的阈值语义
- DEX 阈值应更关注 liquidity 和 txn count
- CEX 阈值应更关注 spread 和 quote volume

实现时允许参数相近，但语义必须分开。

---

## 11. 可解释性要求

所有被过滤的 candidate，必须能回答：
- 为什么没通过
- 是市场不存在，还是流动性不足，还是动量不匹配

因此：
- `price_filter_reasons` 必须保留
- `market_snapshot` 必须尽量保留原始关键指标
- 错误信息要转成人可理解文本，不允许只返回 HTTP 400

---

## 12. 验收标准

V1 验收通过，至少满足：

1. 给定一次扫描，可稳定输出完整 JSON
2. 对于 Binance Spot 可交易标的，能返回 CEX 快照
3. 对于非 Spot 但有链上交易的标的，能返回 DEX 快照
4. 对于无有效市场的标的，能输出明确失败原因
5. 输出中能区分：
   - 候选存在但未通过过滤
   - 候选通过过滤
   - 帖子值得看但标的不可交易
6. 不出现明显的假池优先于主池的情况
7. 代码改动必须围绕本 spec 内的字段和流程进行

---

## 13. V1 待确认项

以下问题暂不在本 spec 内定死，后续需要单独确认：

1. 是否纳入 Binance Futures
2. 是否把链上代币地址解析进输出
3. 是否从帖子正文抽取 entry / tp / sl / leverage
4. 是否做定时扫描和数据库持久化
5. 是否引入告警推送
6. 是否做链上黑名单/貔貅/高税风险过滤

---

## 14. 下一步开发约束

在 V1 结束前，后续代码开发只能做以下类型改动：
- 修复当前 spec 内的 bug
- 提高 CEX/DEX 市场识别准确率
- 调整过滤阈值结构
- 稳定输出字段
- 提高可解释性

以下改动需要先更新 OpenSpec：
- 新增数据源
- 新增市场类型
- 新增输出主结构
- 引入自动下单或告警
- 引入链上安全分析

---

## 15. 一句话定义

Binance Square Scanner V1 是一个“基于帖子线索 + 真实市场可交易性过滤”的候选标的发现工具，不是自动交易系统，也不是完整投研系统。
