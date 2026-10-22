"""Collector for Google Skills public profiles. Rate-limited, retrying, never raises on fetch."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.collectors.base import (
    STATUS_FETCH_ERROR,
    STATUS_NOT_FOUND,
    STATUS_PARSE_ERROR,
    ProfileRef,
    ProfileResult,
)
from app.collectors.skills_parser import ParseError, parse_profile
from app.config import Settings, get_settings
from app.db import utcnow
from app.models import PLATFORM_SKILLS

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 120.0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class SkillsGoogleCollector:
    platform = PLATFORM_SKILLS

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._semaphore = asyncio.Semaphore(self.settings.fetch_concurrency)
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=self.settings.fetch_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SkillsGoogleCollector":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _backoff(self, attempt: int) -> float:
        base = self.settings.fetch_backoff_base
        return base * (2 ** (attempt - 1)) + random.uniform(0, base)

    async def fetch(self, profile: ProfileRef) -> ProfileResult:
        async with self._semaphore:
            try:
                return await self._fetch_with_retries(profile)
            finally:
                if self.settings.fetch_delay_seconds > 0:
                    await asyncio.sleep(self.settings.fetch_delay_seconds)

    async def _fetch_with_retries(self, profile: ProfileRef) -> ProfileResult:
        max_attempts = 1 + max(0, self.settings.fetch_max_retries)
        last_error = "unknown error"
        last_status: int | None = None
        for attempt in range(1, max_attempts + 1):
            wait: float | None = None
            try:
                response = await self._client.get(profile.url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}".rstrip(": ")
                last_status = None
            else:
                last_status = response.status_code
                if response.status_code == 404:
                    return ProfileResult(
                        status=STATUS_NOT_FOUND, url=profile.url, http_status=404,
                        error="HTTP 404", attempts=attempt, fetched_at=utcnow(),
                    )
                if response.status_code == 200:
                    try:
                        result = parse_profile(response.text, profile.url)
                    except ParseError as exc:
                        return ProfileResult(
                            status=STATUS_PARSE_ERROR, url=profile.url, http_status=200,
                            error=str(exc), attempts=attempt, fetched_at=utcnow(),
                        )
                    result.http_status = 200
                    result.attempts = attempt
                    result.fetched_at = utcnow()
                    return result
                last_error = f"HTTP {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    break  # other 4xx: not retryable
                retry_after = _retry_after_seconds(response)
                if retry_after is not None:
                    wait = min(retry_after, MAX_RETRY_AFTER_SECONDS)

            if attempt < max_attempts:
                delay = max(wait or 0.0, self._backoff(attempt))
                log.info("Retrying %s in %.1fs (attempt %d, %s)", profile.url, delay, attempt, last_error)
                await asyncio.sleep(delay)

        return ProfileResult(
            status=STATUS_FETCH_ERROR, url=profile.url, http_status=last_status,
            error=last_error, attempts=attempt, fetched_at=utcnow(),
        )
