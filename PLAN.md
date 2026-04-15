# Kalshi Weather Trading Bot — Architecture Plan

## Context

You want a Python bot that systematically finds and trades mispriced Kalshi weather binary contracts by converting ensemble weather forecasts into probability distributions, comparing them to Kalshi market-implied probabilities, and placing fee-aware trades when the expected-value edge clears a configurable threshold. The bot will eventually run with real money, so correctness and risk controls take priority over throughput. It must default to paper trading, gate live trading behind both a config flag and an interactive confirmation, respect hard risk limits, continuously snapshot data for backtesting, and keep modules small, typed, and unit-tested.

Kalshi's weather markets are binary contracts priced 1¢–99¢, settle on National Weather Service climate reports the morning after the observation day, and currently cover NYC, Chicago, Miami, and Austin for daily highs, lows, rainfall, and snowfall. Ticker examples: `KXHIGHNY-26APR15-T90` ("NYC high > 90°F"), `KXHIGHNY-26APR15-B89.5` ("NYC high between 89 and 90°F"). Event groupings share a prefix like `KXHIGHNY-26APR15`. Fees are `ceil(0.07·C·P·(1−P))` taker and `ceil(0.0175·C·P·(1−P))` maker, in cents, rounded up per-order.

## 1. Project Structure

```
kalshi-weather-bot/
├── config.yaml                     # All runtime config (not secrets)
├── .env.example                    # Template for secrets
├── pyproject.toml                  # uv / pip deps + tool config
├── README.md
├── PLAN.md                         # This file (copied to repo)
├── src/kalshi_weather_bot/
│   ├── __init__.py
│   ├── __main__.py                 # `python -m kalshi_weather_bot` entrypoint
│   ├── cli.py                      # argparse/typer: run, paper, live, backfill, status
│   ├── config.py                   # Pydantic settings: load YAML + env, validate
│   ├── logging_setup.py            # structlog / stdlib config, rotation
│   │
│   ├── kalshi/
│   │   ├── __init__.py
│   │   ├── auth.py                 # RSA-PSS signing of requests (~80 lines)
│   │   ├── client.py               # async httpx client, retry, rate limit (~250)
│   │   ├── models.py               # Pydantic: Market, Event, Orderbook, Order, Fill, Position
│   │   ├── markets.py              # Discovery: list active weather markets, parse tickers
│   │   └── orders.py               # Place/cancel/query orders, position reconciliation
│   │
│   ├── weather/
│   │   ├── __init__.py
│   │   ├── openmeteo.py            # Async Open-Meteo ensemble client
│   │   ├── nws.py                  # Async NWS point + climate API (validator + settle)
│   │   ├── stations.py             # City→NWS station mapping (KNYC, KORD, KMIA, KAUS)
│   │   └── models.py               # EnsembleForecast, ForecastSample dataclasses
│   │
│   ├── probability/
│   │   ├── __init__.py
│   │   ├── distributions.py        # ECDF + KDE/parametric blending (~150)
│   │   ├── threshold.py            # Map contract (floor/cap/strike_type) → probability
│   │   └── calibration.py          # Historical bias correction (loaded from recorder)
│   │
│   ├── edge/
│   │   ├── __init__.py
│   │   ├── implied.py              # Orderbook → implied probability (bid/ask/mid)
│   │   ├── fees.py                 # Kalshi fee formulas, net-of-fee EV
│   │   └── detector.py             # EdgeReport: contract, p_fair, p_market, edge_bps, side
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── sizing.py               # Kelly-fraction + risk-cap position sizing
│   │   ├── router.py               # Pick limit price, route order, handle partials
│   │   └── paper.py                # Simulated matching engine mirroring live API
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── limits.py               # Per-contract / daily-loss / total-exposure checks
│   │   ├── killswitch.py           # Flatten + halt on trigger
│   │   └── health.py               # Data-freshness gates (refuse to trade if stale)
│   │
│   ├── recorder/
│   │   ├── __init__.py
│   │   ├── schema.sql              # SQLite DDL
│   │   ├── db.py                   # aiosqlite connection pool + migrations
│   │   └── snapshot.py             # Periodic market + forecast snapshotting
│   │
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── loop.py                 # APScheduler AsyncIOScheduler, trading tick
│   │
│   ├── alerts/
│   │   ├── __init__.py
│   │   └── notifier.py             # ntfy.sh / Pushover async POST
│   │
│   └── util/
│       ├── __init__.py
│       ├── time_utils.py           # tz-aware helpers, "next NWS close" etc.
│       └── retry.py                # tenacity wrappers
│
├── tests/
│   ├── conftest.py
│   ├── fixtures/                   # Canned Kalshi + Open-Meteo JSON
│   ├── unit/                       # Mirrors src tree
│   └── integration/                # Hits demo Kalshi, live Open-Meteo
└── scripts/
    ├── backfill_markets.py         # One-shot historical pull
    └── inspect_edges.py            # Dry-run: list current edges w/o trading
```

