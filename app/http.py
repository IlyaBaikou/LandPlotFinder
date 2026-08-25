from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import httpx

LOGGER = logging.getLogger(__name__)


class PublicPageClient:
    def __init__(
        self,
        timeout_seconds: float = 30,
        retries: int = 3,
        delay_seconds: float = 0.8,
        user_agent: Optional[str] = None,
    ) -> None:
        self.retries = retries
        self.delay_seconds = delay_seconds
        self._last_request_at = 0.0
        self._metrics = {"requests": 0, "retries": 0, "rate_limits": 0}
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent
                or "Mozilla/5.0 (compatible; LandPlotFinder/0.1; personal-use)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PublicPageClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_text(
        self,
        url: str,
        *,
        min_delay_seconds: Optional[float] = None,
        retry_rate_limit: bool = False,
        rate_limit_pause_seconds: float = 60,
    ) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._respect_delay(min_delay_seconds)
            try:
                self._metrics["requests"] += 1
                response = self._client.get(url)
                if response.status_code == 429 and retry_rate_limit:
                    self._metrics["rate_limits"] += 1
                    last_error = SourceBlockedError(
                        f"{url} returned HTTP 429 after {attempt} attempts"
                    )
                    if attempt >= self.retries:
                        break
                    wait_seconds = _rate_limit_wait(
                        response.headers.get("Retry-After"),
                        rate_limit_pause_seconds,
                        attempt,
                    )
                    LOGGER.warning(
                        "Rate limit for %s; pausing %.0f seconds before attempt %s/%s",
                        url,
                        wait_seconds,
                        attempt + 1,
                        self.retries,
                    )
                    self._metrics["retries"] += 1
                    time.sleep(wait_seconds)
                    continue
                if response.status_code in {403, 429}:
                    raise SourceBlockedError(
                        f"{url} returned HTTP {response.status_code}; source scan stopped"
                    )
                if response.status_code in {404, 410}:
                    raise PageUnavailableError(
                        f"{url} returned HTTP {response.status_code}"
                    )
                response.raise_for_status()
                return response.text
            except SourceBlockedError:
                raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                wait_seconds = _temporary_wait(attempt)
                self._metrics["retries"] += 1
                LOGGER.warning(
                    "Temporary request error for %s (attempt %s/%s): %s",
                    url,
                    attempt,
                    self.retries,
                    exc,
                )
                time.sleep(wait_seconds)
        if isinstance(last_error, SourceBlockedError):
            raise last_error
        raise RuntimeError(f"Unable to fetch {url}: {last_error}")

    def get_json(self, url: str) -> Any:
        return self._request("GET", url).json()

    def post_json(self, url: str, payload: dict) -> dict:
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    def post_text_json(self, url: str, payload: str) -> dict:
        response = self._request(
            "POST",
            url,
            content=payload.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError(f"Expected JSON object from {url}")
        return value

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._respect_delay()
            try:
                self._metrics["requests"] += 1
                response = self._client.request(method, url, **kwargs)
                if response.status_code in {403, 429}:
                    if response.status_code == 429:
                        self._metrics["rate_limits"] += 1
                    raise SourceBlockedError(
                        f"{url} returned HTTP {response.status_code}; source scan stopped"
                    )
                response.raise_for_status()
                return response
            except SourceBlockedError:
                raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                wait_seconds = _temporary_wait(attempt)
                self._metrics["retries"] += 1
                LOGGER.warning(
                    "Temporary request error for %s (attempt %s/%s): %s",
                    url,
                    attempt,
                    self.retries,
                    exc,
                )
                time.sleep(wait_seconds)
        raise RuntimeError(f"Unable to fetch {url}: {last_error}")

    def metrics(self) -> dict:
        return dict(self._metrics)

    def _respect_delay(self, min_delay_seconds: Optional[float] = None) -> None:
        delay_seconds = max(self.delay_seconds, min_delay_seconds or 0)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay_seconds:
            time.sleep(delay_seconds - elapsed)
        self._last_request_at = time.monotonic()


class SourceBlockedError(RuntimeError):
    """Raised when the remote site explicitly blocks or rate-limits the scanner."""


class PageUnavailableError(RuntimeError):
    """Raised when a public listing page is confirmed absent."""


def _rate_limit_wait(
    retry_after: Optional[str],
    fallback_seconds: float,
    attempt: int,
) -> float:
    if retry_after:
        try:
            return max(1.0, min(float(retry_after), 300.0))
        except ValueError:
            pass
    return min(max(1.0, fallback_seconds) * attempt, 180.0)


def _temporary_wait(attempt: int) -> float:
    """Exponential backoff with light jitter to avoid synchronized retries."""
    base = min(2 ** (attempt - 1), 16)
    return base + random.uniform(0, min(1.0, base * 0.2))
