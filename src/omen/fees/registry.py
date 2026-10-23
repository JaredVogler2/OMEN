"""Which fee terms apply to a market right now, built from live API data.

Resolution order for a market in series S, event E at time t:
  1. S's fee_type / fee_multiplier from GET /series (snapshot taken at time s0);
  2. the latest series fee change with s0 < scheduled_ts <= t replaces it (catches changes since the snapshot);
  3. the latest event override for E with scheduled_ts <= t replaces type and/or multiplier (null = cleared);
  4. guard: if a change for S or E takes effect within ``guard`` of t, the more expensive of the two terms is used,
     because an order placed a few minutes after the alert must still be profitable.
Unknown fee types raise FeeUnavailable (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from omen.fees.schedule import FeeQuote, FeeSchedule, FeeUnavailable
from omen.kalshi.models import Series, parse_ts
from omen.money import dec

PROBE_PRICE = Decimal("0.5")
PROBE_COUNT = Decimal("100")


@dataclass(frozen=True)
class ScheduledChange:
    id: str
    scope: str  # "series" | "event"
    ticker: str  # series ticker or event ticker
    series_ticker: str
    fee_type: str | None
    multiplier: Decimal | None
    scheduled: datetime

    @classmethod
    def from_series_row(cls, row: dict) -> ScheduledChange:
        return cls(
            id=str(row["id"]),
            scope="series",
            ticker=row["series_ticker"],
            series_ticker=row["series_ticker"],
            fee_type=row.get("fee_type"),
            multiplier=dec(row["fee_multiplier"]) if row.get("fee_multiplier") is not None else None,
            scheduled=parse_ts(row["scheduled_ts"]),  # type: ignore[arg-type]
        )

    @classmethod
    def from_event_row(cls, row: dict) -> ScheduledChange:
        return cls(
            id=str(row["id"]),
            scope="event",
            ticker=row["event_ticker"],
            series_ticker=row.get("series_ticker") or row["event_ticker"].split("-", 1)[0],
            fee_type=row.get("fee_type_override"),
            multiplier=(
                dec(row["fee_multiplier_override"]) if row.get("fee_multiplier_override") is not None else None
            ),
            scheduled=parse_ts(row["scheduled_ts"]),  # type: ignore[arg-type]
        )

    def describe(self) -> str:
        if self.fee_type is None and self.multiplier is None:
            terms = "override cleared (back to series fees)"
        else:
            mult = f" ×{self.multiplier.normalize():f}" if self.multiplier is not None else ""
            terms = f"{self.fee_type or 'same type'}{mult}"
        return f"{self.scope} {self.ticker}: {terms} from {self.scheduled:%Y-%m-%d %H:%M} UTC"


@dataclass
class FeeRegistry:
    schedule: FeeSchedule
    guard: timedelta = timedelta(minutes=60)
    series: dict[str, Series] = field(default_factory=dict)
    series_snapshot_at: datetime | None = None
    series_changes: dict[str, list[ScheduledChange]] = field(default_factory=dict)
    event_changes: dict[str, list[ScheduledChange]] = field(default_factory=dict)

    def load_series(self, rows: list[Series], at: datetime) -> None:
        self.series = {s.ticker: s for s in rows}
        self.series_snapshot_at = at

    def load_changes(self, series_rows: list[dict], event_rows: list[dict]) -> list[ScheduledChange]:
        changes = [ScheduledChange.from_series_row(r) for r in series_rows]
        changes += [ScheduledChange.from_event_row(r) for r in event_rows]
        self.series_changes = {}
        self.event_changes = {}
        for ch in changes:
            bucket = self.series_changes if ch.scope == "series" else self.event_changes
            bucket.setdefault(ch.ticker, []).append(ch)
        for lst in (*self.series_changes.values(), *self.event_changes.values()):
            lst.sort(key=lambda c: c.scheduled)
        return changes

    # ------------------------------------------------------------------------------------------------------------
    def _terms_at(self, series_ticker: str, event_ticker: str | None, at: datetime) -> tuple[str, Decimal, str]:
        base = self.series.get(series_ticker)
        if base is None:
            raise FeeUnavailable(f"series {series_ticker} not in fee registry")
        fee_type, mult, source = base.fee_type, base.fee_multiplier, "series"
        snap = self.series_snapshot_at  # the snapshot already reflects every change scheduled before it was taken
        for ch in self.series_changes.get(series_ticker, []):
            if ch.scheduled <= at and (snap is None or ch.scheduled > snap):
                fee_type = ch.fee_type or fee_type
                mult = ch.multiplier if ch.multiplier is not None else mult
                source = "series change"
        if event_ticker:
            latest = None
            for ch in self.event_changes.get(event_ticker, []):
                if ch.scheduled <= at:
                    latest = ch
            if latest is not None and not (latest.fee_type is None and latest.multiplier is None):
                fee_type = latest.fee_type or fee_type
                mult = latest.multiplier if latest.multiplier is not None else mult
                source = "event override"
        return fee_type, mult, source

    def _quote(self, fee_type: str, mult: Decimal, source: str) -> FeeQuote:
        formula = self.schedule.formula(fee_type)
        if mult < 0:
            raise FeeUnavailable(f"negative fee multiplier {mult}")
        return FeeQuote(fee_type, mult, formula, self.schedule.rounding_increment, source)

    def quote(self, series_ticker: str, event_ticker: str | None, at: datetime) -> FeeQuote:
        now_q = self._quote(*self._terms_at(series_ticker, event_ticker, at))
        upcoming = [
            ch
            for ch in [*self.series_changes.get(series_ticker, []), *self.event_changes.get(event_ticker or "", [])]
            if at < ch.scheduled <= at + self.guard
        ]
        if not upcoming:
            return now_q
        later = max(ch.scheduled for ch in upcoming)
        later_type, later_mult, _ = self._terms_at(series_ticker, event_ticker, later)
        later_q = self._quote(later_type, later_mult, "scheduled change (guard)")
        probe_now = now_q.taker_fee(PROBE_PRICE, PROBE_COUNT)
        probe_later = later_q.taker_fee(PROBE_PRICE, PROBE_COUNT)
        return later_q if probe_later > probe_now else now_q

    def upcoming_changes(self, at: datetime, horizon: timedelta) -> list[ScheduledChange]:
        out = [
            ch
            for lst in (*self.series_changes.values(), *self.event_changes.values())
            for ch in lst
            if at < ch.scheduled <= at + horizon
        ]
        return sorted(out, key=lambda c: c.scheduled)

    def unknown_fee_types(self) -> set[str]:
        types = {s.fee_type for s in self.series.values()}
        types |= {c.fee_type for lst in self.series_changes.values() for c in lst if c.fee_type}
        types |= {c.fee_type for lst in self.event_changes.values() for c in lst if c.fee_type}
        return {t for t in types if t not in self.schedule.formulas}
