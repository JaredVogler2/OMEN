"""Polite HTTP: per-client rate limiting, retries with backoff on 429/5xx/network errors, gzip, timeouts."""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from omen import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"omen-scanner/{__version__} (+private research bot)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimiter:
    """Token bucket: ``rate`` requests per second with a burst of ``burst``."""

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate = max(rate, 0.01)
        self.capacity = burst if burst is not None else max(1.0, rate)
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                self.sleep((1 - self.tokens) / self.rate)


class HttpClient:
    def __init__(
        self,
        base_url: str = "",
        rate_per_second: float = 8.0,
        timeout: float = 30.0,
        max_retries: int = 6,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        headers: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.sleep = sleep
        self.limiter = RateLimiter(rate_per_second, sleep=sleep)
        self.client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
            follow_redirects=False,
        )
        self.requests_made = 0

    def close(self) -> None:
        self.client.close()

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"

    def request(
        self,
        method: str,
        path: str,
        params: Any = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        header_factory: Callable[[str, str], dict[str, str]] | None = None,
        retry_on_post: bool = False,
    ) -> httpx.Response:
        url = self.url(path)
        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire()
            hdrs = dict(headers or {})
            if header_factory is not None:
                # signatures carry a timestamp, so they are rebuilt on every attempt
                hdrs.update(header_factory(method, url))
            try:
                self.requests_made += 1
                resp = self.client.request(method, url, params=params, json=json_body, headers=hdrs)
            except httpx.TransportError as exc:
                if attempt > self.max_retries or (method != "GET" and not retry_on_post):
                    raise HttpError(f"{method} {url}: {exc}") from exc
                self._backoff(attempt, None, f"network error: {exc}")
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retryable = method == "GET" or retry_on_post
                if retryable and attempt <= self.max_retries:
                    self._backoff(attempt, resp.headers.get("Retry-After"), f"HTTP {resp.status_code}")
                    continue
            if resp.status_code >= 400:
                raise HttpError(f"{method} {url} -> HTTP {resp.status_code}", resp.status_code, resp.text[:500])
            return resp

    def get_json(self, path: str, params: Any = None, **kw: Any) -> Any:
        return self.request("GET", path, params=params, **kw).json()

    def _backoff(self, attempt: int, retry_after: str | None, why: str) -> None:
        delay = min(30.0, 0.75 * (2 ** (attempt - 1))) * (0.75 + random.random() * 0.5)
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        log.debug("retrying in %.1fs (%s)", delay, why)
        self.sleep(delay)