**Data flow (per 5-minute tick):**

1. `scheduler.loop` fires a tick.
2. `risk.health` checks: clocks, last-successful-fetch timestamps, DB writable. If any fail → paper-only mode forced.
3. `kalshi.markets.list_active_weather_markets()` → list of contracts we care about (filtered by `config.series`).
4. `weather.openmeteo.fetch_ensembles(cities, dates)` + `weather.nws.fetch_point_forecast(...)` in parallel.
5. `probability.threshold.contract_probability(contract, ensemble)` → `p_fair` for each contract.
6. `kalshi.markets.orderbook(ticker)` batched → best bid / ask → `edge.implied`.
7. `edge.detector.compute(...)` → list of `EdgeReport` net of fees, passes minimum-edge filter.
8. `risk.limits.approve(...)` drops anything violating caps.
9. `execution.router.submit(...)` places orders (paper or live).
10. `recorder.snapshot.save(...)` writes raw responses + parsed data + decisions.
11. `alerts.notifier` fires on kill-switch, data-staleness > threshold, or fills > size threshold.

## 2. Data Sources

### Kalshi API v2

- **Base URLs:**
  - Production: `https://api.elections.kalshi.com/trade-api/v2`
  - Demo: `https://demo-api.kalshi.co/trade-api/v2`
- **Auth:** RSA-PSS signed requests. Header trio per request: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`. Signature = base64(RSA-PSS-SHA256(privkey, `${ts}${METHOD}${path}`)). Private key stored in env var `KALSHI_PRIVATE_KEY_PEM`, key ID in `KALSHI_API_KEY_ID`. Demo and prod keys are separate; config picks which env var pair to load based on `mode: paper | live | demo`.
- **Endpoints used:**
  - `GET /markets?series_ticker=KXHIGHNY&status=open` — discovery
  - `GET /markets/{ticker}` — detail incl. `floor_strike`, `cap_strike`, `strike_type` ("greater" / "less" / "between"), `expiration_time`
  - `GET /markets/{ticker}/orderbook` — top of book
  - `GET /events?series_ticker=...` — grouping
  - `POST /portfolio/orders` — place (fields: `ticker`, `action` buy/sell, `side` yes/no, `type` limit/market, `count`, `yes_price` in cents, `client_order_id`)
  - `DELETE /portfolio/orders/{id}` — cancel
  - `GET /portfolio/positions` — reconcile
  - `GET /portfolio/fills` — realized P&L
- **Rate limits:** tiered; safe default ≤10 req/s. Use token bucket in `kalshi/client.py`. 200k open-order cap is not a concern.
- **WebSocket** is out of scope for v1; polling every 5 min is sufficient for daily-settled markets.

### Weather — both sources, per your choice

**Open-Meteo Ensemble (primary):**
- Endpoint: `https://ensemble-api.open-meteo.com/v1/ensemble`
- Params: `latitude`, `longitude`, `models=gfs025,ecmwf_ifs025,icon_seamless`, `hourly=temperature_2m`, `forecast_days=3`, `timezone=America/New_York`.
- Returns per-member hourly temperature arrays. We reduce to daily max/min by taking max/min of the `observation_day` local-time window matching each city's NWS climate-day definition.
- Rate limit free tier: 600/min, 5k/hr, 10k/day, non-commercial. **Flag:** Open-Meteo's terms require a paid subscription for commercial use, and real-money trading arguably qualifies. Budget for ~$29/mo or self-host. Covered in open questions below.

**NWS (validator + settlement ground truth):**
- Base: `https://api.weather.gov`
- `GET /points/{lat},{lon}` → grid resolution
- `GET /gridpoints/{office}/{x},{y}/forecast` → point deterministic forecast
- `GET /stations/{stationId}/observations/latest` for live obs
- Settlement: NWS `/products` climate report (e.g., `CLINYC`) — official settlement source; we also use this to build the post-settlement P&L record.
- No auth; requires `User-Agent: kalshi-weather-bot (email)`. No hard rate limit published; be polite (<=5 req/s).
- **Station map:** NYC=KNYC, Chicago=KORD, Miami=KMIA, Austin=KAUS — verified against Kalshi help docs which state settlement follows NWS climate reports.

