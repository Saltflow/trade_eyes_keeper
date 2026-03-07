# 股票量化系统 - 项目文档

## Abstract
A quantitative stock monitoring system that tracks selected Chinese A-shares, checks technical conditions (price < MA60), fetches official company announcements from authoritative sources (cninfo), analyzes fundamentals via LLM, and sends email alerts. The system prioritizes real data from multiple sources and includes a robust announcement system for corporate events like profit distribution, trading suspensions, and annual reports.

## 原始需求
写一个简单的手动量化系统，至少实现以下功能：
1. 输入自选股票代码，定时获取自选股票天级别交易数据（沪深，股票代码，开盘价，收盘价，最高价，最低价）
2. 满足以下所有条件时，发邮件到指定邮箱提醒：
   - 当天最低价 < MA60（前复权）
3. 获取自选股票公布的报告，并通过LLM API分析基本面、盈利情况，计算上一年分红情况，时间，股息率

**核心原则**：接口找不到就用爬虫，别模拟

---

## 系统架构与实现状态

### 系统架构
```
股票量化系统/
├── config/                 # 配置文件
│   ├── config.yaml        # 主配置文件
│   └── config.yaml.example
├── src/                   # 源代码
│   ├── data_fetcher.py    # 数据获取器（akshare + 网页爬虫）
│   ├── web_crawler.py     # 网页爬虫模块（新浪、腾讯、东方财富）
│   ├── condition_checker.py # 条件检查器
│   ├── email_notifier.py  # 邮件通知器
│   ├── llm_analyzer.py    # LLM基本面分析器
│   ├── scheduler_manager.py # 定时任务管理器
│   └── __init__.py
├── data/                  # 数据存储
├── logs/                  # 日志文件
├── main.py               # 主程序入口
├── run.bat               # Windows启动脚本
├── run.sh                # Linux启动脚本
├── requirements.txt      # Python依赖
└── README.md            # 系统文档
```

### 实现状态

#### ✅ 已完成功能
1. **数据获取系统**
   - 优先使用akshare API（因网络问题常失败）
   - 自动降级到网页爬虫获取真实数据
   - 当前使用新浪财经历史数据API获取真实历史价格
   - 支持股票：601728（中国电信）、600938（中国海油）
   - 数据字段：日期、开盘、收盘、最高、最低、成交量、成交额、振幅、涨跌幅、换手率
   - 自动计算MA60（60日移动平均线）

2. **条件检查系统**
   - 检查当天最低价 < MA60（前复权）
   - 计算价差和跌幅百分比
   - 生成详细的条件摘要

3. **邮件通知系统**
   - 使用yeah.net SMTP服务器（端口465，SSL）
   - 支持HTML格式邮件
   - 邮件内容包含：
     - 满足条件的股票列表
     - 所有监控股票状态表格
     - 价格对比和跌幅信息

4. **定时任务系统**
   - 基于APScheduler
   - 可配置运行时间（默认15:30，A股收盘后）
   - 支持单次运行模式（测试用）

5. **LLM分析系统**（可选）
   - 集成DeepSeek API
   - 分析股票基本面、盈利能力、分红情况
   - 结构化分析结果输出

6. **缓存系统改进**
   - 每日缓存机制：`--once`模式使用按天缓存，避免重复请求
   - 缓存验证：检查缓存数据完整性（价格、MA60、股息率等字段）
   - 文件名解析修复：使用`rsplit('_', 1)`正确处理带下划线的股票代码

7. **邮件可读性优化**
   - 表格拆分：将大型股票表格拆分为"价格技术指标"和"基本面指标"两个独立表格
   - 改进视觉设计：使用颜色区分正负值，突出提醒股票
   - 增强可读性：分离技术分析和基本面数据，便于快速扫描

8. **官方公司公告系统**
   - **核心数据源**：巨潮资讯网(cninfo)官方公司公告API
   - **公告类型**：利润分配、停牌、年度/中期/末期报告等重大事项
   - **真实数据**：获取真实公司公告（非新闻文章），包含有效cninfo链接
   - **股息详情**：集成详细分红数据提取（送股比例、派息比例、除权日等）
   - **智能处理**：支持30+种公告类型分类，自动提取重要公告
   - **稳健设计**：解决中文编码问题，支持5列/6列API响应格式
   - **ETF处理**：自动跳过ETF基金（使用不同公告系统）

