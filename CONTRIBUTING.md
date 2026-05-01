# Contributing

## Development Setup

```bash
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
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
  analysis/     Strategy optimizer, signal scanner, indicators
  health_server/ HTTP health check and management server
  core/         Data fetcher, condition checker, scheduler
  data/         Web crawler, data source, cache manager
  alerting/     Multi-layer alert engine
  session/      Session manager
  models/       Pydantic data models
  notification/ Email notifier, chart generator
  utils/        CJK fonts, ETF detector
```

## Security

- Never commit API keys, passwords, or SSH keys
- Use `config/.env` for secrets (gitignored)
- Report security issues privately, not in public issues