**Why both:** Open-Meteo gives us cheap, wide ensembles for `p_fair`. NWS gives the authoritative deterministic forecast we cross-check — if Open-Meteo median drifts >3°F from the NWS point forecast we log a warning and widen our minimum-edge threshold. NWS also provides the actual settlement value, needed for calibration and backtesting.

## 3. Probability Math

The hardest and most load-bearing module. Get this right before anything downstream matters.

### From ensemble → daily aggregate

For each contract:
1. Identify the `(city, observation_date, variable)` it settles on. Parse from ticker + market metadata.
2. For each ensemble member `m`, compute the member's daily-max temperature as `max_t D(m)` over the NWS climate-day window (midnight-to-midnight local; NYC/Chicago use local standard time per NWS CLI product definition).
3. You now have a sample `{x_1, …, x_N}` of size N = (members × models) — e.g., 31 GFS + 51 ECMWF + 40 ICON ≈ 122 samples.

### From samples → contract probability

Three regimes, chosen per contract:

**(a) Far from threshold (|median − strike| > 2·σ):** empirical CDF is fine. `P(X > strike) = (#{x_i > strike} + 0.5) / (N + 1)` (Laplace smoothing to avoid 0/1).

**(b) Near threshold (|median − strike| ≤ 2·σ):** empirical CDF is noisy at the tail. Fit a Gaussian KDE with Silverman's rule bandwidth over the samples and integrate: `P = 1 − KDE.cdf(strike)`. Use `scipy.stats.gaussian_kde`. This smooths the "which side of the strike is the 17th member" discreteness.

**(c) Time-horizon correction:** ensemble spread underestimates true uncertainty at longer horizons (<3 days is generally fine). We apply a learned multiplicative inflation factor `k(h)` to the sample standard deviation before KDE, where `h` is hours to close. `k(h)` is initialized to 1.0, then re-estimated quarterly from the recorder's historical forecast vs. actual pairs (see calibration.py). Until ≥200 settled samples exist, `k(h) ≡ 1.0` and we require a larger edge threshold.

### Contract strike types

