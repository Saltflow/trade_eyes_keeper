# 股票量化系统

一个简单的股票量化系统，用于监控A股股票，在满足条件时发送邮件提醒。

## 功能特性

1. **股票数据获取**：定时获取自选股票的日级别交易数据（开盘价、收盘价、最高价、最低价）
2. **条件检测**：检查当天最低价 < MA60（前复权）条件
3. **邮件提醒**：满足条件时自动发送邮件到指定邮箱
4. **基本面分析**：通过LLM API分析股票基本面、盈利情况和分红情况（可选）
5. **跨平台支持**：支持Windows和Linux系统

## 系统架构

```
├── config/           # 配置文件
│   ├── config.yaml        # 主配置文件
│   ├── .env.example       # 环境变量示例
│   └── .env              # 环境变量（实际使用）
├── data/             # 股票数据存储目录
├── docs/             # 文档文件
│   ├── proj_compressed.md    # 压缩版项目文档
│   └── proj_short.md         # 简短版项目文档
├── logs/             # 日志文件目录
├── scripts/          # 启动脚本
│   ├── run.bat       # Windows启动脚本
│   └── run.sh        # Linux启动脚本
├── src/              # 源代码
│   ├── __init__.py
│   ├── data_fetcher.py    # 数据获取模块（优先akshare，失败时网页爬虫）
│   ├── web_crawler.py     # 网页爬虫模块（从公开网站获取真实数据）
│   ├── condition_checker.py # 条件检查模块
│   ├── email_notifier.py  # 邮件通知模块
│   ├── llm_analyzer.py    # LLM分析模块
│   └── scheduler_manager.py # 定时任务管理
├── tests/            # 测试文件
│   ├── unit/         # 单元测试
│   └── api/          # API接口测试
├── main.py           # 主程序入口
├── proj.md           # 完整项目文档
├── proj4llm.md       # LLM分析模块详细文档
├── requirements.txt  # Python依赖包
├── .gitignore        # Git忽略文件
└── README.md         # 说明文档
```

## 快速开始

### 1. 环境准备

- Python 3.8+
- 安装依赖包：

```bash
pip install -r requirements.txt
```

### 2. 配置系统

#### 2.1 复制环境变量文件

```bash
cp .env.example .env
```

编辑`.env`文件，填写以下信息：

```
# 邮箱配置（用于发送提醒邮件）
EMAIL_SENDER=your_email@example.com
EMAIL_PASSWORD=your_email_password_or_app_specific_password
EMAIL_RECEIVER=receiver_email@example.com

# DeepSeek API配置（可选）
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# Tushare Token（如果使用tushare数据源）
TUSHARE_TOKEN=your_tushare_token_here

# 日志级别
LOG_LEVEL=INFO
```

#### 2.2 编辑主配置文件

编辑`config/config.yaml`，设置股票代码和其他参数：

```yaml
# 股票配置
stocks:
  - 601728  # 中国电信
  - 600938  # 中国海油

# 数据源配置
data_source:
  type: akshare  # 使用akshare获取数据，可选：akshare, tushare
  # 注意：系统不再支持模拟数据模式，确保使用真实数据进行投资决策

# 邮件通知配置
email:
  smtp_server: smtp.yeah.net  # yeah.net邮箱SMTP服务器
  smtp_port: 465  # 使用SSL的端口（yeah.net支持465，587可能超时）
  sender_email: your_email@example.com  # 发送邮箱
  sender_password: your_email_password_or_app_specific_password  # 邮箱授权码，建议使用环境变量
  receiver_email: receiver_email@example.com  # 接收邮箱
  enable_ssl: true  # yeah.net使用SSL连接，不是TLS
  enable_tls: false

# 系统运行配置
scheduler:
  run_time: "15:30"  # 每天15:30运行（收盘后）
  timezone: "Asia/Shanghai"
```

### 3. 运行测试

