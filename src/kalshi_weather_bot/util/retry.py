from __future__ import annotations

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential
import httpx


def http_retry() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        reraise=True,
    )
