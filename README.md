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
# 发现首页帖子 ID
python3 binance_square_tool.py discover --limit 20

# 抓取单条帖子详情
python3 binance_square_tool.py detail 312683736169105

# 批量抓取并输出到 results/
python3 binance_square_tool.py scrape --limit 20

# 输出 jsonl
python3 binance_square_tool.py scrape --limit 50 --format jsonl
```

### 说明
- 当前采用两段式抓取
  - 先通过浏览器页面发现 `content_id`
  - 再通过详情接口补全真实帖子数据
- 这样比直接撞推荐流接口更稳定
- 默认输出字段包括作者、正文、浏览/评论/点赞/分享、币对、链接等

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