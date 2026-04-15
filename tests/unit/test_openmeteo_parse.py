from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_weather_bot.weather.openmeteo import OpenMeteoClient
from kalshi_weather_bot.weather.stations import STATIONS


def test_parse_daily_max_produces_samples_per_day(fixtures_dir: Path) -> None:
    raw = json.loads((fixtures_dir / "openmeteo_ensemble.json").read_text())
    client = OpenMeteoClient()
    st = STATIONS["NY"]
    fetched = datetime(2026, 4, 15, 12, tzinfo=timezone.utc)

    forecasts = client.parse_daily_max(raw, st, fetched)

    assert {ef.target_date.isoformat() for ef in forecasts} == {"2026-04-15", "2026-04-16"}
    for ef in forecasts:
        # 3 members (2 gfs + 1 ecmwf)
        assert len(ef.samples) == 3
        assert ef.variable == "tmax_f"
        assert ef.city == "NY"

    day1 = next(ef for ef in forecasts if ef.target_date.isoformat() == "2026-04-15")
    values = sorted(day1.values())
    assert values == [78.0, 79.0, 80.0]