- `strike_type == "greater"`: `P_yes = P(X > floor_strike)`
- `strike_type == "less"`: `P_yes = P(X < floor_strike)` (floor_strike doubles as cap here per Kalshi's schema)
- `strike_type == "between"`: `P_yes = P(floor_strike ≤ X ≤ cap_strike)` — compute as `KDE.cdf(cap) − KDE.cdf(floor − 1)` (temps in Kalshi are integer °F; intervals are inclusive on both ends by convention)

### Coherence check across the event ladder

For a given event (e.g., `KXHIGHNY-26APR15`), the `between` contracts tile the range and should sum to ≈1.0 together with the tails. After pricing all contracts in an event we assert `|Σ p_fair − 1.0| < 0.02`; violations log a warning and skip the entire event for that tick (likely a parsing bug or missing contract).

### Calibration

`probability/calibration.py` consumes the recorder DB: for every settled contract, `(p_fair_at_T_minus_h, did_yes_settle)`. Group by horizon bucket, compute reliability diagram and Brier score. If calibration is poor for a horizon, that horizon's trades are gated off until fixed. This is a nightly batch job, not inline.

## 4. Edge Detection

### Defining edge

- `p_market_buy_yes = (yes_ask + 1) / 100` (in $). Buying YES means paying the ask.
- `p_market_sell_yes = (yes_bid) / 100`.
- `p_market_mid = (yes_bid + yes_ask) / 200`.
- We **never trade off the mid**; we compute two candidate trades per contract:
  - **Buy YES** at `yes_ask` if `p_fair − (yes_ask/100) − fee_rate(yes_ask) > edge_min`.
  - **Buy NO** at `(1 − yes_bid/100)` if `(1 − p_fair) − (1 − yes_bid/100) − fee_rate(1 − yes_bid/100) > edge_min`. (Kalshi treats NO as its own order.)
- `fee_rate(p) = ceil(0.07 · 1 · p · (1−p) · 100) / 100` per contract for takers. Maker fees use 0.0175. Rounding is up per order (not per contract), so our sizing module applies the ceiling to the whole order.

### Minimum edge

Default `edge_min = 0.04` (4 percentage points of probability net of fees). Rationale: max taker fee at P=0.5 is `ceil(0.07·1·0.25) = 2¢ = 2pp`. A 4pp edge gives ~2pp margin for slippage, queue position risk, and model error. Configurable per-variable in YAML; I recommend 0.06 for rainfall/snowfall initially (fatter tails, less calibration data).

### Time-decay on edge threshold

As a market approaches settlement, remaining forecast uncertainty shrinks while slippage and queue risk stay the same, so a stale edge is more likely noise than signal. Apply a linear ramp over the final 6 hours to close:

```
effective_edge_min(h) = edge_min * (1 + clamp(1 - h/6, 0, 1))   # h = hours to close
```

So `h ≥ 6` → `edge_min`; `h = 3` → `1.5·edge_min`; `h = 0` → `2·edge_min`. Applied identically to every contract. Implemented in `edge/detector.py` alongside the base threshold check.

### Bid-ask handling

Buy YES crosses the spread at `yes_ask`. For non-urgent entries, we offer `yes_ask - 1¢` and sit (maker). If unfilled after `order_ttl_seconds` (default 180s), we either cancel or cross depending on `aggressive: true` flag. Default is to cancel and re-evaluate next tick — this gives cleaner behavior and simpler paper-trade accounting.

### Multiple contracts on the same event

The event ladder (e.g., highs > 80, > 85, > 90) is not independent; they're nested. Rules:
1. Compute `p_fair` for every contract in the event from the same ensemble draw.
2. Rank by edge_bps descending.
3. Take the top edge as primary. For additional contracts in the same event, only add if the implied joint position doesn't violate the event-level exposure cap (see risk).
4. Don't take opposite sides of nested contracts — if we're long YES on "> 85" we won't short YES on "> 90" in the same tick; Kalshi settles these correlated, and the net position is confusing to reason about.

## 5. Order Execution

### Order flow

1. Router receives `TradeDecision(ticker, side, size, limit_price, ttl)`.
2. Generate deterministic `client_order_id = hash(ticker, tick_ts, side)`; idempotent replay safe.
3. POST `/portfolio/orders` with `type=limit`, `count=size`, `yes_price=limit_price_cents`.
4. On 2xx, store `(order_id, ttl_expires_at)` in the active-orders table.
5. Each tick, poll `GET /portfolio/fills?min_ts=last_seen` to update; call `GET /portfolio/positions` and reconcile against our mental model (if they diverge, emit an alert and halt).
6. At TTL expiry, `DELETE /portfolio/orders/{id}`; partial fills are accepted and the unfilled remainder canceled.

### Paper vs live switching

`config.mode`:
- `paper`: uses `demo-api.kalshi.co` for market data (free, real prices) but routes orders to `execution.paper.PaperBroker` which maintains a simulated book. Mirrors the live `OrderClient` interface exactly. P&L recorded in same schema as live.
- `demo`: uses `demo-api.kalshi.co` end-to-end, places real orders in the demo env (mock funds). Useful for testing API integration without simulating matching ourselves.
- `live`: prod env. Startup requires:
  1. `live: true` in config.yaml
  2. `KALSHI_LIVE_CONFIRM=yes-i-mean-it` in environment
  3. Interactive prompt on first invocation each day: "Type the word LIVE and today's date to confirm."

Any mismatch → abort with non-zero exit.

### Partial fills

Kalshi fills are per-contract. `OrderClient.wait_for_fill(order_id, timeout)` polls `/portfolio/fills` and yields `Fill` events until either fully filled, canceled, or timeout. Sizing tolerates partial outcomes; we do not "chase" by adjusting price on the remainder during the same tick — next tick will re-evaluate.

## 6. Risk Management

All limits loaded from `config.risk` section. Values are defaults; tune per your comfort.

- **Per-contract max position:** `max_contracts_per_market = 100` (i.e., $100 notional, since contracts settle 0–$1).
- **Per-event max exposure:** `max_notional_per_event = 200` ($). Prevents stacking every rung of the temp ladder.
- **Daily max loss:** `max_daily_loss_usd = 100`. Computed from sum of realized + mark-to-market unrealized since 00:00 ET. On breach: cancel all open orders, flip to paper mode for the rest of the UTC day, send alert. Resets at 00:00 ET next day.
- **Total portfolio exposure:** `max_total_notional = 1000` ($). Sum of outstanding contract notional + open-order notional.
- **Kill switch:**
  - Manual: `python -m kalshi_weather_bot kill` — cancels all open orders via `DELETE` loop, writes a `kill.lock` file that blocks the scheduler from placing new orders until removed.
  - Automatic triggers: daily-loss breach, position-reconciliation mismatch, more than 3 consecutive tick failures, data-staleness > 30 min for both weather sources.
- **Data-staleness gate:**
  - Kalshi market data: don't trade a market whose `last_updated` > 60s old.
  - Open-Meteo: don't trade if last successful fetch > 20 min or if the model `run_time` is > 8 hours old.
  - NWS: warn-only; if Open-Meteo looks sane and NWS is down, we still trade with slightly widened `edge_min`.
- **Paper mode behavior:** every gate fires identically; only the order submission changes.

## 7. Data Recording for Backtesting

Runs unconditionally on every tick — even before live trading is enabled. Backtesting is impossible without it.

### SQLite schema (`recorder/schema.sql`)

```sql
-- Raw API responses for forensic replay; keep forever.
CREATE TABLE raw_responses (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,           -- 'kalshi_markets' | 'kalshi_orderbook' | 'openmeteo' | 'nws_point' | 'nws_climate'
    endpoint TEXT NOT NULL,
    params_json TEXT NOT NULL,
    response_json TEXT NOT NULL,    -- gzipped if > 8kb (store as BLOB column in practice)
    fetched_at INTEGER NOT NULL,    -- unix seconds
    status_code INTEGER NOT NULL
);
CREATE INDEX idx_raw_source_time ON raw_responses(source, fetched_at);

-- Parsed Kalshi market snapshots.
CREATE TABLE market_snapshots (
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    snapshot_ts INTEGER NOT NULL,
    yes_bid INTEGER, yes_ask INTEGER,        -- cents
    no_bid INTEGER,  no_ask INTEGER,
    last_price INTEGER, volume INTEGER,
    floor_strike REAL, cap_strike REAL, strike_type TEXT,
    expiration_ts INTEGER NOT NULL,
    PRIMARY KEY (ticker, snapshot_ts)
);
CREATE INDEX idx_market_event_time ON market_snapshots(event_ticker, snapshot_ts);

-- Parsed weather forecasts. One row per (city, target_date, source, member, fetched_at).
CREATE TABLE forecast_samples (
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,      -- YYYY-MM-DD local
    source TEXT NOT NULL,           -- 'gfs025' | 'ecmwf_ifs025' | 'icon_seamless' | 'nws_point'
    member INTEGER,                 -- null for deterministic
    variable TEXT NOT NULL,         -- 'tmax_f' | 'tmin_f' | 'precip_in' | 'snow_in'
    value REAL NOT NULL,
    fetched_at INTEGER NOT NULL,
    run_time INTEGER NOT NULL,      -- model init time
    PRIMARY KEY (city, target_date, source, member, variable, fetched_at)
);

-- Computed probabilities per tick.
CREATE TABLE probability_snapshots (
    ticker TEXT NOT NULL,
    snapshot_ts INTEGER NOT NULL,
    p_fair REAL NOT NULL,
    p_market_yes_ask REAL, p_market_yes_bid REAL,
    edge_buy_yes REAL, edge_buy_no REAL,
    hours_to_close REAL NOT NULL,
    PRIMARY KEY (ticker, snapshot_ts)
);

-- Trade decisions (whether executed or not).
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    snapshot_ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,           -- 'buy_yes' | 'buy_no' | 'skip_small_edge' | 'skip_risk_cap' | 'skip_stale'
    size INTEGER, limit_price INTEGER,
    reason TEXT NOT NULL,
    mode TEXT NOT NULL              -- 'paper' | 'demo' | 'live'
);

-- Orders + fills (mirrors Kalshi state).
CREATE TABLE orders (
    client_order_id TEXT PRIMARY KEY,
    kalshi_order_id TEXT UNIQUE,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL, action TEXT NOT NULL,
    count INTEGER NOT NULL, yes_price INTEGER NOT NULL,
    status TEXT NOT NULL,           -- 'open' | 'filled' | 'partial' | 'canceled'
    created_ts INTEGER NOT NULL,
    resolved_ts INTEGER, mode TEXT NOT NULL
);
CREATE TABLE fills (
    id INTEGER PRIMARY KEY,
    kalshi_order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    count INTEGER NOT NULL, yes_price INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    filled_ts INTEGER NOT NULL, mode TEXT NOT NULL
);

-- Settlement outcomes (joined for calibration).
CREATE TABLE settlements (
    event_ticker TEXT NOT NULL,
    city TEXT NOT NULL, target_date TEXT NOT NULL,
    variable TEXT NOT NULL,
    observed_value REAL NOT NULL,
    nws_source TEXT NOT NULL,        -- e.g., 'CLINYC'
    settled_at INTEGER NOT NULL,
    PRIMARY KEY (event_ticker)
);
```

WAL mode enabled, `PRAGMA synchronous=NORMAL`. DB file under `data/recorder.sqlite3`. Nightly job compresses `raw_responses` rows older than 7 days into a parquet archive and deletes from live DB to keep it snappy.

## 8. Scheduling and Operations

- **Scheduler:** APScheduler `AsyncIOScheduler` inside a long-running daemon process (`python -m kalshi_weather_bot run`). Two jobs:
  - `trading_tick` every 5 minutes (`*/5 * * * *`).
  - `calibration_nightly` at 03:00 ET daily.
  - Extra one-shot jobs spawned post-settlement to record outcomes.
- **Process model:** Single process, async. Runs under `launchd` (macOS) or systemd (Linux) with auto-restart. No Docker required for v1.
- **Logging:**
  - `structlog` → JSON lines to stdout + `logs/bot.log` with `RotatingFileHandler` (10 MB × 10).
  - Levels: DEBUG for tick internals (off by default), INFO for decisions & orders, WARNING for stale-data / calibration drift, ERROR for API failures, CRITICAL for kill-switch.
  - Every log line includes `tick_id` (UUID per tick) and `mode` for correlation.
- **Config (`config.yaml`):**
  ```yaml
  mode: paper                       # paper | demo | live
  kalshi:
    env: demo                       # demo | production
    rate_limit_per_sec: 8
  weather:
    openmeteo:
      models: [gfs025, ecmwf_ifs025, icon_seamless]
    nws:
      user_agent: "kalshi-weather-bot (you@example.com)"
  series: [KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHAUS]
  trading:
    edge_min:
      default: 0.04
      rainfall: 0.06
      snowfall: 0.08
    close_decay_hours: 6            # linear ramp: edge_min → 2·edge_min over last 6h
    order_ttl_seconds: 180
    aggressive: false
    sizing: flat                    # flat | kelly_quarter (enable post-M6)
    flat_size: 10                   # contracts per trade when sizing: flat
  risk:
    max_contracts_per_market: 100
    max_notional_per_event: 200
    max_total_notional: 1000
    max_daily_loss_usd: 100
  alerts:
    ntfy_topic: kalshi-bot-XXXX     # obscure to avoid abuse
    fill_alert_min_size: 25
  ```
- **Secrets (env / .env, never in config.yaml):** `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PEM`, `KALSHI_DEMO_API_KEY_ID`, `KALSHI_DEMO_PRIVATE_KEY_PEM`, `OPENMETEO_API_KEY` (if commercial), `NTFY_TOKEN` (if private topic), `KALSHI_LIVE_CONFIRM` (only set for live mode boots).
- **Alerts (ntfy.sh):**
  - Kill-switch fired (CRITICAL)
  - Daily loss > 50% of limit (WARN)
  - Any fill ≥ `fill_alert_min_size` contracts (INFO)
  - 3+ consecutive tick failures (ERROR)
  - Data staleness >30 min on both sources (WARN)
  - Nightly summary with P&L, #trades, calibration drift (INFO)
- **Operational CLI:** `run`, `tick` (one-shot), `kill`, `status`, `backfill` (historical market pull), `inspect-edges` (dry-run report), `calibrate` (force recalc).

## 9. Dependencies

```toml
[project]
requires-python = ">=3.11"
dependencies = [
  "httpx[http2]==0.27.2",          # async HTTP, HTTP/2 for Kalshi
  "pydantic==2.9.2",               # models & settings validation
  "pydantic-settings==2.5.2",      # env + yaml loading
  "pyyaml==6.0.2",                 # config file
  "cryptography==43.0.1",          # RSA-PSS signing for Kalshi
  "aiosqlite==0.20.0",             # async SQLite
  "apscheduler==3.10.4",           # scheduling (async variant)
  "structlog==24.4.0",             # structured logging
  "tenacity==9.0.0",               # retries with backoff
  "numpy==2.1.2",                  # ensemble arithmetic
  "scipy==1.14.1",                 # gaussian_kde, stats
  "typer==0.12.5",                 # CLI
  "python-dotenv==1.0.1",          # .env loading
]
[project.optional-dependencies]
dev = [
  "pytest==8.3.3",
  "pytest-asyncio==0.24.0",
  "pytest-httpx==0.32.0",          # mock httpx responses
  "freezegun==1.5.1",              # time control in tests
  "hypothesis==6.112.1",           # property-based tests (probability math!)
  "ruff==0.6.9",
  "mypy==1.11.2",
]
```

No heavy GRIB or xarray deps since we decided to consume NWS point API (not raw GRIB). No pandas — numpy/scipy are enough for the math, and dataframe objects tend to leak into places they don't belong.

## 10. Build Order (Milestones)

Each milestone is independently runnable with its own tests; no milestone is "done" until tests pass in CI-equivalent local runs and the user has manually sanity-checked output.

### M1 — Data fetching (Kalshi read-only + weather)
- `kalshi/auth.py`, `kalshi/client.py`, `kalshi/markets.py`, `kalshi/models.py`: list all active `KXHIGH{NY,CHI,MIA,AUS}` markets and orderbooks against demo env.
- `weather/openmeteo.py`, `weather/nws.py`, `weather/stations.py`, `weather/models.py`: fetch 4-city 3-day ensembles and NWS point forecasts.
- `recorder/db.py`, `recorder/schema.sql`, `recorder/snapshot.py`: persist raw + parsed.
- `config.py`, `logging_setup.py`, `cli.py` skeleton.
- **Tests:**
  - Unit: auth signing matches Kalshi's published sample vectors; Pydantic models validate against fixture JSON; station mapping is exhaustive.
  - Integration: one real-call-against-demo-Kalshi + real-call-against-Open-Meteo test, gated by a `RUN_INTEGRATION=1` env var.
- **Exit criteria:** `python -m kalshi_weather_bot backfill --days 1` produces a populated SQLite file you can inspect.

### M2 — Probability engine (offline, no trading)
- `probability/distributions.py`, `probability/threshold.py`.
- Hypothesis-based property tests: `p_fair ∈ [0,1]`; ladder sums to ~1.0 across a synthetic event; monotonicity (higher strike ≤ lower P).
- Golden-file tests against three hand-computed ensemble samples.
- **Exit criteria:** `python -m kalshi_weather_bot inspect-edges --dry-run` prints a table of every active market with market price, fair price, and edge — no trades placed.

### M3 — Edge detection + fees
- `edge/fees.py` (exact Kalshi formula with ceiling); unit tests against published examples.
- `edge/implied.py`, `edge/detector.py`.
- **Exit criteria:** `inspect-edges` table now includes net-of-fee edge and flags contracts above `edge_min`.

### M4 — Paper trading + risk gates
- `execution/paper.py`, `execution/sizing.py` (**flat sizing only**: size = `min(config.flat_size, risk_cap_remaining)`), `execution/router.py` (paper path only).
- `risk/limits.py`, `risk/health.py`, `risk/killswitch.py`.
- `scheduler/loop.py` wires it all end-to-end.
- `alerts/notifier.py` fires on simulated fills + kill-switch.
- **Tests:** paper broker matches a scripted orderbook sequence correctly; risk limits reject orders at boundary conditions; kill-switch cancels and halts.
- **Exit criteria:** Run the bot in paper mode for 48 hours on live Kalshi prices. Verify simulated P&L, trade log, and alerts behave sanely. Manually review a few filled trades for correctness.

### M5 — Demo live orders
- Swap paper broker for real Kalshi demo-env orders via `kalshi/orders.py`. Production code path; same risk gates.
- Add `GET /portfolio/positions` reconciliation to `risk/health.py`.
- **Exit criteria:** Run in demo mode for 1 week. Position reconciliation clean, no surprise fills, alerts sensible.

### M6 — Calibration + settlement ingestion (+ Kelly sizing switch)
- `probability/calibration.py`, nightly scheduler job.
- NWS CLI product parser in `weather/nws.py` to extract observed daily high/low/precip.
- Reliability diagrams saved to `data/reports/`.
- Once ≥200 settlements show Brier < 0.20 and good reliability, extend `execution/sizing.py` with **fractional Kelly (0.25× Kelly, capped by `max_contracts_per_market`)** selected via `config.trading.sizing: flat | kelly_quarter`. Flat remains default until you flip the flag.
- **Exit criteria:** Calibration report shows Brier < 0.20 and roughly-diagonal reliability; A/B comparison of flat vs 0.25× Kelly on the recorded tick log shows Kelly doesn't produce crazy sizing (no single-order size > per-market cap, no position concentration spikes).

### M7 — Live trading
- Enable `mode: live`, wire the `LIVE_CONFIRM` env var + CLI prompt.
- Start with `max_daily_loss_usd: 20` and `max_contracts_per_market: 10` for the first two weeks.
- Daily summary alert becomes mandatory.
- **Exit criteria:** Two weeks of live trading with results within one standard deviation of paper-mode backtest over the same period.

## Critical Files (quick reference)

- `src/kalshi_weather_bot/kalshi/auth.py` — RSA-PSS signing; cryptographically load-bearing.
- `src/kalshi_weather_bot/probability/threshold.py` — turns forecasts into `p_fair`; the whole strategy lives or dies here.
- `src/kalshi_weather_bot/edge/fees.py` — fee math must exactly match Kalshi's ceiling rules; a rounding error compounds into unprofitable trades.
- `src/kalshi_weather_bot/risk/limits.py` — last line of defense before capital is deployed.
- `src/kalshi_weather_bot/execution/router.py` — must be idempotent under retries (client_order_id).
- `src/kalshi_weather_bot/recorder/schema.sql` — once live data flows in, schema changes become migrations.

## Verification Plan

End-to-end validation that the finished bot works correctly:

1. **Unit + property tests:** `pytest tests/unit` green; `hypothesis` runs with default settings pass for probability and fee math.
2. **Integration against demo Kalshi:** `RUN_INTEGRATION=1 pytest tests/integration` — authenticates, lists markets, places + cancels a demo order.
3. **Dry-run edge inspection:** `python -m kalshi_weather_bot inspect-edges` on 4 active cities. Manually sanity-check 2–3 rows: does the edge direction make sense given the NWS forecast?
4. **Paper mode soak test:** 48 hours of `mode: paper`. After, query SQLite: `SELECT count(*), sum(CASE WHEN action='buy_yes' THEN 1 ELSE 0 END) FROM decisions WHERE snapshot_ts > ?`. Confirm trades were taken, alerts fired on simulated fills, no tick-loop crashes.
5. **Kill-switch drill:** While paper mode is running, in another shell run `python -m kalshi_weather_bot kill`. Verify all open paper orders cancel within one tick and `kill.lock` blocks new orders.
6. **Demo-env soak test:** 1 week of `mode: demo`. Verify position reconciliation between our DB and `GET /portfolio/positions` is clean every tick.
7. **Calibration sanity:** After ≥200 settlements, run `python -m kalshi_weather_bot calibrate` and inspect `data/reports/reliability.png`. Expect roughly-diagonal reliability.
8. **Live cutover:** only after steps 1–7 all pass, with reduced limits as in Milestone 7.

## Resolved Decisions

1. **Open-Meteo licensing.** Code path is the same either way (same endpoint, optional `apikey` query param). Dev + paper trading runs on the free tier (non-commercial). Before flipping `mode: live` in M7, subscribe to the paid plan and set `OPENMETEO_API_KEY` env var; client reads the key if present and routes to the paid endpoint. Licensing gate is documented in M7's startup checklist alongside the `LIVE_CONFIRM` prompt.

2. **Sizing.** Flat sizing (`flat_size: 10` contracts) through M4 and M5 — easier to reason about during paper soak and demo. Fractional Kelly (0.25×) is implemented as an opt-in in M6 after calibration demonstrates Brier < 0.20; switched on by changing `config.trading.sizing` from `flat` to `kelly_quarter`.

3. **Multi-rung stacking.** Single best edge per event per tick, with per-event notional cap. Revisit post-M6.

4. **Time-decay on edge threshold.** Linear ramp over final 6 hours: `edge_min` → `2·edge_min`. Formula in §4. Config key: `trading.close_decay_hours: 6`.

5. **NWS station mapping.** Verified during M1 against each market's `settlement_source` / rulebook text, not just the help-center prose.

6. **Lows, rainfall, snowfall.** Deferred to post-M7. `KXHIGH*` series only through live cutover.
