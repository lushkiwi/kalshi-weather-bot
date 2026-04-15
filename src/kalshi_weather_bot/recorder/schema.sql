-- Kalshi weather bot recorder schema. See PLAN.md §7.

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    params_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    status_code INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_source_time ON raw_responses(source, fetched_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    series_ticker TEXT,
    snapshot_ts INTEGER NOT NULL,
    yes_bid INTEGER,
    yes_ask INTEGER,
    no_bid INTEGER,
    no_ask INTEGER,
    last_price INTEGER,
    volume INTEGER,
    floor_strike REAL,
    cap_strike REAL,
    strike_type TEXT,
    expiration_ts INTEGER,
    PRIMARY KEY (ticker, snapshot_ts)
);
CREATE INDEX IF NOT EXISTS idx_market_event_time ON market_snapshots(event_ticker, snapshot_ts);

CREATE TABLE IF NOT EXISTS forecast_samples (
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    source TEXT NOT NULL,
    member INTEGER,
    variable TEXT NOT NULL,
    value REAL NOT NULL,
    fetched_at INTEGER NOT NULL,
    run_time INTEGER NOT NULL,
    PRIMARY KEY (city, target_date, source, member, variable, fetched_at)
);

CREATE TABLE IF NOT EXISTS probability_snapshots (
    ticker TEXT NOT NULL,
    snapshot_ts INTEGER NOT NULL,
    p_fair REAL NOT NULL,
    p_market_yes_ask REAL,
    p_market_yes_bid REAL,
    edge_buy_yes REAL,
    edge_buy_no REAL,
    hours_to_close REAL NOT NULL,
    PRIMARY KEY (ticker, snapshot_ts)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    snapshot_ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    size INTEGER,
    limit_price INTEGER,
    reason TEXT NOT NULL,
    mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    kalshi_order_id TEXT UNIQUE,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    count INTEGER NOT NULL,
    yes_price INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    resolved_ts INTEGER,
    mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    kalshi_order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    count INTEGER NOT NULL,
    yes_price INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    filled_ts INTEGER NOT NULL,
    mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    event_ticker TEXT NOT NULL,
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    variable TEXT NOT NULL,
    observed_value REAL NOT NULL,
    nws_source TEXT NOT NULL,
    settled_at INTEGER NOT NULL,
    PRIMARY KEY (event_ticker)
);
