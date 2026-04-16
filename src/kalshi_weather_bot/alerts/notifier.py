"""Thin wrapper around ntfy.sh.

Configured topic is posted to as a plain HTTPS POST; an empty topic disables
alerts entirely so the bot runs fine in a sandbox without external calls.
Bearer token is optional (ntfy.sh private topics).
"""

from __future__ import annotations

from typing import Literal

import httpx

from kalshi_weather_bot.config import AlertsConfig
from kalshi_weather_bot.logging_setup import get_logger


Level = Literal["info", "warn", "error", "critical"]


class Notifier:
    def __init__(self, cfg: AlertsConfig, token: str | None = None) -> None:
        self._cfg = cfg
        self._token = token
        self._log = get_logger("alerts.notifier").bind(topic_set=bool(cfg.ntfy_topic))

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.ntfy_topic)

    async def send(self, level: Level, title: str, body: str) -> bool:
        if not self.enabled:
            self._log.debug("alert_suppressed", level=level, title=title)
            return False
        url = f"{self._cfg.ntfy_base_url.rstrip('/')}/{self._cfg.ntfy_topic}"
        headers = {"Title": title, "Priority": _priority(level), "Tags": level}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(url, content=body.encode(), headers=headers)
                r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            self._log.warning("alert_failed", level=level, title=title, error=str(e))
            return False


def _priority(level: Level) -> str:
    return {"info": "3", "warn": "4", "error": "5", "critical": "5"}[level]


__all__ = ["Level", "Notifier"]
