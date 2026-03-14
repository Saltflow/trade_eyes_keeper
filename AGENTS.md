# Agent Guidelines for Stock Quantitative System

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

# Scheduled run (default)
python main.py

# Using scripts (cross-platform)
./scripts/run.sh [--once]      # Linux/Mac
scripts/run.bat [--once]       # Windows
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_module_imports.py

# Run specific test function
pytest tests/unit/test_module_imports.py::test_imports -v

# Run tests matching a pattern
pytest -k "import"  # Run tests with "import" in name
pytest -k "test_email"  # Run email-related tests

# Run tests in a specific directory
pytest tests/unit/
pytest tests/integration/

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
# No specific linter configured, but follow these practices:
# - Ensure no syntax errors
# - Run tests before committing
# - Check for unused imports
# - Consider using black/isort for formatting (optional)
```

## Code Style Guidelines

### Imports Order
1. **Standard library imports** (e.g., `import os`, `import logging`)
2. **Third-party imports** (e.g., `import pandas as pd`, `import akshare as ak`)
3. **Local application imports** (e.g., `from .cache_manager import CacheManager`)

Use relative imports within `src/`, absolute imports from `main.py`.

### Naming Conventions
- **Classes**: PascalCase (e.g., `StockDataFetcher`, `EmailNotifier`)
- **Functions/Methods**: snake_case (e.g., `fetch_stock_data`, `_should_bypass_cache`)
- **Variables**: snake_case (e.g., `stock_code`, `dividend_per_share`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_CACHE_DAYS`)
- **Private members**: Leading underscore (e.g., `_fetch_from_akshare`)

### Type Hints
Use type hints for function parameters and return values (e.g., `def fetch_stock_data(self) -> pd.DataFrame:`).

### Error Handling
```python
try:
    # Operation that may fail
    data = ak.stock_zh_a_hist(symbol=symbol, period="daily")
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
- **Data sources**: `akshare` API (primary), web crawler fallback (Sina → QQ → Eastmoney), never use simulated/hardcoded data
- **Cache**: `cache/data/` and `cache/analysis/`, 7-day retention, bypass after 15:05 if cached data not from today
- **Config**: `config/config.yaml`, environment variables in `config/.env` (gitignored)
- **Scheduler**: Run at 15:30 daily, cache bypass cutoff 15:05, timezone Asia/Shanghai



### Development Practices
- **No hardcoded data**: Solutions must work automatically for all stocks
- **Temporary files**: Max 2 temp files per function, clean up after use
- **Data validation**: Check dividend yields (0.5-20% reasonable range)
- **ETF handling**: ETFs (510880, 512810) return None appropriately
- **Unit conversion**: Handle cents to yuan, per-10-shares to per-share
- **Commit messages**: Use conventional prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`), include ticket/reference, English with Chinese context
- **Testing**: New features include unit tests, mock external APIs, use `conftest.py` for shared fixtures

## Agent Instructions
- Run tests before submitting changes
- Follow existing patterns and conventions
- Prioritize real data over simulated data
- Document significant changes in `proj4llm.md`
- Check for sensitive information before committing

## Troubleshooting
- **Import errors**: Ensure `src/` is in Python path (see `conftest.py`)
- **Chinese encoding**: Set `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`
- **Akshare failures**: System should automatically fall back to web crawler
- **Email sending**: Set `SKIP_EMAIL=true` env var for testing
- **Cache issues**: Delete `cache/` directory to force fresh data fetch
- **Pytest capture errors**: Check pytest configuration if tests fail with capture issues

## Recent Code Review Findings (2026-03-14)
**Health Server Security Improvements**: `src/health_server.py` - Added security measures for internet-facing health server on port 1933:
1. **HTML Injection Protection**: All dynamic content in HTML responses is now escaped using `html.escape()` (`src/health_server.py:141-145`).
2. **Rate Limiting**: 1 QPS (60 requests/minute) per IP address with automatic blocking (`src/health_server.py:23-85`).
3. **Secure Query Parsing**: Uses `urllib.parse.parse_qs()` instead of string matching for query parameters (`src/health_server.py:359-370`).
4. **HTTPS for External IP**: Changed `http://ifconfig.me` to `https://ifconfig.me` to prevent MITM attacks (`src/health_server.py:664`).
5. **IP Format Validation**: Basic regex validation for public IP responses (`src/health_server.py:667-671`).
6. **Security Status Display**: Health page now shows security status and rate limit statistics (`src/health_server.py:147-282`).

**ROE Inconsistency Fix**: `src/web_crawler.py:563-578` - Added consistency validation for ROE values from QQ data source. Uses calculated ROE (PB/PE) when discrepancy exceeds 5%.

**Price Validation Added**: Added price relationship validation in `src/data_fetcher.py:164-180` and `src/condition_checker.py:46-58` to detect anomalies where close < low, close > high, or low > high. Logs warnings for investigation.

**Timezone Bug Fixed**: `src/data_fetcher.py:78-82` - Fixed mixing timezone-aware `now` with naive `cached_date` by converting cached date to local timezone before comparison.

**Bug Investigation**: User reported "end-day price (close) of each stock is reported as incorrect compared to the lowest price (low)". Investigation found:
- All stored CSV files show correct price relationships (close ≥ low, close ≤ high)
- Email archive outputs show correct prices
- Data sources (Sina, QQ, Eastmoney) return consistent data
- Added validation to catch any future data anomalies
- **Deployed fixes** to production server (DEPLOY_HOST) with SSH key authentication. Price validation and timezone fixes active.

**Root Cause Identified**: System runs at 15:30 but data source may not have updated today's data yet, causing yesterday's prices to be used. Fixed by:
1. **Schedule adjustment**: Changed `run_time` from 15:30 to 16:00 and `cache_bypass_cutoff` from 15:05 to 15:55 in `config/config.yaml`.
2. **Date validation**: Added check in `src/data_fetcher.py:162-168` to log warning if fetched data date is not today.
3. **Timezone fix**: Enhanced `_should_bypass_cache` to properly compare dates in Asia/Shanghai timezone (`src/data_fetcher.py:73-96`).
4. **Server deployment**: Updated server with all fixes; verified that system now fetches today's data (2026-03-13).

**SSH Logic Duplication**: `ci_cd_deploy.py:385-600` - `investigate_server()` duplicates SSH connection/authentication code from `deploy()`. Refactor common SSH logic into shared functions.

**Unnecessary 60-Second Wait**: Hardcoded 60-second wait on every deployment/investigation, even in dry-run mode. Consider making wait time configurable or only apply when actually hitting rate limits.

**Redundant Exception Handling**: Exception blocks that only re-raise add no value. Remove or add meaningful error handling/logging.

**Removed DSA Key Support**: DSA SSH key support removed; could break deployments using DSA keys. Document as breaking change or restore DSA support.

**Pytest Environment Issues**: Pytest capture errors preventing test execution (environment issue). Investigate pytest configuration/capture plugin conflicts.

## Cursor/Copilot Rules
- No `.cursorrules` or `.cursor/rules/` files found
- No `.github/copilot-instructions.md` found
- No pre-commit hooks configured

**Last Updated**: 2026-03-14  
**Project Version**: v1.10+