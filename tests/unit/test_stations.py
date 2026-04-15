from __future__ import annotations

from kalshi_weather_bot.weather.stations import STATIONS, by_city, by_series


def test_all_four_cities_present() -> None:
    assert set(STATIONS.keys()) == {"NY", "CHI", "MIA", "AUS"}


def test_series_to_station_mapping_is_consistent() -> None:
    for code, st in STATIONS.items():
        assert by_series(st.series_ticker) is st
        assert by_city(code) is st
        assert st.series_ticker == f"KXHIGH{code}"


def test_every_station_has_coordinates_and_tz() -> None:
    for st in STATIONS.values():
        assert -90 <= st.lat <= 90
        assert -180 <= st.lon <= 180
        assert st.tz.startswith("America/")
        assert st.station_id.isalpha() and st.station_id.isupper()
        assert len(st.station_id) == 4