#### ⚠️ 当前配置
- **股票监控**：11只股票（包括中国电信、中国海油、中国核电等，详见配置）
- **数据源**：新浪财经历史数据API（获取真实120天历史数据）
- **公告源**：巨潮资讯网(cninfo)官方公司公告API
- **邮件设置**：
  - 发件人：your_email@example.com（使用授权码）
  - 收件人：receiver_email@example.com
  - SMTP：smtp.yeah.net:465（SSL）
- **运行时间**：每天15:30（可配置）
- **日志系统**：文件和控制台双输出

#### 🔄 数据源优先级
**价格数据**：
1. akshare API（因网络问题常失败）
2. 新浪财经历史数据API（当前主要数据源）
3. 腾讯财经历史数据API（备用）
4. 东方财富API（备用）

**公司公告**：
1. 巨潮资讯网(cninfo)官方公告API（主要数据源）
2. 上海/深圳证券交易所API（备用）
3. 新浪财经公告页面（降级方案）

---

## 配置文件详解

### config/config.yaml
```yaml
# 公告配置（官方公司公告）
announcements:
  days: 7                    # 获取最近几天公告
  enable: true               # 启用官方公司公告抓取
  include_in_email: true     # 在邮件中包含公告（推荐）

# 数据源配置
data_source:
  type: akshare              # 优先使用akshare

# 邮件通知配置（敏感信息通过环境变量提供）
email:
  smtp_server: smtp.yeah.net
  smtp_port: 465             # SSL端口
  enable_ssl: true
  enable_tls: false
  # sender_email, sender_password, receiver_email 通过环境变量配置

# LLM API配置（敏感信息通过环境变量提供）
llm:
  api_type: deepseek
  base_url: https://api.deepseek.com/v1
  model: deepseek-chat
  # api_key 通过环境变量配置

# 日志配置
logging:
  file: ./logs/quant_system.log
  format: '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
  level: INFO

# 定时任务配置
scheduler:
  run_time: '15:30'          # 每天15:30运行
  timezone: Asia/Shanghai

# 股票监控列表
stocks:
  - 601728  # 中国电信
  # - 600938  # 中国海油（示例）

# 存储配置
storage:
  cache_days: 7              # 缓存保留天数
  cache_dir: ./cache         # 缓存目录
  csv_format: true           # 是否保存CSV文件
  data_dir: ./data           # 数据目录
```

### 环境变量（.env）
所有敏感信息已从配置文件中移除，必须通过环境变量提供。系统使用`python-dotenv`从`config/.env`文件加载环境变量。

**必需的环境变量**：
```bash
# 邮箱配置（用于发送提醒邮件）
EMAIL_SENDER=your_email@example.com
EMAIL_PASSWORD=your_email_password_or_app_specific_password
EMAIL_RECEIVER=receiver_email@example.com

# DeepSeek API配置（可选）
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# Tushare Token（如果使用tushare数据源）
TUSHARE_TOKEN=

# 日志级别
LOG_LEVEL=INFO
```

**配置优先级**：
1. 环境变量（最高优先级）
2. `.env`文件中的配置
3. `config.yaml`中的默认配置

**安全建议**：
- 将`.env`文件添加到`.gitignore`，避免提交敏感信息
- 使用邮箱授权码而非密码
- 定期轮换API密钥

---

## 使用说明

### 启动系统
```bash
# Windows
run.bat

# Linux/Mac
./run.sh

# 单次运行（测试）
python main.py --once
```

### 测试功能
系统使用`pytest`框架进行测试，测试文件已按功能组织：

```bash
# 运行所有单元测试
python -m pytest tests/unit/ -v

# 运行集成测试
python -m pytest tests/integration/ -v

# 运行API接口测试
python -m pytest tests/api/ -v

# 运行特定测试模块
python -m pytest tests/unit/test_cache_manager.py -v

# 运行单次系统测试（完整功能）
python main.py --once
```

**测试目录结构**：
```
tests/
├── unit/           # 单元测试（模块导入、缓存、邮件编码等）
├── integration/    # 集成测试（缓存、邮件表格、公告抓取等）
└── api/           # API集成测试（新浪财经、LLM等）
```

**临时测试脚本清理**：所有临时调试脚本（`debug_*.py`, `test_email_encoding_*.py`等）已删除或转换为正式测试。

### 日志查看
- 系统日志：`logs/quant_system.log`
- 测试日志：`logs/test.log`

---

## 技术实现细节

