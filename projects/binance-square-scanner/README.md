# Binance Square Scanner — 交易机会发现系统

> **V1 定位：** 多源热度 + 叙事理解 + 资金确认的机会发现系统，**不是自动交易系统**。

## 系统概述

本项目从 Binance Square 内容、交易所行情、链上数据、合约资金面、宏观事件等多个维度扫描市场，发现值得关注的交易线索，输出结构化候选池供人工研判。

## 模块架构

```
monitoring_hub.py          ← 统一监控总入口
├── hotspot_radar.py       ← 热点雷达：趋势候选 & 买入候选
├── narrative_monitor.py   ← 叙事监控：板块叙事阶段 & 情绪
├── announcement_monitor.py       ← 币安公告监控：官方公告分类 & 催化信号
├── onchain_strong_buy_monitor.py  ← 链上强买盘发现
├── meme_early_radar.py            ← 一级 MEME 早期雷达：新 token profile / boost / pair 质量
├── funding_rate_monitor.py        ← 主流币合约资金面
├── macro_calendar_monitor.py      ← 宏观事件 & 新闻催化
├── heat_scanner.py        ← 热度扫描引擎
├── binance_square_scanner.py      ← Binance Square 帖子抓取
└── lib/
    ├── binance_square_tool.py     ← Square API 工具
    ├── binance_square_strategy.py ← 帖子信号评分策略
    └── binance_square_research.py ← 研究辅助
```

### 各模块职责

| 模块 | 输入 | 核心输出 |
|---|---|---|
| **Hotspot Radar** | Square 帖子 + Binance 行情 + OI | 趋势候选、买入候选、强资金买入候选 |
| **Narrative Monitor** | Square 帖子 | 叙事排名（阶段、情绪、关联币种） |
| **Announcement Monitor** | Square 官方账号帖子 | 公告分类（上币/合约/Launchpool/下架等）+ 催化强度 |
| **On-chain Strong Buy** | DexScreener API | 链上强买盘候选 + 观察池 |
| **Meme Early Radar** | DexScreener token profiles / boosts / pairs + GoPlus security | 一级 MEME `early_watch`、`momentum_watch`、`rug_risk`、`overheated`；重复出现时识别流动性/成交加速、发现来源交叉验证、paid boost 低牵引、社交链接可信度、估值/流动性错配、成交微结构和多池流动性结构，并标注 owner/holder 集中度和创建者历史风险 |
| **Funding Rate Monitor** | Binance Futures API | 资金费率 z-score、OI 变化、综合评分 |
| **Macro Calendar** | CoinDesk / The Block + 宏观日历 | 催化评分、近期新闻、重大事件 |

## 快速开始

### 环境要求

- Python 3.10+
- 依赖：`requests`（无其他第三方依赖）

```bash
pip install requests
```

Windows 上如果 `python` 指向 Python 2，请使用 `py -3` 运行下面的命令。

### 运行 Monitoring Hub（全模块聚合）

```bash
# 默认参数运行
python monitoring_hub.py

# 只依赖新闻/叙事 + 技术指标生成涨跌观察榜（更快）
python monitoring_hub.py --direction-only --limit 20 --technical-limit 12 --lookback-hours 72

# 记录本次方向观察，后续用于命中率评估
python monitoring_hub.py --direction-only --limit 20 --technical-limit 12 --record-direction-signals

# 评估已经到期的历史方向观察
python monitoring_hub.py --evaluate-direction-signals --evaluation-windows 1,4,12,24

# 输出方向观察命中率报告
python monitoring_hub.py --direction-research-report --research-min-samples 3

# 记录一级 MEME 候选，后续用于涨跌评估
python monitoring_hub.py --record-meme-signals --meme-limit 30

# 评估已经到期的 MEME 候选
python monitoring_hub.py --evaluate-meme-signals --meme-evaluation-windows 1,4,24

# 输出 MEME 候选研究报告；低样本分组先过滤掉
python monitoring_hub.py --meme-research-report --meme-research-min-samples 3

# 自定义参数
python monitoring_hub.py \
  --limit 80 \
  --top-n 10 \
  --futures-symbols BTCUSDT ETHUSDT SOLUSDT \
  --technical-limit 12 \
  --lookback-hours 72 \
  --onchain-limit 20
```

