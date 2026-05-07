# OpenSpec Change — 添加币安公告监控模块

## Change ID: `change_add_binance_announcement_monitor`
## 状态: Draft → 待审批 → 实施

---

## 1. 变更背景

### 1.1 现状问题

当前 `narrative_monitor` 的叙事识别**仅依赖 Binance Square 用户帖子**，存在以下盲区：

- 币安官方公告（新币上线、合约开通、Launchpool 等）是极强的叙事催化剂，但当前完全缺失
- 用户帖子中的叙事讨论往往滞后于官方公告 30 分钟 ~ 数小时
- 缺少公告驱动的"硬信号"，导致叙事阶段判断可能误判（比如公告已发但 Square 还没讨论起来）

### 1.2 目标

新增 `announcement_monitor` 模块，采集币安官方公告，为叙事系统提供**事件驱动的催化信号**。

---

## 2. 变更范围

### 2.1 新增文件

| 文件 | 说明 |
|---|---|
| `announcement_monitor.py` | 公告采集、分类、信号提取模块 |
| `spec/openspec_binance_announcement_monitor_v1.md` | 模块完整设计规格 |

### 2.2 修改文件

| 文件 | 变更 |
|---|---|
| `monitoring_hub.py` | 引入 `announcement_monitor`，将公告事件注入 `focus_board` 和报告 |
| `narrative_monitor.py` | 接收公告信号作为叙事确认加分项（可选，V1 可暂不改） |
| `README.md` | 补充公告监控模块说明 |

---

## 3. 数据来源

### 3.1 主要来源：Binance 官方公告 API / 页面

V1 优先尝试以下路径（按可行性排序）：

1. **Binance Square 官方账号帖子** — `binance-official` 等官方账号的 Square 帖子，复用现有 `scrape_square` 采集链路
2. **Binance 公告页面抓取** — `https://www.binance.com/en/support/announcement` 列表页
3. **Binance API 公告接口** — 如果存在公开的公告列表 API（需验证可用性）

V1 实际实现以**路径 1（Square 官方账号）为首选**，因为：
- 复用已有的采集和评分基础设施
- 无需额外处理反爬或页面结构变化
- 官方账号发帖 = 公告的镜像，内容足够

### 3.2 不纳入的来源（V1）

- Twitter/X 官方账号（属多平台扩展，V2 再做）
- Telegram 公告频道（同上）
- 链上合约部署事件（属链上监控范畴）

---

## 4. 公告分类体系

V1 将公告分为以下类型，每种类型对应不同的催化强度：

| 类型 | 识别规则 | 催化强度 | 示例 |
|---|---|---|---|
| **new_listing** | 关键词：will list, adds, new listing, will open trading | 🔴 高 | "Binance Will List XYZUSDT" |
| **futures_listing** | 关键词：futures, perpetual, delivery, coin-m futures | 🔴 高 | "Binance Futures Will Launch XYZ Perpetual" |
| **launchpool** | 关键词：launchpool, megadrop, launchpad | 🟡 中-高 | "Binance Launchpool Project XYZ" |
| **deposit_withdrawal** | 关键词：deposit, withdrawal, suspend, resume, network maintenance | 🟡 中 | "Binance Will Support XYZ Network Upgrade" |
| **delisting** | 关键词：delist, remove, trading pair removal, cease trading | 🔴 高（负面） | "Binance Will Delist XYZ" |
| **trading_comp** | 关键词：trading competition, rewards, airdrop, bonus | 🟢 低-中 | "Binance Trading Competition" |
| **general** | 不匹配以上规则 | ⚪ 低 | 其他公告 |

---

## 5. 核心逻辑

### 5.1 采集流程

```
scrape_square(author_filter=['binance', 'binanceofficial', ...])
       ↓
  过滤官方账号帖子
       ↓
  公告类型分类（关键词匹配）
       ↓
  提取关联币种（交易对正则 + 帖子标签）
       ↓
  生成公告事件列表
```

### 5.2 输出结构

```json
{
  "module": "announcement_monitor_v1",
  "generated_at": "2026-05-07T08:00:00Z",
  "summary": {
    "total_announcements": 12,
    "high_impact": 2,
    "medium_impact": 4,
    "symbols_mentioned": ["XYZUSDT", "ABCUSDT"]
  },
  "announcements": [
    {
      "type": "new_listing",
      "impact": "high",
      "title": "Binance Will List XYZ (XYZUSDT)",
      "symbols": ["XYZUSDT"],
      "publish_time": "...",
      "web_link": "...",
      "body_preview": "...",
      "catalyst_hint": "new_listing_通常引发短期拉盘+高换手"
    }
  ],
  "narrative_catalysts": [
    {
      "symbol": "XYZUSDT",
      "event_type": "new_listing",
      "impact": "high",
      "related_narrative_candidates": ["AI", "DeFi"]
    }
  ]
}
```

### 5.3 与 Monitoring Hub 集成

`monitoring_hub.py` 新增公告模块后，报告增加以下板块：

```markdown
## Announcements & Catalysts

### 🔴 Binance Will List XYZ (XYZUSDT)
- type: `new_listing` | impact: `high`
- symbols: XYZUSDT
- link: ...
```

同时在 `system_summary` 增加：
- `announcement_high_impact`: 高影响公告数
- `announcement_total`: 公告总数

---

## 6. V1 边界

**做：**
- 从 Binance Square 官方账号采集公告帖子
- 公告类型分类（7 种）
- 提取关联币种
- 输出结构化公告事件
- 注入 Monitoring Hub 报告

**不做（V2 再做）：**
- 公告页面直接抓取（依赖反爬策略）
- 公告与叙事模块的自动关联加分（V1 先输出，人工研判）
- Twitter/Telegram 多源聚合
- 实时推送 / 告警
- 公告历史数据库

---

## 7. 实施步骤

1. 创建 `spec/openspec_binance_announcement_monitor_v1.md` — 完整规格文档
2. 实现 `announcement_monitor.py` — 核心模块
3. 修改 `monitoring_hub.py` — 集成公告模块
4. 更新 `README.md` — 补充模块说明
5. 测试运行 — `python monitoring_hub.py` 验证输出

---

## 8. 验收标准

- [ ] `announcement_monitor.py` 可独立运行，输出公告列表
- [ ] `monitoring_hub.py` 报告包含公告板块
- [ ] 公告类型分类正确（基于关键词规则）
- [ ] 关联币种提取正确
- [ ] 无额外第三方依赖（复用 `requests`）
