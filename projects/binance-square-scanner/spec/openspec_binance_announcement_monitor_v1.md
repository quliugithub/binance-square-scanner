# OpenSpec v1 - Binance Announcement Monitor

## 北极星目标

Binance Announcement Monitor 的目标，是采集币安官方公告，识别**对特定币种有催化作用的事件**，为叙事系统和机会发现提供事件驱动的"硬信号"。

它要回答的核心问题是：
- 币安最近发了哪些公告
- 哪些公告会对特定币种产生价格催化
- 哪些公告类型催化强度最高
- 当前哪些币种刚刚被币安"点名"

一句话定义：
**Binance Announcement Monitor V1 是一个面向币安官方公告的事件采集与催化分类模块。**

---

## 1. 模块目标

该模块负责：
- 采集币安官方账号在 Binance Square 的发帖
- 对帖子进行公告类型分类
- 提取关联币种
- 评估催化强度
- 输出结构化公告事件列表

该模块不负责：
- 自动下单
- 直接抓取 Binance 官网公告页面（V1 不做）
- 多平台公告聚合（Twitter/Telegram 等，V2 再做）
- 公告历史数据库存储
- 实时推送/告警

---

## 2. 输入来源

### 2.1 V1 主要来源：Binance Square 官方账号

复用现有 `binance_square_tool.scrape_square()` 采集链路，通过作者过滤筛选官方账号。

已知官方账号标识（需根据实际验证）：
- `binance`
- `Binance'
- `binanceofficial`
- 含 "binance" 的认证账号

### 2.2 V1 不使用的来源

- Binance 官网公告页面（反爬复杂度高，V2 再做）
- Twitter @binance（多平台扩展）
- Telegram Binance 公告频道（多平台扩展）

---

## 3. 公告分类体系

### 3.1 类型定义

| type | 关键词规则（OR 逻辑） | impact | 说明 |
|---|---|---|---|
| `new_listing` | "will list", "adds", "new listing", "will open trading", "lists" | high | 新币上线现货 |
| `futures_listing` | "futures", "perpetual", "delivery", "coin-m", "usdⓢ-m", "will launch.*futures" | high | 合约上线 |
| `launchpool` | "launchpool", "megadrop", "launchpad" | high | Launchpool/Megadrop |
| `delisting` | "delist", "remove", "cease trading", "trading pair removal" | high (negative) | 下架公告 |
| `deposit_withdrawal` | "deposit", "withdrawal", "suspend", "resume", "network upgrade", "maintenance", "mainnet swap" | medium | 充提/网络相关 |
| `trading_comp` | "trading competition", "rewards", "airdrop", "bonus", "challenge" | low-medium | 活动/竞赛 |
| `general` | 以上均不匹配 | low | 其他公告 |

### 3.2 分类优先级

当一条公告匹配多个类型时，按以下优先级取第一个：
`new_listing` > `futures_listing` > `launchpool` > `delisting` > `deposit_withdrawal` > `trading_comp` > `general`

---

## 4. 核心处理流程

### 4.1 采集

```python
raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
# 过滤出官方账号帖子
official_posts = [p for p in raw_posts if is_official_author(p)]
```

### 4.2 官方账号识别

```python
OFFICIAL_MARKERS = ['binance', 'binanceofficial', 'binance exchange']

def is_official_author(post: dict) -> bool:
    username = (post.get('username') or '').lower()
    display = (post.get('author') or '').lower()
    verified = post.get('is_verified') or post.get('is_official') or False
    return any(marker in username or marker in display for marker in OFFICIAL_MARKERS) or verified
```

### 4.3 分类

```python
ANNOUNCEMENT_RULES = [
    ('new_listing',     ['will list', 'adds', 'new listing', 'will open trading', 'lists']),
    ('futures_listing', ['futures', 'perpetual', 'delivery', 'coin-m', 'usdⓢ-m']),
    ('launchpool',      ['launchpool', 'megadrop', 'launchpad']),
    ('delisting',       ['delist', 'remove', 'cease trading']),
    ('deposit_withdrawal', ['deposit', 'withdrawal', 'suspend', 'resume', 'network upgrade', 'maintenance']),
    ('trading_comp',    ['trading competition', 'rewards', 'airdrop', 'bonus', 'challenge']),
]

def classify_announcement(text: str) -> tuple[str, str]:
    text_lower = text.lower()
    for ann_type, keywords in ANNOUNCEMENT_RULES:
        if any(kw in text_lower for kw in keywords):
            IMPACT_MAP = {
                'new_listing': 'high', 'futures_listing': 'high',
                'launchpool': 'high', 'delisting': 'high',
                'deposit_withdrawal': 'medium', 'trading_comp': 'low',
            }
            return ann_type, IMPACT_MAP.get(ann_type, 'low')
    return 'general', 'low'
```

### 4.4 币种提取

从帖子的 `trading_pairs` 字段和正文中提取关联币种：
- 优先使用帖子自带的 `trading_pairs`
- 回退到正文中 `XXXXUSDT` 模式匹配

---

## 5. 输出结构

### 5.1 完整 payload

```json
{
  "module": "announcement_monitor_v1",
  "generated_at": "ISO-8601",
  "scan_config": { "limit": 50, "start_page": 1, "max_pages": 4 },
  "summary": {
    "total_announcements": 8,
    "high_impact": 2,
    "medium_impact": 3,
    "low_impact": 3,
    "symbols_mentioned": ["XYZUSDT", "ABCUSDT"]
  },
  "announcements": [
    {
      "type": "new_listing",
      "impact": "high",
      "title": "...",
      "symbols": ["XYZUSDT"],
      "publish_time": "...",
      "author": "Binance",
      "web_link": "...",
      "body_preview": "..."
    }
  ]
}
```

### 5.2 Markdown 渲染

```markdown
## Announcements & Catalysts

### 🔴 Binance Will List XYZ (XYZUSDT)
- type: `new_listing` | impact: `high`
- symbols: XYZUSDT
- time: 2026-05-07 08:00
- link: https://...

### 🟡 Binance Will Support ABC Network Upgrade
- type: `deposit_withdrawal` | impact: `medium`
- symbols: ABCUSDT
```

---

## 6. 与系统集成

### 6.1 Monitoring Hub 集成

在 `monitoring_hub.py` 的 `build_hub_payload()` 中新增：

```python
import announcement_monitor
announcement_payload = announcement_monitor.build_announcement_payload(...)
```

在 `system_summary` 中新增字段：
- `announcement_total`
- `announcement_high_impact`

在 `focus_board` 中新增：
- `high_impact_announcements`

在 `render_markdown()` 中新增 "Announcements & Catalysts" 板块。

### 6.2 叙事模块联动（V1 可选）

V1 不自动修改叙事分数，但可以在报告中提供关联提示：
- 当某币种同时出现在公告和叙事候选中时，标记为 "事件+叙事共振"

---

## 7. V1 边界

**做：**
- Square 官方账号帖子采集
- 7 种公告类型分类
- 币种提取
- 催化强度评估
- 结构化输出 + Markdown 报告
- Monitoring Hub 集成

**不做：**
- Binance 官网页面直接抓取
- Twitter/Telegram 多源聚合
- 自动关联叙事加分
- 实时告警推送
- 历史公告数据库