输出文件保存在 `results/` 目录，同时生成 `.json` 和 `.md` 两种格式。
JSON 中的 `narrative_monitor.source_stats.freshness_filter` 和 `narrative_monitor.source_stats.dedupe_filter`
分别记录过期新闻过滤、重复内容过滤的数量和来源分布；`narrative_monitor.source_stats.event_cooldown`
记录跨运行重复事件冷却。Markdown 摘要会显示 fetched/fresh/deduped 和 cooldown 计数。

### 单独运行各模块

```bash
# 热点雷达
python hotspot_radar.py --limit 50 --top-n 8

# 叙事监控
python narrative_monitor.py --limit 50 --top-n 5

# 链上强买盘
python onchain_strong_buy_monitor.py --limit 15 --top-n 8

# 一级 MEME 早期雷达，重复 token 会进入冷却
python meme_early_radar.py --limit 40 --top-n 10 --chains solana,base,bsc,ethereum --token-cooldown-hours 24

# 把外部早期源导出的 token 列表接入雷达；支持 JSON / JSONL / CSV
python meme_early_radar.py --extra-candidates results/pump_candidates.jsonl --limit 80

# 可选：给 GoPlus 安全检查提供 token；也可以设置 GOPLUS_API_TOKEN 环境变量
python meme_early_radar.py --limit 40 --goplus-api-token YOUR_TOKEN

# 单独雷达中关闭 GoPlus 安全检查
python meme_early_radar.py --limit 40 --disable-security-check

# 单独雷达中关闭本地历史 edge 校准
python meme_early_radar.py --limit 40 --disable-research-edge

# 合约资金面（默认监控 BTC/ETH）
python funding_rate_monitor.py --symbols BTCUSDT ETHUSDT

# 宏观日历
python macro_calendar_monitor.py

# 币安公告监控
python announcement_monitor.py --limit 80 --max-pages 6
```

