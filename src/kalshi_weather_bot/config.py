from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


Mode = Literal["paper", "demo", "live"]
KalshiEnv = Literal["demo", "production"]


class KalshiConfig(BaseModel):
    env: KalshiEnv = "demo"
    rate_limit_per_sec: float = 8.0
    request_timeout_sec: float = 15.0


class OpenMeteoConfig(BaseModel):
    models: list[str] = Field(default_factory=lambda: ["gfs025", "ecmwf_ifs025", "icon_seamless"])
    forecast_days: int = 3
    rate_limit_per_sec: float = 5.0


class NwsConfig(BaseModel):
    user_agent: str
    rate_limit_per_sec: float = 3.0

    @field_validator("user_agent")
    @classmethod
    def _must_be_real(cls, v: str) -> str:
        if "set-your-email" in v or "example.com" in v:
            # NWS blocks requests without a real contact; let it through in dev but warn.
            pass
        return v


class WeatherConfig(BaseModel):
    openmeteo: OpenMeteoConfig
    nws: NwsConfig


class CityConfig(BaseModel):
    lat: float
    lon: float
    tz: str
    station: str


class EdgeMin(BaseModel):
    default: float = 0.04
    rainfall: float = 0.06
    snowfall: float = 0.08


class TradingConfig(BaseModel):
    edge_min: EdgeMin = EdgeMin()
    close_decay_hours: float = 6.0
    order_ttl_seconds: int = 180
    aggressive: bool = False
    sizing: Literal["flat", "kelly_quarter"] = "flat"
    flat_size: int = 10


class RiskConfig(BaseModel):
    max_contracts_per_market: int = 100
    max_notional_per_event: int = 200
    max_total_notional: int = 1000
    max_daily_loss_usd: int = 100
    kalshi_stale_seconds: int = 60
    openmeteo_stale_seconds: int = 1200
    nws_stale_seconds: int = 3600


class AlertsConfig(BaseModel):
    ntfy_topic: str = ""
    ntfy_base_url: str = "https://ntfy.sh"
    fill_alert_min_size: int = 25


class RecorderConfig(BaseModel):
    db_path: str = "data/recorder.sqlite3"
    raw_archive_after_days: int = 7


class AppConfig(BaseModel):
    mode: Mode = "paper"
    kalshi: KalshiConfig
    weather: WeatherConfig
    series: list[str]
    cities: dict[str, CityConfig]
    trading: TradingConfig
    risk: RiskConfig
    alerts: AlertsConfig
    recorder: RecorderConfig


class Secrets(BaseModel):
    """Secrets live in env vars only. None of these should ever appear in config.yaml."""

    kalshi_demo_key_id: str | None = None
    kalshi_demo_private_key_pem: str | None = None
    kalshi_prod_key_id: str | None = None
    kalshi_prod_private_key_pem: str | None = None
    kalshi_live_confirm: str | None = None
    openmeteo_api_key: str | None = None
    ntfy_token: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        load_dotenv(override=False)
        return cls(
            kalshi_demo_key_id=os.getenv("KALSHI_DEMO_API_KEY_ID") or None,
            kalshi_demo_private_key_pem=os.getenv("KALSHI_DEMO_PRIVATE_KEY_PEM") or None,
            kalshi_prod_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
            kalshi_prod_private_key_pem=os.getenv("KALSHI_PRIVATE_KEY_PEM") or None,
            kalshi_live_confirm=os.getenv("KALSHI_LIVE_CONFIRM") or None,
            openmeteo_api_key=os.getenv("OPENMETEO_API_KEY") or None,
            ntfy_token=os.getenv("NTFY_TOKEN") or None,
        )

    def kalshi_credentials(self, env: KalshiEnv) -> tuple[str, str]:
        if env == "demo":
            key_id, pem = self.kalshi_demo_key_id, self.kalshi_demo_private_key_pem
            label = "KALSHI_DEMO_API_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_PEM"
        else:
            key_id, pem = self.kalshi_prod_key_id, self.kalshi_prod_private_key_pem
            label = "KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PEM"
        if not key_id or not pem:
            raise RuntimeError(f"Missing Kalshi {env} credentials in env ({label})")
        return key_id, pem


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    data = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(data)
