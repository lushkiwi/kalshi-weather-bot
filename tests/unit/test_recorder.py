from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_weather_bot.recorder.db import Recorder


@pytest.mark.asyncio
async def test_recorder_creates_schema_and_roundtrips(tmp_path: Path) -> None:
    db = tmp_path / "rec.sqlite3"
    async with Recorder(db) as rec:
        await rec.execute(
            "INSERT INTO raw_responses (source, endpoint, params_json, response_json, fetched_at, status_code) VALUES (?,?,?,?,?,?)",
            ("openmeteo", "/ensemble", "{}", '{"ok": true}', 1700000000, 200),
        )
        await rec.commit()
        rows = await rec.fetchall("SELECT source, status_code FROM raw_responses")
        assert rows == [("openmeteo", 200)]


@pytest.mark.asyncio
async def test_recorder_idempotent_connect(tmp_path: Path) -> None:
    db = tmp_path / "rec2.sqlite3"
    async with Recorder(db):
        pass
    async with Recorder(db) as rec:
        rows = await rec.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {r[0] for r in rows}
        assert {"raw_responses", "market_snapshots", "forecast_samples", "orders", "fills", "settlements"} <= names
