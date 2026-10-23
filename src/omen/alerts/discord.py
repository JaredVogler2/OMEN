"""Discord delivery: webhooks or the STONKS bot token (REST only, no Gateway), through a durable outbox.

Channel keys: ``plays`` (actionable plays), ``research`` (shadow-model signals), ``status`` (fees, health, digest).
A key that is not configured falls back to ``default``; a message with no route stays queued and is reported.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from omen.http import USER_AGENT
from omen.ledger import Ledger

log = logging.getLogger(__name__)

CHANNELS = ("plays", "research", "status")
WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{5,30}/[A-Za-z0-9_.\-]{20,200}$"
)
API = "https://discord.com/api/v10"
MAX_ATTEMPTS = 8


class DiscordConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Route:
    kind: str  # "webhook" | "bot"
    target: str  # webhook URL or channel id

    def redacted(self) -> str:
        if self.kind == "webhook":
            return re.sub(r"/[^/]+$", "/***", self.target)
        return f"channel {self.target}"


def build_routes(webhooks: dict[str, str], bot_token: str | None, channel_map: dict[str, str]) -> dict[str, Route]:
    routes: dict[str, Route] = {}
    allowed = set(CHANNELS) | {"default"}
    for key, url in webhooks.items():
        if key not in allowed:
            raise DiscordConfigError(f"unknown Discord channel key {key!r} (use {', '.join(sorted(allowed))})")
        if not WEBHOOK_RE.match(url.strip()):
            raise DiscordConfigError(f"Discord webhook for {key!r} is not a discord.com/api/webhooks URL")
        routes[key] = Route("webhook", url.strip())
    if channel_map:
        if not bot_token:
            raise DiscordConfigError("OMEN_DISCORD_CHANNEL_MAP is set but DISCORD_BOT_TOKEN is not")
        for key, cid in channel_map.items():
            if key not in allowed:
                raise DiscordConfigError(f"unknown Discord channel key {key!r}")
            if not cid.isdigit():
                raise DiscordConfigError(f"Discord channel id for {key!r} must be numeric")
            routes.setdefault(key, Route("bot", cid))
    return routes


def resolve(routes: dict[str, Route], channel: str) -> Route | None:
    return routes.get(channel) or routes.get("default")


class DiscordTransport:
    """Minimal REST client. ``send`` returns the message id; ``edit`` updates an earlier message."""

    def __init__(
        self,
        bot_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.bot_token = bot_token
        self.sleep = sleep
        self.client = httpx.Client(timeout=20, transport=transport, headers={"User-Agent": USER_AGENT})

    def _headers(self, route: Route) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if route.kind == "bot":
            h["Authorization"] = f"Bot {self.bot_token}"
        return h

    def _call(self, method: str, url: str, route: Route, payload: dict) -> httpx.Response:
        for attempt in range(3):
            resp = self.client.request(method, url, headers=self._headers(route), content=json.dumps(payload))
            if resp.status_code == 429 and attempt < 2:
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except (ValueError, json.JSONDecodeError):
                    wait = float(resp.headers.get("Retry-After", "1"))
                self.sleep(min(max(wait, 0.5), 30.0))
                continue
            return resp
        return resp

    def send(self, route: Route, payload: dict) -> str | None:
        if route.kind == "webhook":
            resp = self._call("POST", f"{route.target}?wait=true", route, payload)
        else:
            resp = self._call("POST", f"{API}/channels/{route.target}/messages", route, payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"Discord {route.redacted()} -> HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return str(resp.json().get("id")) if resp.content else None
        except json.JSONDecodeError:
            return None

    def edit(self, route: Route, message_id: str, payload: dict) -> None:
        if route.kind == "webhook":
            url = f"{route.target}/messages/{message_id}"
        else:
            url = f"{API}/channels/{route.target}/messages/{message_id}"
        resp = self._call("PATCH", url, route, payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"Discord edit {route.redacted()} -> HTTP {resp.status_code}: {resp.text[:200]}")


class Notifier:
    """Queue messages in the ledger outbox and deliver them; failures retry on later cycles (max 8 attempts)."""

    def __init__(
        self, ledger: Ledger, routes: dict[str, Route], transport: DiscordTransport | None, dry_run: bool = False
    ):
        self.ledger = ledger
        self.routes = routes
        self.transport = transport
        self.dry_run = dry_run
        self.printed: list[dict] = []

    @property
    def configured(self) -> bool:
        return bool(self.routes) and self.transport is not None

    def queue(
        self, channel: str, payload: dict, purpose: str, ref: str | None, now: datetime, edit_of: str | None = None
    ) -> None:
        payload = {**payload, "allowed_mentions": {"parse": []}}
        if self.dry_run:
            self.printed.append({"channel": channel, "purpose": purpose, "payload": payload})
            return
        if resolve(self.routes, channel) is None:
            log.debug("no Discord route for %s; %s kept in the ledger only", channel, purpose)
            return
        self.ledger.enqueue(channel, payload, purpose, ref, now, edit_of)

    def recent_count(self, channel: str, purpose: str, since: datetime) -> int:
        row = self.ledger.db.execute(
            "SELECT COUNT(*) n FROM outbox WHERE channel=? AND purpose=? AND created_at >= ?",
            (channel, purpose, since.isoformat(timespec="seconds")),
        ).fetchone()
        return int(row["n"])

    def flush(self, now: datetime) -> tuple[int, int]:
        sent = failed = 0
        if self.dry_run:
            return 0, 0
        for row in self.ledger.pending_outbox(MAX_ATTEMPTS):
            route = resolve(self.routes, row["channel"])
            if route is None or self.transport is None:
                continue
            payload = json.loads(row["payload"])
            try:
                if row["edit_of"]:
                    self.transport.edit(route, row["edit_of"], payload)
                    mid = row["edit_of"]
                else:
                    mid = self.transport.send(route, payload)
                self.ledger.outbox_sent(row["id"], mid, now)
                if row["purpose"] == "play" and row["ref"] and mid:
                    self.ledger.set_alert_message(row["ref"], mid)
                sent += 1
            except Exception as exc:  # network errors, 4xx/5xx: keep in the outbox
                log.warning("Discord delivery failed (%s): %s", row["purpose"], exc)
                self.ledger.outbox_failed(row["id"], str(exc))
                failed += 1
        return sent, failed
