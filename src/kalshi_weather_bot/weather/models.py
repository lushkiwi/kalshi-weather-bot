from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(slots=True)
class ForecastSample:
    """One ensemble member's prediction for one (city, target_date, variable)."""

    city: str
    target_date: date
    source: str                    # 'gfs025' | 'ecmwf_ifs025' | 'icon_seamless' | 'nws_point'
    member: int | None             # None for deterministic forecasts
    variable: str                  # 'tmax_f' | 'tmin_f' | 'precip_in' | 'snow_in'
    value: float
    fetched_at: datetime
    run_time: datetime


@dataclass(slots=True)
class EnsembleForecast:
    """All ensemble samples for a single (city, target_date, variable)."""

    city: str
    target_date: date
    variable: str
    samples: list[ForecastSample] = field(default_factory=list)

    def values(self) -> list[float]:
        return [s.value for s in self.samples]
