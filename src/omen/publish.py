"""Push the Pi's live state to the OMEN website (POST /api/ingest), signed with a shared secret.

The website never calls the Pi: the Pi pushes a snapshot (live plays, activity, paper ledger, models, runs, fee
outlook) after a cycle whenever the plays changed, and at least every ``publish_interval_seconds`` as a heartbeat.
Publishing is best-effort: a failure is logged and retried later, it never interrupts scanning or alerts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from omen.snapshot import build_feed

log = logging.getLogger(__name__)

MAX_BYTES = 900_000  # the website rejects snapshots over 950 KB
MIN_GAP_SECONDS = 20  # never publish more often than this, even when plays change


def sign(secret: str, ts: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def fit(feed: dict[str, Any], limit: int = MAX_BYTES) -> bytes:
    """Serialize ``feed``, shedding history (never live plays) until it fits the website's size limit."""
    trims: list[tuple[str, ...]] = [
        ("paper", "settled"),
        ("activity",),
        ("runs",),
        ("paper", "open"),
        ("fees", "upcoming"),
    ]
    while True:
        body = json.dumps(feed, default=str, separators=(",", ":")).encode()
        if len(body) <= limit or not trims:
            return body
        path = trims[0]
        parent = feed
        for key in path[:-1]:
            parent = parent[key]
        items = parent[path[-1]]
        if len(items) > 4:
            parent[path[-1]] = items[: len(items) // 2]
        else:
            trims.pop(0)


class Publisher:
    def __init__(
        self,
        url: str,
        secret: str,
        interval_seconds: int = 120,
        bypass: str | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.endpoint = url.rstrip("/") + "/api/ingest"
        self.secret = secret
        self.interval = interval_seconds
        self.bypass = bypass
        self.clock = clock
        self.client = httpx.Client(timeout=20, transport=transport, follow_redirects=False)
        self.last_sent: float | None = None
        self.last_signature: str | None = None
        self.failures = 0
        self.retry_after = 0.0
        self.last_error: str | None = None

    @classmethod
    def from_settings(cls, s: Any) -> Publisher | None:
        if not s.web_url or not s.ingest_secret:
            return None
        return cls(s.web_url, s.ingest_secret, s.publish_interval_seconds, s.vercel_bypass)

    @staticmethod
    def signature(feed: dict[str, Any]) -> str:
        """What a viewer would notice changing: the set of live plays and their profit to the cent."""
        plays = sorted((p.get("key", ""), round(p.get("profit") or 0, 2)) for p in feed.get("plays", []))
        return hashlib.sha256(json.dumps(plays).encode()).hexdigest()

    def due(self, signature: str) -> bool:
        t = self.clock()
        if t < self.retry_after:
            return False
        if self.last_sent is None:
            return True
        elapsed = t - self.last_sent
        if elapsed < MIN_GAP_SECONDS:
            return False
        return signature != self.last_signature or elapsed >= self.interval

    def maybe_publish(self, ledger: Any, engine: Any, models: list[Any], settings: Any, now: datetime) -> bool:
        feed = build_feed(ledger, engine, models, settings, now)
        sig = self.signature(feed)
        if not self.due(sig):
            return False
        return self.send(feed, sig)

    def send(self, feed: dict[str, Any], signature: str | None = None) -> bool:
        body = fit(feed)
        ts = int(self.clock())
        headers = {
            "content-type": "application/json",
            "x-omen-ts": str(ts),
            "x-omen-sig": sign(self.secret, ts, body),
        }
        if self.bypass:
            headers["x-vercel-protection-bypass"] = self.bypass
        try:
            resp = self.client.post(self.endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return self._failed(f"network error: {exc}")
        if resp.status_code != 200:
            detail = resp.text[:200].replace("\n", " ")
            return self._failed(f"HTTP {resp.status_code}: {detail}")
        self.last_sent = self.clock()
        self.last_signature = signature or self.signature(feed)
        self.failures = 0
        self.retry_after = 0.0
        self.last_error = None
        log.debug("published %d bytes to %s", len(body), self.endpoint)
        return True

    def _failed(self, why: str) -> bool:
        self.failures += 1
        self.last_error = why
        # back off 1, 2, 4 ... up to 30 minutes so a misconfigured site isn't hammered
        self.retry_after = self.clock() + min(1800, 60 * 2 ** (self.failures - 1))
        level = logging.WARNING if self.failures in (1, 5) or self.failures % 30 == 0 else logging.DEBUG
        log.log(level, "website publish failed (%d in a row): %s", self.failures, why)
        return False
