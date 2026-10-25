"""Kalshi Trade API v2 client (public market data + optional signed order entry)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

from omen.http import HttpClient
from omen.kalshi.auth import KalshiSigner
from omen.kalshi.models import Event, Market, OrderBook, Series

log = logging.getLogger(__name__)

MAX_MARKETS_PAGE = 1000
MAX_EVENTS_PAGE = 200
MAX_ORDERBOOK_BATCH = 100
MAX_TICKERS_PER_QUERY = 100


def chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class KalshiClient:
    def __init__(self, http: HttpClient, signer: KalshiSigner | None = None):
        self.http = http
        self.signer = signer

    # ----------------------------------------------------------------------------------------------- exchange
    def exchange_status(self) -> dict:
        return self.http.get_json("/exchange/status")

    # ------------------------------------------------------------------------------------------------- series
    def all_series(self) -> list[Series]:
        """Every series with its current fee_type / fee_multiplier (one request, ~15k rows)."""
        data = self.http.get_json("/series")
        return [Series.from_api(s) for s in data.get("series") or []]

    def series(self, ticker: str) -> Series:
        return Series.from_api(self.http.get_json(f"/series/{ticker}")["series"])

    def series_fee_changes(self, show_historical: bool = False) -> list[dict]:
        params = {"show_historical": "true"} if show_historical else None
        return list(self.http.get_json("/series/fee_changes", params=params).get("series_fee_change_arr") or [])

    def event_fee_changes(self) -> list[dict]:
        out: list[dict] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = self.http.get_json("/events/fee_changes", params=params)
            out.extend(data.get("event_fee_changes") or [])
            cursor = data.get("cursor") or ""
            if not cursor:
                return out

    # ------------------------------------------------------------------------------------------------- events
    def iter_events(self, **params: Any) -> Iterator[Event]:
        for raw in self._paged("/events", "events", MAX_EVENTS_PAGE, params):
            yield Event.from_api(raw)

    def events_by_ticker(self, tickers: Iterable[str]) -> list[Event]:
        out: list[Event] = []
        for batch in chunks(sorted(set(tickers)), MAX_TICKERS_PER_QUERY):
            out.extend(self.iter_events(tickers=",".join(batch)))
        return out

    # ------------------------------------------------------------------------------------------------ markets
    def iter_markets(self, **params: Any) -> Iterator[Market]:
        for raw in self._paged("/markets", "markets", MAX_MARKETS_PAGE, params):
            yield Market.from_api(raw)

    def markets_by_ticker(self, tickers: Iterable[str]) -> list[Market]:
        out: list[Market] = []
        for batch in chunks(sorted(set(tickers)), MAX_TICKERS_PER_QUERY):
            out.extend(self.iter_markets(tickers=",".join(batch)))
        return out

    def orderbooks(self, tickers: Iterable[str]) -> dict[str, OrderBook]:
        books: dict[str, OrderBook] = {}
        for batch in chunks(sorted(set(tickers)), MAX_ORDERBOOK_BATCH):
            data = self.http.get_json("/markets/orderbooks", params=[("tickers", t) for t in batch])
            for row in data.get("orderbooks") or []:
                books[row["ticker"]] = OrderBook.from_api(row["ticker"], row.get("orderbook_fp") or {})
        return books

    # ------------------------------------------------------------------------------------------ signed (exec)
    def _signed(self, method: str, path: str, body: Any = None, params: Any = None, retry: bool = False) -> Any:
        """``retry``: also retry a non-GET on 429/5xx/network errors (only for idempotent calls such as cancels)."""
        if self.signer is None:
            raise RuntimeError("no Kalshi API key configured")
        resp = self.http.request(
            method, path, params=params, json_body=body, header_factory=self.signer.headers, retry_on_post=retry
        )
        return resp.json() if resp.content else {}

    def balance(self) -> dict:
        return self._signed("GET", "/portfolio/balance")

    def create_order(self, order: dict) -> dict:
        """POST /portfolio/events/orders (V2: YES-side book, ``bid`` = buy YES, ``ask`` = sell YES / buy NO)."""
        return self._signed("POST", "/portfolio/events/orders", body=order)

    def positions(self, **params: Any) -> dict:
        return self._signed("GET", "/portfolio/positions", params=params or None)

    # ---------------------------------------------------------------------------------------------- internals
    def _paged(self, path: str, key: str, page_size: int, params: dict[str, Any]) -> Iterator[dict]:
        cursor = ""
        pages = 0
        while True:
            q = {k: v for k, v in params.items() if v is not None}
            q["limit"] = page_size
            if cursor:
                q["cursor"] = cursor
            data = self.http.get_json(path, params=q)
            pages += 1
            yield from data.get(key) or []
            cursor = data.get("cursor") or ""
            if not cursor:
                log.debug("%s: %d pages", path, pages)
                return
