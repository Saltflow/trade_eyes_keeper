# Agent Guidelines for Stock Quantitative System

Reasoning Effort: Absolute maximum with no shortcuts permitted.
You MUST be very thorough in your thinking and comprehensively decompose the
problem to resolve the root cause, rigorously stress-testing your logic against all potential
paths, edge cases, and adversarial scenarios.
Explicitly write out your entire deliberation process, documenting every intermediate
step, considered alternative, and rejected hypothesis to ensure absolutely no assumption
is left unchecked.

This document provides guidelines for AI agents working on this repository. It covers build commands, testing, code style, and project-specific conventions.

## Build and Test Commands

### Environment Setup
```bash
# Python 3.8+ required
python --version

# Install dependencies
pip install -r requirements.txt

# For development (additional tools)
pip install pytest pytest-mock
```

### Running the System
```bash
# Single run (for testing)
python main.py --once

# Brief report (morning snapshot, only price + anchor data)
python main.py --brief [report_id]

# Scheduled run (default)
python main.py

# Strategy optimizer (Bayesian search)
python main.py --optimize

# Using scripts (cross-platform)
./scripts/run.sh [--once]      # Linux/Mac
scripts/run.bat [--once]       # Windows
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/validation/test_system_validation.py

# Run specific test function
pytest tests/validation/test_system_validation.py::TestPriceRelationshipValidation::test_valid_price_data_no_warnings -v

# Run tests matching a pattern
pytest -k "price"  # Run tests with "price" in name

# Run tests in a specific directory
pytest tests/validation/

# Run with coverage
pytest --cov=src tests/

# Run with verbose output
pytest -v
```

### CI/CD Deployment
```bash
# Deploy with dry-run (no actual changes)
python ci_cd_deploy.py --dry-run

# Deploy to production
python ci_cd_deploy.py

# Investigate server health
python ci_cd_deploy.py --investigate

# Use custom SSH port
python ci_cd_deploy.py --ssh-port 2222
```

### Code Quality
```bash
# Lint with ruff (configured for 88 char lines, double quotes)
ruff check .            # Check for lint issues
ruff check --fix .      # Auto-fix fixable issues
ruff format .           # Format code

# Legacy linting (flake8)
python -m flake8 src/   # Line length 88, ignore E203/W503
```

## Code Style Guidelines

### Imports Order
1. **Standard library imports** (e.g., `import os`, `import logging`)
2. **Third-party imports** (e.g., `import pandas as pd`, `import requests`)
3. **Local application imports** (e.g., `from .cache_manager import CacheManager`)

Use relative imports within `src/`, absolute imports from `main.py`.

### Naming Conventions
- **Classes**: PascalCase (e.g., `StockDataFetcher`, `EmailNotifier`)
- **Functions/Methods**: snake_case (e.g., `fetch_stock_data`, `_should_bypass_cache`)
- **Variables**: snake_case (e.g., `stock_code`, `dividend_per_share`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_CACHE_DAYS`)
- **Private members**: Leading underscore (e.g., `_fetch_from_web_crawler`)

### Type Hints
Use type hints for function parameters and return values (e.g., `def fetch_stock_data(self) -> pd.DataFrame:`).

### Error Handling
```python
try:
    # Operation that may fail
    data = fetch_from_api(symbol)
except Exception as e:
    logger.error(f"Failed to fetch stock {stock_code} data: {e}")
    # Fallback to alternative data source
    return self._fetch_from_web_crawler(stock_code)
```

### Logging
```python
import logging
logger = logging.getLogger(__name__)

# Use appropriate levels
logger.debug("Detailed debug information")
logger.info("Normal operational messages")
logger.warning("Warning messages")
logger.error("Error conditions")
logger.critical("Critical conditions requiring immediate attention")

# Use f-strings for variable inclusion
logger.info(f"Stock {stock_code} cache bypassed, current time {now.strftime('%H:%M')}")
```

## Project-Specific Conventions

### Data and Configuration
- **Data sources**: Web crawler (Sina → QQ → Yahoo) with LLM extraction cache for dividends, never use simulated/hardcoded data
- **Cache**: `cache/data/` and `cache/analysis/`, 7-day retention, bypass after 15:55 if cached data not from today
- **Config**: `config/config.yaml`, environment variables in `config/.env` (gitignored)
- **Scheduler**: Run at 19:00 daily, cache bypass cutoff 15:55, timezone Asia/Shanghai
- **Brief reports**: Morning snapshot 09:50 + Afternoon snapshot 14:30, both skip weekends

### Development Practices
- **No hardcoded data**: Solutions must work automatically for all stocks
- **Temporary files**: Max 2 temp files per function, clean up after use
- **Data validation**: Check dividend yields (0.5-20% reasonable range)
- **ETF handling**: ETFs (510880, 512810) return None appropriately
- **Unit conversion**: Handle cents to yuan, per-10-shares to per-share
- **Commit messages**: Use conventional prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`), include ticket/reference, English with Chinese context
- **Testing**: New features include unit tests, mock external APIs, use `conftest.py` for shared fixtures
- **LaTeX 模板**: 修改 `report_daily.tex` 后，确保文件不含 `\r\n` (xelatex 将 `\r` 视为 `^^M`, 导致 Emergency stop)。代码在编译前自动执行 `.replace("\r\n","\n")`。`_` 必须转义为 `\_`, `%` 为 `\%`, `&` 为 `\&`, `$` 为 `\$`。使用 `_esc()` 辅助函数。
- **xelatex 测试**: `python main.py --once` 自动编译 PDF, 编译警告记录在日志中。服务器需安装 `texlive-xetex`。
- **全量验证**: 策略优化/信号扫描/回测分析的 HTML 报告和邮件内容必须基于 `config/config.yaml` 全量标的运行产出。禁止用 1-3 只股票的子集跑 `--optimize` 或生成用于验证的 HTML 报告。`python main.py --optimize` 全量运行耗时 ~30min，跑完后产出方为有效测试数据。

