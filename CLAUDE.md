# Kalshi Weather Trading Bot

## Project Rules
- Python 3.11+, async where appropriate
- Type hints on every function
- No file should exceed 300 lines — refactor if it grows past that
- All secrets via environment variables, never hardcoded
- Config lives in config.yaml
- Default to paper trading mode. Live trading requires explicit config flag AND CLI confirmation
- Every module gets unit tests
- Commit after each working milestone

## Style
- No unnecessary comments on obvious code
- Docstrings on public functions only
- Use logging module, not print statements
- Prefer explicit over clever

## Structure
- Refer to PLAN.md for architecture decisions
- Build one milestone at a time, test it, then move on