# Contributing

## Development Setup

```bash
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
# Linux: install system dependencies for PDF generation
sudo apt install -y texlive-xetex poppler-utils
```

## Running Tests

```bash
pytest tests/ -p no:capture -q
pytest tests/ --cov=src --cov-report=term
```

Individual test files:

```bash
pytest tests/test_rule_engine.py -v
pytest tests/test_portfolio_strategy.py -v
pytest tests/test_import_smoke.py -v
pytest tests/test_security.py -v  # expression sandbox and input security
```

## Code Style

- Line length: 88 characters
- Double quotes for strings
- Type hints required for function signatures
- Import order: standard library → third-party → local
- Run `ruff check src/` before committing

## Commit Convention

```
feat:     New feature
fix:      Bug fix
docs:     Documentation change
refactor: Code restructuring (no behavior change)
test:     Test additions or fixes
chore:    Build/config/maintenance
```

## Pull Request Process

1. Run `pytest tests/` and ensure all tests pass
2. Run `ruff check src/` and fix any issues
3. Update `docs/llm/proj4llm.md` if the design changes
4. Add tests for new functionality
5. Request review from a maintainer

## Project Structure

```
src/
  strategy/     TradingStrategy, TradePlan and strategy plugins
  search/       Solver plugins, evaluation, validation and promotion
  backtest/     Unified execution and portfolio simulation
  core/         RuntimeService, shared executor, single scheduler and reports
  data/         Web crawler, data source, cache manager
  alerting/     Multi-layer alert engine
  session/      Session manager
  models/       Pydantic data models
  notification/ Email notifier, chart generator
  interactive/  Feishu/Telegram, authenticated conversations and tools
  personal_monitor/ Accounts, reminders and delivery outbox
  fundamental_data/ Financial extraction, context and explicit handoffs
  utils/        CJK fonts, ETF detector
```

## Security

HTTP management, OTP and the old research execution modules have been retired.
See the [documentation index](docs/README.md) and
[architecture diagrams](docs/architecture_overview.md) for current entrypoints.

- Never commit API keys, passwords, or SSH keys
- Use `config/.env` for secrets (gitignored)
- Report security issues privately, not in public issues
