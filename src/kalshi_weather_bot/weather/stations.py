from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Station:
    city_code: str          # matches config.cities keys: NY, CHI, MIA, AUS
    series_ticker: str      # e.g., KXHIGHNY
    name: str
    lat: float
    lon: float
    tz: str
    station_id: str         # NWS station / ICAO, e.g., KNYC


# NWS primary climate stations for Kalshi weather markets. Double-checked at M1
# runtime against each market's settlement_source / rules_primary text.
STATIONS: dict[str, Station] = {
    "NY":  Station("NY",  "KXHIGHNY",  "New York (Central Park)", 40.7794, -73.9691, "America/New_York", "KNYC"),
    "CHI": Station("CHI", "KXHIGHCHI", "Chicago (O'Hare)",        41.9796, -87.9045, "America/Chicago",  "KORD"),
    "MIA": Station("MIA", "KXHIGHMIA", "Miami (MIA)",             25.7933, -80.2906, "America/New_York", "KMIA"),
    "AUS": Station("AUS", "KXHIGHAUS", "Austin (Camp Mabry)",     30.3213, -97.7599, "America/Chicago",  "KATT"),
}


def by_series(series_ticker: str) -> Station | None:
    for st in STATIONS.values():
        if st.series_ticker == series_ticker:
            return st
    return None


def by_city(code: str) -> Station | None:
    return STATIONS.get(code)