### 数据获取策略
1. **真实数据优先**：系统拒绝使用模拟数据，确保投资决策基于真实信息
2. **多源备份**：集成多个免费数据源，提高系统可靠性
3. **自动降级**：主数据源失败时自动切换到备用数据源
4. **历史数据**：获取至少120天历史数据用于MA60计算

### 邮件系统优化
1. **SSL连接**：使用端口465的SSL连接，解决yeah.net SMTP超时问题
2. **授权码认证**：支持邮箱授权码（非密码）认证
3. **HTML格式**：美观的HTML表格展示股票信息
4. **详细内容**：包含条件摘要和所有股票状态

### 条件检查算法
1. **前复权处理**：数据源提供前复权价格
2. **MA60计算**：使用pandas rolling函数计算60日移动平均
3. **实时检查**：只检查最新交易日的数据
4. **详细报告**：计算价差和百分比跌幅

---

## 问题与解决方案

### 已解决问题
1. **akshare连接失败**：使用网页爬虫替代
2. **yeah.net SMTP超时**：改用端口465 SSL连接
3. **历史数据模拟**：集成新浪财经真实历史数据API
4. **授权码认证**：更新邮件配置使用授权码

### 已知限制
1. akshare API在国内网络环境下可能不稳定
2. 免费数据源可能有频率限制
3. LLM分析需要额外的API成本
4. 仅支持A股市场

---

## 未来改进方向

### 短期改进
1. 增加更多数据源（网易财经、雪球等）
2. 添加更多技术指标（RSI、MACD等）
3. 支持更多股票和指数
4. 添加数据验证机制

### 长期规划
1. 添加回测功能
2. 支持自定义策略
3. Web管理界面
4. 移动端通知（微信、钉钉）
5. 多账户支持

---

## 维护与部署

### 日常维护
1. 定期检查数据源可用性
2. 监控邮件发送成功率
3. 更新API密钥（如需）
4. 查看系统日志

### 部署建议
1. 使用云服务器（确保网络稳定）
2. 配置cron任务定时运行
3. 使用虚拟环境隔离依赖
4. 定期备份配置和数据

---

## 版本历史
- **v1.0**：基础功能实现（数据获取、条件检查、邮件通知）
- **v1.1**：解决邮件发送问题（SSL连接、授权码认证）
- **v1.2**：改进历史数据准确性（集成新浪财经真实历史数据API）
- **v1.3**：添加LLM基本面分析功能
- **v1.4**：三项系统改进（2026-03-07）
   1. **缓存增强**：每日缓存机制，缓存数据完整性验证，文件名解析修复
   2. **邮件可读性**：表格拆分为价格技术指标和基本面指标，改进视觉设计
   3. **公告抓取**：集成上海/深圳交易所公告API，多源备用，邮件集成
   4. **安全改进**：敏感信息移出配置文件，完全依赖环境变量
   5. **测试优化**：清理临时脚本，重组测试目录，引入pytest框架
      - 删除15个临时调试脚本
      - 重组测试目录结构（unit/, integration/, api/)
      - 转换核心单元测试为pytest格式（6个单元测试）
      - 修复缓存和LLM编码测试中的模拟问题

- **v1.5**：邮件副本与公告增强（2026-03-07）
    1. **邮件副本存档**：每次发送邮件时自动保存HTML副本到`./data/email_archive/`
    2. **邮件变更追踪**：新增`compare_email_changes()`方法分析邮件内容变化
    3. **公告获取修复**：优化SSE/SZSE API错误处理，改进新浪财经备用数据源
    4. **公告邮件显示**：确保公告在邮件中正确显示（即使获取失败时显示友好提示）

- **v1.6**：官方公司公告系统升级（2026-03-07）
    1. **切换到官方公告源**：从新闻文章切换到**巨潮资讯网(cninfo)**获取真实公司公告
    2. **公告类型扩展**：支持分红、利润分配、停牌、年度报告等30+种官方公告类型
    3. **股息详情提取**：集成`stock_dividend_cninfo()`获取详细分红数据
    4. **编码问题修复**：解决中文列名编码问题，实现稳健的列映射和访问
    5. **ETF处理优化**：自动跳过ETF基金（使用不同公告系统），返回空列表
    6. **错误处理增强**：30天窗口回退，多列格式支持，移除模拟数据依赖
    7. **真实公告验证**：确认获取到真实公司公告（如"中国电信H股公告"、"中国海油增持公告"等）

**最后更新**：2026-03-07  
**状态**：稳定运行，公告系统已升级为官方公司公告源，所有功能正常运行