```bash
# 运行基本功能测试
cd tests/unit && python test_basic.py

# 测试网页爬虫
cd tests/unit && python test_crawler.py

# 测试数据获取
python -c "from src.data_fetcher import StockDataFetcher; import yaml; config=yaml.safe_load(open('config/config.yaml')); fetcher=StockDataFetcher(config); print(fetcher.fetch_stock_data())"

# 测试邮件发送（不实际发送）
python -c "from src.email_notifier import EmailNotifier; import yaml; config=yaml.safe_load(open('config/config.yaml')); notifier=EmailNotifier(config); print(notifier.send_test_email())"
```

### 4. 运行系统

#### 4.1 使用启动脚本（推荐）

**Windows系统：**
```bash
cd scripts && run.bat --once  # 单次运行
cd scripts && run.bat         # 定时运行
```

**Linux系统：**
```bash
cd scripts && chmod +x run.sh
./run.sh --once  # 单次运行
./run.sh         # 定时运行
```

#### 4.2 直接运行

**单次运行模式：**
```bash
python main.py --once
```

**定时运行模式：**
```bash
python main.py
```

系统将在每天15:30（收盘后）自动运行，获取数据并检查条件。

### 5. 查看日志

日志文件位于`logs/`目录：

```bash
tail -f logs/quant_system.log
```

## 配置说明

### 股票代码

在`config/config.yaml`的`stocks`部分添加或修改股票代码：

```yaml
stocks:
  - 601728  # 中国电信
  - 600938  # 中国海油
  - 000001  # 平安银行
```

支持上海证券交易所（6开头）和深圳证券交易所（0、3开头）的股票。

### 数据源配置

系统支持多种数据源：

1. **akshare**：免费数据源，无需注册（默认）
2. **tushare**：需要注册获取token，数据更稳定
3. **mock**：模拟数据，用于测试或网络不可用时

配置示例：

```yaml
data_source:
  type: akshare  # 或 tushare, mock
  enable_mock: true  # 当真实数据获取失败时使用模拟数据
  tushare_token: "your_tushare_token_here"  # 使用tushare时需要
```

### 邮件配置

支持SMTP协议的邮箱服务，如163、qq、yeah.net等：

```yaml
email:
  smtp_server: smtp.yeah.net
  smtp_port: 465  # 使用SSL端口
  sender_email: your_email@yeah.net
  sender_password: your_authorization_code  # 使用授权码，不是密码
  receiver_email: receiver@email.com
  enable_ssl: true  # yeah.net使用SSL
  enable_tls: false
```

**注意**：部分邮箱需要使用授权码而非登录密码。

### 定时任务配置

```yaml
scheduler:
  run_time: "15:30"  # 运行时间（24小时制）
  timezone: "Asia/Shanghai"  # 时区
  run_on_startup: false  # 启动时立即运行一次
```

## 高级功能

### LLM基本面分析

系统支持使用DeepSeek API进行股票基本面分析：

1. 在DeepSeek官网注册并获取API密钥
2. 在`.env`文件中设置`DEEPSEEK_API_KEY`
3. 系统将在每日任务中自动分析股票基本面

### 自定义条件

如需修改检测条件，编辑`src/condition_checker.py`中的`check_condition`方法。

### 数据存储

股票历史数据以CSV格式存储在`data/`目录，文件名为`{股票代码}_history.csv`。

## 故障排除

### 1. 数据获取失败

- 检查网络连接
- 确认akshare或tushare服务可用
- 启用模拟模式（设置`enable_mock: true`）

### 2. 邮件发送失败

- 检查邮箱配置（服务器、端口、用户名、密码）
- 确认邮箱已开启SMTP服务
- 检查防火墙设置

### 3. 程序运行错误

- 查看日志文件`logs/quant_system.log`
- 确认Python版本和依赖包已正确安装
- 检查配置文件格式（YAML语法）

## 系统要求

- Python 3.8+
- 内存：至少512MB
- 磁盘空间：至少100MB
- 网络连接（用于获取股票数据和发送邮件）

## 许可证

本项目仅供学习和研究使用，请勿用于商业用途。

## 贡献

欢迎提交Issue和Pull Request。

## 联系方式

如有问题，请通过邮件联系或提交GitHub Issue。