## 命令行参数（monitoring_hub.py）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--limit` | 50 | Square 帖子抓取量 |
| `--start-page` | 1 | 起始页 |
| `--max-pages` | 4 | 最大翻页数 |
| `--top-n` | 8 | 每个维度取前 N 个 |
| `--onchain-limit` | 15 | 链上数据查询量 |
| `--futures-symbols` | BTCUSDT, ETHUSDT | 监控的合约交易对 |
| `--technical-limit` | 12 | 技术指标 watchlist 最大币种数 |
| `--lookback-hours` | 72 | 新闻/叙事进入涨跌观察前的时效窗口；设为 0 关闭 |
| `--event-cooldown-hours` | 48 | 同一新闻/催化重复出现时的跨运行冷却窗口；设为 0 关闭 |
| `--event-memory-db` | `results/direction_event_memory.sqlite3` | 叙事事件记忆库路径 |
| `--meme-limit` | 30 | 完整 hub 中一级 MEME 候选扫描量 |
| `--meme-max-age-hours` | 72 | 一级 MEME pair 最大年龄 |
| `--meme-min-liquidity-usd` | 10000 | 一级 MEME 最小参考流动性；低于该值会打风险旗标 |
| `--meme-token-cooldown-hours` | 24 | 一级 MEME 重复 token 冷却窗口；设为 0 关闭 |
| `--meme-token-memory-db` | `results/meme_token_memory.sqlite3` | 一级 MEME token 记忆库路径 |
| `--disable-meme-security-check` | false | 完整 hub 中关闭一级 MEME GoPlus 安全检查 |
| `--goplus-api-token` | 环境变量 `GOPLUS_API_TOKEN` | GoPlus 安全检查 token；不填时仍会尝试公开接口 |
| `--meme-extra-candidates` | 可重复 | 完整 hub 中额外接入外部早期 token 候选文件，支持 JSON / JSONL / CSV；字段兼容 `chainId/chain_id/chain` 和 `tokenAddress/token_address/address/mint/contractAddress` |
| `--record-meme-signals` | false | 将本次一级 MEME 候选写入研究库 |
| `--meme-research-db` | `results/meme_research.sqlite3` | 一级 MEME 候选研究库路径 |
| `--evaluate-meme-signals` | false | 只评估已到期的历史 MEME 候选，不运行新扫描 |
| `--meme-evaluation-windows` | 1,4,24 | MEME 候选评估窗口，单位小时 |
| `--meme-research-report` | false | 输出 MEME 候选按 stage、window、confidence、发现来源验证、boost 牵引、社交验证、估值流动性、成交微结构、多池流动性结构、risk_flag、source_flag、creator/owner 历史画像等维度的收益报告 |
| `--meme-research-min-samples` | 1 | MEME 研究报告分组最小样本数 |
| `--meme-research-top-n` | 20 | MEME 研究报告每类分组/案例最大输出数量 |
| `--disable-meme-research-edge` | false | 完整 hub 中关闭一级 MEME 本地历史 edge 校准 |
| `--meme-edge-min-samples` | 8 | 一级 MEME 本地历史 edge 校准每个分组最小样本数 |
| `--disable-meme-early` | false | 完整 hub 中关闭一级 MEME 雷达 |
| `--direction-only` | false | 只跑新闻/叙事 + 技术指标涨跌观察榜 |
| `--include-web-search` | false | direction-only 模式中额外启用网页搜索新闻源 |
| `--record-direction-signals` | false | 将本次 `market_direction_board` 写入方向研究库 |
| `--direction-db` | `results/direction_signals.sqlite3` | 方向信号研究库路径 |
| `--disable-direction-edge` | false | 关闭实时方向榜的本地历史 edge 校准 |
| `--direction-edge-min-samples` | 8 | 本地历史 edge 校准每个分组最小样本数 |
| `--evaluate-direction-signals` | false | 只评估已到期的历史方向信号，不运行新扫描 |
| `--evaluation-interval` | 1h | 方向信号评估使用的 K 线周期 |
| `--evaluation-windows` | 1,4,12,24 | 评估窗口，例如 1h K 线下的 1/4/12/24 根 |
| `--neutral-threshold-pct` | 0.5 | `neutral_watch` 的 fallback 阈值；有技术面 ATR 时会按 ATR 和窗口动态计算 |
| `--direction-research-report` | false | 输出方向信号命中率报告，不运行新扫描 |
| `--research-min-samples` | 1 | 命中率报告中每个分组最少样本数 |
| `--research-top-n` | 20 | 命中率报告每个分组最多输出数量 |
| `--output` | 自动生成 | 自定义输出路径 |

### announcement_monitor.py 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--limit` | 80 | Square 帖子抓取量 |
| `--start-page` | 1 | 起始页 |
| `--max-pages` | 6 | 最大翻页数 |
| `--output` | 自动生成 | 自定义输出路径 |

## 输出结构

```
results/
├── monitoring_hub_20260506_205151.json   ← 完整结构化数据
└── monitoring_hub_20260506_205151.md     ← 可读 Markdown 报告
```

### Markdown 报告包含

