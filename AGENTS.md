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
- **Data sources**: Web crawler (Sina → QQ → Eastmoney) with LLM extraction cache for dividends, never use simulated/hardcoded data
- **Cache**: `cache/data/` and `cache/analysis/`, 7-day retention, bypass after 15:55 if cached data not from today
- **Config**: `config/config.yaml`, environment variables in `config/.env` (gitignored)
- **Scheduler**: Run at 16:00 daily, cache bypass cutoff 15:55, timezone Asia/Shanghai

### Development Practices
- **No hardcoded data**: Solutions must work automatically for all stocks
- **Temporary files**: Max 2 temp files per function, clean up after use
- **Data validation**: Check dividend yields (0.5-20% reasonable range)
- **ETF handling**: ETFs (510880, 512810) return None appropriately
- **Unit conversion**: Handle cents to yuan, per-10-shares to per-share
- **Commit messages**: Use conventional prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`), include ticket/reference, English with Chinese context
- **Testing**: New features include unit tests, mock external APIs, use `conftest.py` for shared fixtures

## Agent Instructions
- Run tests before submitting changes (`pytest tests/validation/`)
- Follow existing patterns and conventions (ruff linting, 88 char lines, double quotes)
- Prioritize real data over simulated data
- Document significant changes in `proj4llm.md`
- Check for sensitive information before committing
- Use specialized agents defined in `.opencode/agents/` for specific tasks:
  - `data-source-validator`: Run real system to validate external data sources
  - `narrow-down-designer`: Analyze requirements and break down work
  - `checkpoint-acceptor`: Focus on table checking and sub-function acceptance
  - `cycle_guard`: Detect repetitive error patterns, prevent circular coding
  - `todosaver`: Save pending todos to docs/todo_backlog.md and clear context
  - `compaction`: Override built-in English compactor with Chinese version
  - `mail_checker`: Run system and validate latest email archive for data readiness and format compliance

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
| Timezone Bug | `src/data_fetcher.py:78-96` | ✅ Proper Asia/Shanghai timezone handling |
| Management Interface | `src/health_server.py` | ✅ OTP authentication, watchlist management |
| Schedule Optimization | `config/config.yaml` | ✅ Run time 16:00, bypass cutoff 15:55 |
| Akshare Removal | `announcement_fetcher.py` | ✅ Consolidated to web crawler + LLM cache |
| Dividend Architecture | `cache_manager.py`, `data_fetcher.py` | ✅ LLM extraction cache prioritized |

## Cursor/Copilot Rules
- No `.cursorrules` or `.cursor/rules/` files found
- No `.github/copilot-instructions.md` found
- No pre-commit hooks configured
- **Ruff configuration**: `pyproject.toml` (line-length 88, double quotes)
- **Flake8 configuration**: `.flake8` (max-line-length 88, ignore E203/W503)
- **YAPF configuration**: `.style.yapf` (pep8 style, column_limit 88)

**Last Updated**: 2026-04-02  
**Project Version**: v1.12+