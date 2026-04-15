# kalshi-weather-bot

Systematic trading bot for Kalshi weather markets. Converts ensemble weather
forecasts (Open-Meteo + NWS) into probability distributions, compares them to
Kalshi market-implied probabilities, and places fee-aware trades when the
expected-value edge clears a configurable threshold.

See [PLAN.md](./PLAN.md) for architecture.

## Setup

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in Kalshi demo credentials at minimum
```

## Run

```sh
python -m kalshi_weather_bot backfill --days 1   # fetch + snapshot current markets & forecasts
python -m kalshi_weather_bot inspect-edges        # dry-run edge table (post M2)
python -m kalshi_weather_bot run                  # start trading loop (post M4)
```

Defaults to `mode: paper`. Live trading requires `mode: live` in `config.yaml`
**and** `KALSHI_LIVE_CONFIRM=yes-i-mean-it` **and** an interactive prompt.

## Tests

```sh
pytest tests/unit                          # fast, no network
RUN_INTEGRATION=1 pytest tests/integration  # hits demo Kalshi + Open-Meteo
```
