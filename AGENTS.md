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

# Test structure:
# - tests/unit/       # Unit tests
# - tests/integration/# Integration tests  
# - tests/api/        # API tests
```

### Code Quality
```bash
# No specific linter configured, but follow these practices:
# - Ensure no syntax errors
# - Run tests before committing
# - Check for unused imports
```

## Code Style Guidelines

### Imports Order
```python
# 1. Standard library imports
import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

# 2. Third-party imports
import pandas as pd
import numpy as np
import akshare as ak
import yaml

# 3. Local application imports
from .cache_manager import CacheManager
from .web_crawler import StockWebCrawler

# Use relative imports within src/ (e.g., from .module import Class)
# Use absolute imports from main.py (e.g., from src.module import Class)
```

### Naming Conventions
- **Classes**: PascalCase (e.g., `StockDataFetcher`, `EmailNotifier`)
- **Functions/Methods**: snake_case (e.g., `fetch_stock_data`, `_should_bypass_cache`)
- **Variables**: snake_case (e.g., `stock_code`, `dividend_per_share`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_CACHE_DAYS`)
- **Private members**: Leading underscore (e.g., `_fetch_from_akshare`)

### Type Hints
```python
def fetch_stock_data(self) -> pd.DataFrame:
    """
    Get stock data.
    
    Returns:
        pandas.DataFrame: Stock data with required columns
    """
```

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

### Documentation
```python
def method_name(self, param1: str, param2: int) -> pd.DataFrame:
    """
    Brief description of method.
    
    Args:
        param1: Description of first parameter
        param2: Description of second parameter
        
    Returns:
        Description of return value
        
    Raises:
        ValueError: When invalid input provided
    """
```

## Project-Specific Conventions

### Data Source Priority
1. **Primary**: `akshare` API for stock data and dividends
2. **Fallback**: Web crawler (Sina → QQ → Eastmoney) when akshare fails
3. **Never use**: Simulated or hardcoded data for investment decisions

### Cache Management
- Cache location: `cache/data/` and `cache/analysis/`
- Default retention: 7 days (configurable)
- Cache bypass logic: After 15:05 daily, use fresh data if cached data isn't from today
- Cache files: `{stock_code}_{YYYYMMDD}.json`

### Configuration
- Main config: `config/config.yaml`
- Environment variables: `config/.env` (gitignored, use `.env.example` as template)
- Never commit secrets or API keys

**Important scheduler settings**:
```yaml
scheduler:
  run_time: '15:30'  # Daily execution time
  cache_bypass_cutoff: '15:05'  # Time after which stale cache is bypassed
  timezone: Asia/Shanghai
```

### File Structure
```
src/
├── data_fetcher.py      # Stock data fetching with fallback
├── web_crawler.py       # Web scraping for real-time data
├── condition_checker.py # MA60 condition checking
├── email_notifier.py    # Email notification with HTML tables
├── llm_analyzer.py      # DeepSeek API integration
├── announcement_fetcher.py # Company announcement fetching
├── cache_manager.py     # Cache management
├── scheduler_manager.py # Task scheduling
└── content_fetcher.py   # Announcement content extraction
```

### Development Rules (from proj4llm.md)
- **No hardcoded data**: Solutions must work automatically for all stocks
- **Temporary files**: Max 2 temp files per function, clean up after use
- **Data validation**: Check dividend yields (0.5-20% reasonable range)
- **ETF handling**: ETFs (510880, 512810) return None appropriately
- **Unit conversion**: Handle cents to yuan, per-10-shares to per-share

### Commit Messages
- Use conventional prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`
- Include ticket/reference if applicable
- Write in English with Chinese context when needed
- Example: `feat: Add cache bypass logic for data freshness`

### Testing Requirements
- New features should include unit tests
- Integration tests for data fetching and email sending
- Mock external APIs (akshare, email) in unit tests
- Use `conftest.py` for shared fixtures

## Agent Instructions
- Always run tests before submitting changes
- Follow existing patterns and conventions
- Prioritize real data over simulated data
- Document significant changes in `proj4llm.md`
- Check for sensitive information before committing
- Use descriptive commit messages explaining the "why"

## Troubleshooting
- **Import errors**: Ensure `src/` is in Python path (see `conftest.py`)
- **Chinese encoding**: Set `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`
- **Akshare failures**: System should automatically fall back to web crawler
- **Email sending**: Set `SKIP_EMAIL=true` env var for testing
- **Cache issues**: Delete `cache/` directory to force fresh data fetch

**Last Updated**: 2026-03-09  
**Project Version**: v1.9+