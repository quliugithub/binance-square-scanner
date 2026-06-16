# Binance Square Scanner

本仓库用于依赖多路新闻、叙事和技术指标观察虚拟货币的上涨/下跌倾向，输出结构化观察榜供人工研判。它不是自动交易系统，也不直接连接交易账户下单。

核心实现位于 `projects/binance-square-scanner/`。

## 核心能力

- 新闻/叙事来源：Binance Square、BlockBeats、CoinTelegraph/The Block RSS，可选网页搜索。
- 叙事时效过滤：默认只让最近 72 小时内可解析时间的内容进入涨跌观察，降低旧新闻污染。
- 来源质量：对新闻源、社媒源和数据源标注基础权重；若本地研究库已有回测样本，会叠加作者历史命中率；叙事方向分还会按新闻新旧做时效衰减。
- 事件冷却：同一新闻/催化在多轮扫描中重复出现时，会按 `direction_event_memory.sqlite3` 记忆降低后续影响。
- 一级 MEME 雷达：基于 DexScreener token profiles、boosts 和 token pairs，也可接入外部早期 token 候选文件，输出 `early_watch`、`momentum_watch`、`rug_risk`、`overheated`；重复 token 会按 `meme_token_memory.sqlite3` 降权，同时识别流动性/成交加速的二次确认、发现来源交叉验证、paid boost 低牵引风险、社交链接可信度、估值/流动性错配、成交微结构风险和多池流动性结构风险，并把发现来源状态、boost 牵引状态、社交验证状态、估值流动性状态、微结构状态、多池结构状态纳入本地复盘和 edge 校准；默认尝试用 GoPlus 标注合约、税率、owner 和 holder 集中度风险，并记忆同一创建者/owner 的历史风险画像；本地样本足够时会按 MEME 历史收益和流动性留存 edge 校准分数。
- 技术指标：从 Binance K 线计算 15m/1h/4h 多周期 RSI、MA、MACD、ATR、布林带和成交量趋势；spot 不支持时 fallback 到 U 本位合约 K 线并在输出中标注。
- 涨跌观察：`market_direction_board` 汇总新闻/叙事和技术面，输出 `rise_watch`、`fall_watch`、`neutral_watch`；证据方向冲突时会扣分并降低置信度，并用 BTC/ETH 多周期技术面作为 market regime 过滤山寨币顺风/逆风。
- 方向评估：`neutral_watch` 使用 ATR 动态阈值，避免高波动币和低波动币都按固定 0.5% 判断；15m/1h/4h 技术面混乱或逆向时会通过 `technical_alignment_risk` 降权和封顶，方向分较强但缺少成交量确认或处于缩量时会通过 `volume_confirmation` 降权和封顶，RSI/布林带/MA25 偏离显示追高或追空时会通过 `price_position_risk` 降权，只有单一核心来源支撑强方向时会通过 `source_confirmation` 降权；实时方向榜会在本地样本足够时按历史 edge 轻微校准分数和置信度。
- 完整监控：可叠加合约资金、宏观新闻、公告、链上和热点模块。

## 快速开始

要求 Python 3.10+ 和 `requests`。

```bash
pip install requests
```

Windows 上如果 `python` 指向 Python 2，请使用 `py -3`。

```bash
cd projects/binance-square-scanner

# 快速涨跌观察：新闻/叙事 + 技术指标
py -3 monitoring_hub.py --direction-only --limit 20 --technical-limit 12 --lookback-hours 72

# 记录本次涨跌观察，后续用于命中率评估
py -3 monitoring_hub.py --direction-only --limit 20 --technical-limit 12 --record-direction-signals

# 评估已经到期的历史方向信号
py -3 monitoring_hub.py --evaluate-direction-signals --evaluation-windows 1,4,12,24

# 输出方向信号命中率报告
py -3 monitoring_hub.py --direction-research-report --research-min-samples 3

# 完整 hub：新闻、技术、公告、合约资金、链上和热点
py -3 monitoring_hub.py --limit 50 --top-n 8 --technical-limit 12 --lookback-hours 72

# 单独跑一级 MEME 早期雷达，重复 token 会进入冷却
py -3 meme_early_radar.py --limit 40 --top-n 10 --token-cooldown-hours 24

# 接入外部早期源导出的 token 列表，支持 JSON / JSONL / CSV
py -3 meme_early_radar.py --extra-candidates results/pump_candidates.jsonl --limit 80

# 可选：给 GoPlus 安全检查提供 token；也可以设置 GOPLUS_API_TOKEN 环境变量
py -3 meme_early_radar.py --limit 40 --goplus-api-token YOUR_TOKEN

# 记录并评估一级 MEME 候选表现
py -3 monitoring_hub.py --record-meme-signals --meme-limit 30
py -3 monitoring_hub.py --evaluate-meme-signals --meme-evaluation-windows 1,4,24
py -3 monitoring_hub.py --meme-research-report --meme-research-min-samples 3
```

输出会写入 `projects/binance-square-scanner/results/`，包含 `.json` 结构化数据和 `.md` 可读报告。

## 关键输出

- `system_summary`：各监控维度数量和涨跌观察数量。
- `market_direction_board`：按方向分数排序的涨跌观察榜，`quality_flags.direction_conflict` 会标注多空证据冲突，`quality_flags.market_regime` 会标注 BTC/ETH 大盘顺风或逆风，`quality_flags.catalyst_types` 会标注技术、合约资金、公告、宏观等催化类型，`quality_flags.technical_alignment_risk`、`quality_flags.volume_confirmation`、`quality_flags.price_position_risk`、`quality_flags.source_confirmation`、`quality_flags.volatility_risk` 和 `quality_flags.futures_crowding_risk` 会标注多周期技术冲突、缩量/放量确认、追高追空位置风险、单源方向风险、高 ATR 与合约资金拥挤的降权和置信度封顶。
- `meme_early_radar`：一级 MEME 早期候选、动量候选、风险候选和过热候选，包含 `discovery_status`、`memory_weight`、`memory_delta`、`local_research_edge`、`security` 与 `security_note`。
- `meme_research`：可选写入 MEME 候选研究库，并在窗口到期后评估真实收益；报告会按 stage、发现来源验证、boost 牵引、社交验证、估值流动性、成交微结构、多池流动性结构、风险旗标、creator/owner 历史画像和地址维度分组。
- `technical_monitor`：每个币种的 RSI、均线、MACD、ATR 等技术指标，以及 `multi_timeframe` 多周期共振状态。
- `narrative_monitor`：新闻/叙事热度、情绪和关联币种。
- `narrative_monitor.source_stats.freshness_filter`：本次旧内容过滤数量和按来源统计。
- `narrative_monitor.source_stats.dedupe_filter`：本次重复内容过滤数量，覆盖重复 `content_id`、URL 和近似正文。
- `narrative_monitor.source_stats.event_cooldown`：跨运行重复事件统计，以及本轮冷却降权情况。
- `narrative_monitor.source_quality`：来源类型、有效权重和是否有本地回测验证。
- `direction_research`：可选写入方向信号库，并在历史评估时输出命中率；报告会按证据来源、催化类型、方向+催化类型、技术共振风险、成交量确认、价格位置风险、单源/多源确认、波动率风险、合约资金拥挤风险、冲突程度、BTC/ETH market regime 顺风/逆风分组。

更多模块、参数和 OpenSpec 说明见 [projects/binance-square-scanner/README.md](projects/binance-square-scanner/README.md)。
