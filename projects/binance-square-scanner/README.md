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
| **Funding Rate Monitor** | Binance Futures API | 资金费率 z-score、OI 变化、综合评分 |
| **Macro Calendar** | CoinDesk / The Block + 宏观日历 | 催化评分、近期新闻、重大事件 |

## 快速开始

### 环境要求

- Python 3.10+
- 依赖：`requests`（无其他第三方依赖）

```bash
pip install requests
```

### 运行 Monitoring Hub（全模块聚合）

```bash
# 默认参数运行
python monitoring_hub.py

# 自定义参数
python monitoring_hub.py \
  --limit 80 \
  --top-n 10 \
  --futures-symbols BTCUSDT ETHUSDT SOLUSDT \
  --onchain-limit 20
```

输出文件保存在 `results/` 目录，同时生成 `.json` 和 `.md` 两种格式。

### 单独运行各模块

```bash
# 热点雷达
python hotspot_radar.py --limit 50 --top-n 8

# 叙事监控
python narrative_monitor.py --limit 50 --top-n 5

# 链上强买盘
python onchain_strong_buy_monitor.py --limit 15 --top-n 8

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
2. **公告 & 催化** — 币安官方公告（上币/合约/Launchpool/下架等），按催化强度排序
3. **主流币合约资金面** — BTC/ETH 资金费率、OI、综合评分与操作建议
4. **宏观 & 新闻** — 催化评分、近期事件、重要新闻
5. **强资金买入候选** — 综合评分排名
6. **Top 叙事** — 当前市场主要叙事板块
7. **链上强买盘 / 观察池** — 链上资金异动标的

## 设计原则

1. **机会发现，不自动交易** — 所有输出仅供人工研判，不接入下单
2. **可解释性优先** — 每个候选都附带评分依据和信号来源
3. **多源交叉验证** — 热度、叙事、资金面、链上数据相互印证
4. **噪音过滤** — 过滤假池、低流动性、小样本噪音
5. **模块独立** — 每个模块可单独运行，互不耦合

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
