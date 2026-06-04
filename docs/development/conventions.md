# 开发规范

本文档汇总本项目的代码风格、开发实践与 Agent 工作流规范。

---

## 构建与测试命令

### 环境准备

```bash
python --version          # 3.8+
pip install -r requirements.txt
pip install pytest pytest-mock   # 开发依赖
```

### 运行系统

```bash
python main.py --once                # 单次运行
python main.py                       # 定时运行
./scripts/run.sh [--once]            # Linux/Mac
scripts\run.bat [--once]             # Windows
```

### 测试

```bash
pytest                               # 全部测试
pytest tests/validation/             # 验证测试
pytest -k "price"                    # 按名称过滤
pytest --cov=src tests/              # 覆盖率
pytest -v                            # 详细输出
```

### CI/CD

```bash
python ci_cd_deploy.py --dry-run     # 预览
python ci_cd_deploy.py               # 正式部署
python ci_cd_deploy.py --investigate # 排查
```

### 代码质量

```bash
ruff check .                         # 检查
ruff check --fix .                   # 自动修复
ruff format .                        # 格式化
python -m flake8 src/                # 传统 lint（行宽 88，忽略 E203/W503）
```

---

## 代码风格

### 导入顺序

1. 标准库（`import os`, `import logging`）
2. 第三方库（`import pandas as pd`, `import requests`）
3. 本地应用（`from .cache_manager import CacheManager`）

`src/` 内部使用相对导入，`main.py` 使用绝对导入。

### 命名规范

| 类型 | 规范 | 示例 |
|------|------|------|
| 类 | PascalCase | `StockDataFetcher` |
| 函数/方法 | snake_case | `fetch_stock_data` |
| 变量 | snake_case | `stock_code` |
| 常量 | UPPER_SNAKE_CASE | `MAX_CACHE_DAYS` |
| 私有成员 | 前导下划线 | `_fetch_from_web_crawler` |

### 类型提示

函数参数与返回值应使用类型提示：

```python
def fetch_stock_data(self) -> pd.DataFrame:
    ...
```

### 日志规范

```python
import logging
logger = logging.getLogger(__name__)

logger.debug("Detailed debug information")
logger.info("Normal operational messages")
logger.warning("Warning messages")
logger.error("Error conditions")
logger.critical("Critical conditions requiring immediate attention")
# 使用 f-strings 包含变量
logger.info(f"Stock {stock_code} cache bypassed")
```

### 行宽与格式

- 行宽上限 88 字符
- 字符串使用双引号
- 格式化工具：ruff（主要）/ yapf / flake8

---

## 项目约定

### 数据原则

- **真实数据优先**：Web crawler (Sina -> QQ -> Yahoo) + LLM 提取缓存，绝不使用模拟/硬编码数据
- **ETF 处理**：ETF（510880, 512810）返回 None，不参与股息计算
- **单位转换**：分->元、每10股->每股需统一处理
- **数据验证**：股息率合理范围 0.5%-20%，价格关系 close >= low <= high

### 缓存规则

- 路径：`cache/data/` + `cache/analysis/`，保留 7 天
- 交易日 15:55 后若缓存非当日则强制绕过
- 回测历史缓存：`cache/historical/`，保留 30 天

### 编码原则

- **禁止 try-catch 包装编码问题**：必须在源头修复（设置 `PYTHONUTF8=1`，使用 `policy.SMTPUTF8` 等）
- 所有文件操作显式指定 `encoding='utf-8'`

### 依赖管理

- 谨慎添加第三方依赖，移除后不应重新添加
- 重大依赖变更前必须更新 `docs/llm/design_decisions.md`

### 提交消息

使用约定式前缀：

```
feat: 添加新功能
fix: 修复缺陷
docs: 文档变更
refactor: 重构（不改变功能）
test: 测试相关
```

---

## Agent 工作流

项目定义了多个专用 Agent（见 `.opencode/agents/`）：

| Agent | 用途 |
|-------|------|
| `data-source-validator` | 运行真实系统验证外部数据源 |
| `narrow-down-designer` | 需求分析与工作分解 |
| `checkpoint-acceptor` | 表格检查与子功能验收 |
| `cycle_guard` | 检测重复错误模式，防止循环编码 |
| `todosaver` | 保存待办到 `docs/development/todo_backlog.md` |
| `mail_checker` | 验证最新邮件存档数据与格式 |
| `net-checker` | SSH 远程检查健康服务器状态 |

### 关键规则

- 提交变更前运行 `pytest tests/validation/`
- 所有设计决策存档到 `docs/llm/design_decisions.md`
- 提交前检查是否包含敏感信息
- 优先遵循已有模式，而非引入新范式

---

**相关文档**：
- [开发日志](devlog.md)
- [编码审计日志](audit_log.md)
- [架构说明](../architecture.md)
