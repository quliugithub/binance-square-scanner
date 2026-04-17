# 🎁 礼品卡交易监控系统

## 系统概述
这是一个基于6551 API的自动化礼品卡买家/卖家发现和监控系统，专门用于识别高价值交易机会。

## 主要功能
- ✅ 实时关键词监控
- ✅ 智能可信度评分
- ✅ 自动分类标记
- ✅ 多级提醒系统
- ✅ 区域市场分析
- ✅ 数据库持久化存储

## 配置文件说明

### `giftcard_monitor_config.json`
包含所有监控规则和配置：
- **high_priority**: 高优先级关键词组合
- **regional**: 区域定向搜索规则  
- **user_tracking**: 重点用户跟踪列表

### 核心监控规则
1. **高价值买家识别**: 信任评分≥70
2. **实时机会捕捉**: 包含具体金额($100+)的需求
3. **区域市场覆盖**: 东南亚、中东、非洲
4. **重点用户监控**: 已验证的活跃买家

## 部署指南

### 依赖安装
```bash
pip install requests python-dotenv
```

### 环境配置
创建 `.env` 文件：
```env
API_6551_KEY=your_6551_api_key
NOTIFICATION_EMAIL=your_email@example.com
DATABASE_URL=sqlite:///giftcard_monitor.db
```

### 运行系统
```bash
python giftcard_monitor.py
```

## 币安广场通用抓取工具

新增了一个可复用 CLI 工具：`binance_square_tool.py`

### 用法
```bash
# 自动发现帖子 ID（基于 Binance Square sitemap，无需手工 seed）
python3 binance_square_tool.py discover --limit 20

# 从第 3 页开始，最多扫描 5 页
python3 binance_square_tool.py discover --limit 50 --start-page 3 --max-pages 5

# 抓取单条帖子详情
python3 binance_square_tool.py detail 313380193156273

# 批量抓取并输出到 results/
python3 binance_square_tool.py scrape --limit 20

# 输出 jsonl / csv
python3 binance_square_tool.py scrape --limit 50 --format jsonl
python3 binance_square_tool.py scrape --limit 50 --format csv

# 按关键词和互动阈值过滤
python3 binance_square_tool.py scrape --limit 100 --keyword BTC --keyword ETH --min-likes 10 --min-comments 3
```

### 说明
- 当前采用两段式抓取
  - 先通过 `https://www.binance.com/en/square/sitemap/post/{page}` 自动发现 `content_id`
  - 再通过详情接口补全真实帖子数据
- 不再依赖手工 seed 或本地缓存文件
- 这样比直接撞推荐流接口更稳定，因为 feed/recommend 接口常见 403 或空数据
- 默认输出字段包括作者、正文、浏览/评论/点赞/分享、币对、标签、链接等

## 币安广场策略评分与回测脚手架

新增策略研究脚本：`binance_square_strategy.py`

### 功能
- 对广场帖子做信号评分
- 聚合同币种帖子生成 `long_watch` / `short_watch` / `no_trade`
- 对帖子做事件研究型回测
- 输出每个事件在未来 15m / 1h / 4h 的收益、最大顺行、最大逆行

### 用法
```bash
# 直接抓取并打分
python3 binance_square_strategy.py score --limit 50

# 对已抓到的帖子文件打分
python3 binance_square_strategy.py score --input results/binance_square_posts_xxx.json

# 对评分结果做回测
python3 binance_square_strategy.py backtest --input results/binance_square_scored_xxx.json

# 指定 5m K 线下的回测窗口，例如 3,12,48 根 K
python3 binance_square_strategy.py backtest --input results/binance_square_scored_xxx.json --interval 5m --windows 3,12,48
```

### 当前评分框架
- `engagement_score`：浏览、点赞、评论、分享、收藏的对数加权
- `specificity_score`：是否明确币对、是否给出 entry/tp/sl、是否包含数值
- `quality_score`：正文长度、标签、作者信息完整度
- `confidence_score`：结合 tendency 和文本关键词推断多空倾向与置信度

### 当前输出
- 帖子级别：`signal_score`、`signal_label`、`tone`、`symbol_candidates`
- 币种级别：`conviction_score`、`bias`、`post_count`、`unique_authors`
- 回测级别：未来窗口收益、最大顺行 MFE、最大逆行 MAE、胜率与均值统计

## 币安广场可持续样本研究框架

新增研究框架脚本：`binance_square_research.py`

### 设计目标
- 持续抓取并累计样本，而不是一次性分析
- 为每条帖子保存首见时间、末次见时间、评分结果、文本和快照
- 为每次采样保存互动快照，方便后续研究互动增速
- 把回测事件写入 SQLite，逐渐形成作者、币种、文本风格的长期统计库

### 数据存储
默认数据库：
```bash
results/binance_square_research.sqlite3
```

包含三类核心表：
- `posts`：帖子主档案
- `snapshots`：每次采样快照
- `backtest_events`：回测结果历史

### 用法
```bash
# 抓取最新样本并写入研究库
python3 binance_square_research.py ingest --limit 50

# 查看累计研究报告
python3 binance_square_research.py report --min-posts 2 --min-signal-score 55

# 对库里信号样本批量补回测并保存历史统计
python3 binance_square_research.py backfill --interval 5m --windows 3,12,48
```

### 这个框架能持续积累什么
- 作者维度
  - 发帖数量
  - 平均信号分
  - 高质量信号占比
  - 历史方向胜率
- 币种维度
  - 被提及频次
  - 多空倾向分布
  - 历史 conviction
- 帖子维度
  - 首见时间 / 末次见时间
  - 每次互动快照变化
- 回测维度
  - 不同 tone、窗口下的平均收益、胜率、MFE、MAE
- 时间桶维度
  - 5 分钟桶内同币种提及爆发
  - 作者去重数
  - 聚合 conviction
  - `long_watch / short_watch / no_trade`

### 新增研究能力
- `author_edge`
  - 基于历史回测结果统计作者方向命中率
  - 判断谁更像有效信号源，谁更像噪音源
- `time_bucket_signals`
  - 按 5 分钟桶聚合同币种帖子
  - 适合发现“同一时间窗口情绪突然集中到一个币”这种机会

### 推荐工作流
```bash
# 1. 定时采样，持续入库
python3 binance_square_research.py ingest --limit 50

# 2. 定期补回测
python3 binance_square_research.py backfill --interval 5m --windows 3,12,48

# 3. 输出研究报告
python3 binance_square_research.py report --min-posts 3 --min-signal-score 55

# 4. 看最近 24 小时 5 分钟聚合信号
python3 binance_square_research.py report --bucket-minutes 5 --lookback-hours 24 --bucket-min-posts 2
```

## 扩展功能

### 通知渠道集成
支持多种通知方式：
- 📧 邮件通知
- 📱 移动推送  
- 💬 即时消息（Telegram/Discord）
- 📊 Feishu 企业微信

### 数据分析报告
- 日报：新发现机会汇总
- 周报：市场趋势分析  
- 月报：ROI 和转化率统计

## 维护和优化

### 关键词优化
定期分析关键词效果，调整监控策略：
- 添加新兴平台关键词
- 优化地区搜索规则
- 更新高价值用户列表

### 性能监控
- API 调用频率限制
- 数据库性能优化
- 错误处理和重试机制

## 安全考虑
- API 密钥安全存储
- 数据隐私保护
- 访问权限控制

---

**注意**: 需要将6551 API集成到 `fetch_latest_tweets()` 方法中，并根据实际API文档调整数据结构。