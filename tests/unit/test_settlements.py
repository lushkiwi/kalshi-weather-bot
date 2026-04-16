from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.recorder.settlements import compute_realized_pnl


async def _seed_data(rec: Recorder) -> None:
    await rec.execute(
        "INSERT INTO orders (client_order_id, kalshi_order_id, ticker, side, action, "
        "count, yes_price, status, created_ts, resolved_ts, mode) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?, ?, ?)",
        ("c1", "k1", "KXHIGHNY-26APR15-T86", "yes", 10, 30, 1000, 1000, "paper"),
    )
    await rec.execute(
        "INSERT INTO fills (kalshi_order_id, ticker, count, yes_price, fee_cents, "
        "filled_ts, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("k1", "KXHIGHNY-26APR15-T86", 10, 30, 5, 1000, "paper"),
    )
    await rec.execute(
        "INSERT INTO orders (client_order_id, kalshi_order_id, ticker, side, action, "
        "count, yes_price, status, created_ts, resolved_ts, mode) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?, ?, ?)",
        ("c2", "k2", "KXHIGHNY-26APR15-B85.5", "no", 10, 80, 1001, 1001, "paper"),
    )
    await rec.execute(
        "INSERT INTO fills (kalshi_order_id, ticker, count, yes_price, fee_cents, "
        "filled_ts, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("k2", "KXHIGHNY-26APR15-B85.5", 10, 80, 3, 1001, "paper"),
    )
    await rec.commit()


@pytest.mark.asyncio
async def test_realized_pnl_yes_wins(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "test.sqlite3") as rec:
        await _seed_data(rec)
        await rec.execute(
            "INSERT INTO market_settlements (ticker, event_ticker, result, settled_ts) "
            "VALUES (?, ?, ?, ?)",
            ("KXHIGHNY-26APR15-T86", "KXHIGHNY-26APR15", "yes", 2000),
        )
        await rec.execute(
            "INSERT INTO market_settlements (ticker, event_ticker, result, settled_ts) "
            "VALUES (?, ?, ?, ?)",
            ("KXHIGHNY-26APR15-B85.5", "KXHIGHNY-26APR15", "yes", 2000),
        )
        await rec.commit()

        rows = await compute_realized_pnl(rec)
        assert len(rows) == 2
        # Bought YES at 30, settled YES: (100-30)*10 - 5 = 695
        yes_row = [r for r in rows if r["side"] == "yes"][0]
        assert yes_row["pnl_cents"] == 695
        # Bought NO at (100-80)=20, settled YES: -(100-80)*10 - 3 = -203
        no_row = [r for r in rows if r["side"] == "no"][0]
        assert no_row["pnl_cents"] == -203


@pytest.mark.asyncio
async def test_realized_pnl_no_wins(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "test.sqlite3") as rec:
        await _seed_data(rec)
        await rec.execute(
            "INSERT INTO market_settlements (ticker, event_ticker, result, settled_ts) "
            "VALUES (?, ?, ?, ?)",
            ("KXHIGHNY-26APR15-T86", "KXHIGHNY-26APR15", "no", 2000),
        )
        await rec.execute(
            "INSERT INTO market_settlements (ticker, event_ticker, result, settled_ts) "
            "VALUES (?, ?, ?, ?)",
            ("KXHIGHNY-26APR15-B85.5", "KXHIGHNY-26APR15", "no", 2000),
        )
        await rec.commit()

        rows = await compute_realized_pnl(rec)
        # Bought YES at 30, settled NO: -30*10 - 5 = -305
        yes_row = [r for r in rows if r["side"] == "yes"][0]
        assert yes_row["pnl_cents"] == -305
        # Bought NO at (100-80)=20, settled NO: 80*10 - 3 = 797
        no_row = [r for r in rows if r["side"] == "no"][0]
        assert no_row["pnl_cents"] == 797


@pytest.mark.asyncio
async def test_unsettled_fills_excluded(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "test.sqlite3") as rec:
        await _seed_data(rec)
        rows = await compute_realized_pnl(rec)
        assert len(rows) == 0
