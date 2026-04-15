from __future__ import annotations

from pathlib import Path

from kalshi_weather_bot.config import load_config


def test_load_repo_config() -> None:
    cfg = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
    assert cfg.mode in {"paper", "demo", "live"}
    assert set(cfg.cities.keys()) == {"NY", "CHI", "MIA", "AUS"}
    for code, city in cfg.cities.items():
        assert -90 <= city.lat <= 90
        assert city.station.startswith("K") or city.station.startswith("P")  # airport ICAO
        assert code in cfg.series[0] or any(code in s for s in cfg.series)
    assert cfg.trading.close_decay_hours == 6.0
    assert cfg.trading.sizing == "flat"