## Agent Instructions
- Run tests before submitting changes (`pytest tests/validation/`)
- Follow existing patterns and conventions (ruff linting, 88 char lines, double quotes)
- Prioritize real data over simulated data
- Document significant changes in `docs/llm/proj4llm.md` (moved from root `proj4llm.md`)
- Check for sensitive information before committing
- Use specialized agents defined in `.opencode/agents/` for specific tasks:
  - `data-source-validator`: Run real system to validate external data sources
  - `narrow-down-designer`: Analyze requirements and break down work
  - `checkpoint-acceptor`: Focus on table checking and sub-function acceptance
  - `cycle_guard`: Detect repetitive error patterns, prevent circular coding
  - `todosaver`: Save pending todos to `docs/development/todo_backlog.md` and clear context
  - `mail_checker`: Run system and validate latest email archive for data readiness and format compliance
  - `net-checker`: SSH to remote server, check health-server status and verify endpoint compliance

## Troubleshooting
- **Import errors**: Ensure `src/` is in Python path (see `conftest.py`)
- **Chinese encoding**: Set `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`
- **Web crawler failures**: System automatically falls back to alternative data sources
- **Email sending**: Set `SKIP_EMAIL=true` env var for testing
- **Cache issues**: Delete `cache/` directory to force fresh data fetch
- **Pytest capture errors**: Check pytest configuration in `pytest.ini`

## Recent Code Review Findings (Summary)

| Issue | Fix Location | Status |
|-------|-------------|--------|
| Health Server Security | `src/health_server.py` | ✅ HTML escaping, rate limiting, HTTPS, IP validation |
| ROE Inconsistency | `src/web_crawler.py:563-578` | ✅ Added validation (5% threshold) |
| Price Validation | `src/condition_checker.py:46-58` | ✅ Checks close≥low≤high, logs warnings |
| Cache Oversharing | `src/data/data_source.py:96,121,130,133,149` | ✅ 5 return paths trimmed to requested days |
| Cache Bypass Regression | `src/data/data_source.py` | ✅ Restored `_should_bypass_cache` with per-stock granularity |
| Debt Ratio Removal | `src/models/schemas.py` + `data_fetcher.py` + `web_crawler.py` | ✅ Full-stack deletion, `items[52/53]` were mis-mapped PEs |
| ROE Calculation | `src/core/data_fetcher.py` | ✅ Derived from `PB/PE × 100`, <0.2% error vs financial reports |
| Brief Report Sorting | `src/notification/email_notifier.py` | ✅ Ascending by anchor deviation, larger drops first |
| Afternoon Brief Report | `config/config.yaml` + `ci_cd_deploy.py` | ✅ Added `afternoon_snapshot` at 14:30 |
| Daily Run Time | `config/config.yaml` + `ci_cd_deploy.py` | ✅ Changed from 16:00 to 19:00 |
| CJK Font (Windows) | `src/chart_generator.py` → `_setup_cjk_font()` | ✅ Unified platform-aware font setup |
| Date Alignment | `src/portfolio_strategy.py:evaluate()` | ✅ Real-date alignment instead of index-based |
| Dividend Architecture | `cache_manager.py`, `data_fetcher.py` | ✅ LLM extraction cache prioritized |
| Brief Report Trading Day | `src/email_notifier.py:send_brief_report()` | ✅ 3-day window + weekend skip |
| Rule Engine Extensibility | `src/rule_engine.py` | ✅ YAML-driven config, no code change needed |
| QQ Real-time Quote | `src/data/web_crawler.py` | ✅ `fetch_realtime_quote()` for intraday brief refresh |
| Eastmoney Removal | `src/data/web_crawler.py` + `data_source.py` | ✅ Removed from all 4 fallback chains |
| Optimizer P0 Crash | `src/analysis/strategy_optimizer.py` | ✅ `best_params: dict` type relaxed |
| Bollinger Column Name | `src/core/technical_indicators.py` | ✅ `boll_pb` → `boll_pct_b` unified |
| Data Source Health Probe | `tests/test_data_source_health.py` | ✅ 14 smoke tests for A/HK/ETF data |

## Cursor/Copilot Rules
- No `.cursorrules` or `.cursor/rules/` files found
- No `.github/copilot-instructions.md` found
- No pre-commit hooks configured
- **Ruff configuration**: `pyproject.toml` (line-length 88, double quotes)
- **Flake8 configuration**: `.flake8` (max-line-length 88, ignore E203/W503)
- **YAPF configuration**: `.style.yapf` (pep8 style, column_limit 88)

**Last Updated**: 2026-06-04  
**Project Version**: v1.17.1 (简报增强 + 数据清理 + 调度调整 + QQ 实时行情 + 优化器 P0 修复)