1. **系统摘要** — 各维度候选数量一览
2. **涨跌观察榜** — 汇总新闻/叙事、公告、技术指标、合约资金与宏观新闻的 rise/fall/neutral 观察方向，并标注多空证据冲突；BTC/ETH 多周期技术面会作为 market regime 过滤山寨币顺风/逆风，15m/1h/4h 多周期混乱或逆向会通过 `technical_alignment_risk` 降权和封顶，缩量或缺少放量确认会通过 `volume_confirmation` 降权和封顶，RSI/布林带/MA25 偏离显示追高或追空时会通过 `price_position_risk` 降权，强方向只有单一核心来源支撑时会通过 `source_confirmation` 降权
3. **方向研究记录** — 可选记录本次方向观察，用于后续真实涨跌评估；报告包含 `by_catalyst_type`、`by_catalyst_family`、`by_direction_catalyst_type`、`by_technical_alignment_risk`、`by_volume_confirmation`、`by_price_position_risk`、`by_source_confirmation`、`by_volatility_risk`、`by_futures_crowding_risk`、`by_direction_conflict` 与 `by_market_regime_*` 分组；实时方向榜会在样本足够时用这些本地历史 edge 轻微校准分数和置信度
4. **来源质量** — 新闻源/社媒源/数据源有效权重、叙事时效衰减、事件冷却，以及是否有本地回测验证
5. **一级 MEME 早期雷达** — 基于 token profile、boost、pair 年龄、流动性、交易笔数和社交链接输出 `early_watch` / `momentum_watch` / `rug_risk`，并用 token 记忆区分新发现、重复出现、流动性/成交加速或衰退；发现来源验证会区分外部源、DexScreener profile、boost 的单源或交叉确认；paid boost 会和成交额、交易笔数、社交链接、流动性做牵引校验，避免纯买量候选排太前；社交验证会区分无链接、仅官网、单一社交、核心社交存在和 Twitter+Telegram 组合确认；估值流动性会按 FDV/liquidity、池子深度、h1 成交额/流动性和交易笔数识别健康启动、薄池、高 FDV 低流动性或极端错配；成交微结构会按 h1 成交额/流动性、m5 集中度、买卖比和交易笔数识别健康早期流、均衡动量、瞬时拉盘或刷量风险；多池结构会按主池流动性占比、活跃池数量、显著池数量和新池迁移识别流动性分散或换池风险；这些状态都会进入研究报告和本地 edge 复盘；GoPlus 可用时会补充 honeypot、税率、owner、mint、LP、创建者持仓和 holder 集中度等安全旗标，同时记忆同一创建者/owner 的历史风险画像；本地样本足够时会按 MEME 历史收益和流动性留存 edge 校准分数
6. **公告 & 催化** — 币安官方公告（上币/合约/Launchpool/下架等），按催化强度排序
7. **主流币合约资金面** — BTC/ETH 资金费率、OI、综合评分与操作建议
8. **技术指标** — 15m/1h/4h RSI、MA、MACD、ATR 与 rise/fall/neutral 观察方向；spot 不支持的币会尝试 U 本位合约 K 线并标注 `kline_market`
9. **宏观 & 新闻** — 催化评分、近期事件、重要新闻
10. **强资金买入候选** — 综合评分排名
11. **Top 叙事** — 当前市场主要叙事板块
12. **链上强买盘 / 观察池** — 链上资金异动标的

## 设计原则

1. **机会发现，不自动交易** — 所有输出仅供人工研判，不接入下单
2. **可解释性优先** — 每个候选都附带评分依据和信号来源
3. **多源交叉验证** — 热度、叙事、资金面、链上数据相互印证
4. **噪音过滤** — 过滤假池、低流动性、小样本噪音、过期新闻和重复新闻/转发
5. **事件冷却** — 同一新闻/催化或一级 MEME token 跨运行反复出现时降低后续方向分影响
6. **一级 MEME 风险优先** — GoPlus 安全层只能降低明显合约风险，不等于完整审计；接口不可用、链不支持或数据缺失时会标记 `contract_security_unavailable`
7. **冲突降权** — 技术、叙事、资金和公告方向互相打架时降低方向分和置信度
8. **来源可信度分层** — 未回测来源只给保守权重；有历史回测后再按作者/来源命中率调整
9. **结果可回看** — 方向观察可落库，并在窗口到期后计算真实收益和命中率
10. **模块独立** — 每个模块可单独运行，互不耦合

## OpenSpec 设计文档

详细的设计规格见 `spec/` 目录：

- [Opportunity Discovery System](spec/openspec_opportunity_discovery_system_v1.md) — 系统总体架构
- [Binance Square Scanner](spec/openspec_binance_square_scanner_v1.md) — 帖子扫描器
- [Trend Monitor](spec/openspec_trend_monitor_v1.md) — 趋势监控
- [On-chain Strong Buy](spec/openspec_onchain_strong_buy_monitor_v1.md) — 链上买盘监控
- [Major Coin Futures](spec/openspec_major_coin_futures_monitor_v1.md) — 合约资金面
- [Heat OI Opportunity](spec/openspec_heat_oi_opportunity_scanner_v2.md) — 热度 OI 扫描

## V1 边界

**不做：**
- 自动下单 / 连接交易所账户
- 智能合约安全审计（貔貅、授权风险等）
- 链上地址级鲸鱼归因
- 多平台社媒融合（仅 Binance Square）
- 实时流式扫描（手动触发）
