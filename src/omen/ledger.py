"""SQLite ledger: event cache, fee audit trail, opportunity history, paper positions, model predictions, Discord
outbox and run log. One file, WAL mode, safe to copy while running (``omen backup``)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from omen.kalshi.models import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_ticker TEXT PRIMARY KEY, series_ticker TEXT, title TEXT, sub_title TEXT, category TEXT,
  mutually_exclusive INTEGER NOT NULL, collateral_return_type TEXT, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fee_terms (
  series_ticker TEXT PRIMARY KEY, fee_type TEXT NOT NULL, fee_multiplier TEXT NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fee_changes_seen (
  id TEXT PRIMARY KEY, scope TEXT, ticker TEXT, fee_type TEXT, multiplier TEXT, scheduled_ts TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS fee_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, ticker TEXT,
  detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS opportunities (
  key TEXT PRIMARY KEY, short_id TEXT NOT NULL, strategy TEXT NOT NULL, tier TEXT NOT NULL, event_ticker TEXT,
  title TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, gone_at TEXT, times_seen INTEGER NOT NULL DEFAULT 1,
  best_profit TEXT, last_profit TEXT, last_payload TEXT,
  alerted_at TEXT, alert_profit TEXT, alert_message_id TEXT, alert_channel TEXT);
CREATE INDEX IF NOT EXISTS opp_live ON opportunities(gone_at);
CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY, opportunity_key TEXT NOT NULL, short_id TEXT, strategy TEXT, tier TEXT,
  opened_at TEXT NOT NULL, settles_at TEXT, sets TEXT, capital TEXT, fees TEXT, expected_profit TEXT,
  payout_min TEXT, legs_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', settled_at TEXT, payout TEXT,
  pnl TEXT);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY, model TEXT NOT NULL, ticker TEXT NOT NULL, event_ticker TEXT, ts TEXT NOT NULL,
  side TEXT NOT NULL, p_model REAL NOT NULL, p_conservative REAL NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
  edge REAL NOT NULL, would_alert INTEGER NOT NULL, close_time TEXT, outcome REAL, settled_at TEXT,
  features_json TEXT);
CREATE INDEX IF NOT EXISTS pred_open ON predictions(outcome, close_time);
CREATE TABLE IF NOT EXISTS model_status (model TEXT PRIMARY KEY, state TEXT NOT NULL, changed_at TEXT NOT NULL,
  note TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, markets INTEGER, candidates INTEGER,
  opportunities INTEGER, alerts INTEGER, requests INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY, opportunity_key TEXT, mode TEXT, ts TEXT, leg_ticker TEXT, request_json TEXT,
  response_json TEXT, status TEXT, cost TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL, purpose TEXT, ref TEXT,
  created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT, message_id TEXT,
  edit_of TEXT);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent_at);
CREATE TABLE IF NOT EXISTS lp_fills (
  id INTEGER PRIMARY KEY, mode TEXT NOT NULL, ts TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
  price REAL NOT NULL, qty REAL NOT NULL, fee REAL NOT NULL, order_id TEXT, trade_id TEXT,
  UNIQUE(mode, trade_id, order_id, side));
CREATE TABLE IF NOT EXISTS lp_rewards (
  mode TEXT NOT NULL, hour TEXT NOT NULL, ticker TEXT NOT NULL, usd REAL NOT NULL, seconds REAL NOT NULL,
  PRIMARY KEY(mode, hour, ticker));
CREATE TABLE IF NOT EXISTS lp_settlements (
  mode TEXT NOT NULL, ticker TEXT NOT NULL, result TEXT NOT NULL, ts TEXT NOT NULL, cost REAL NOT NULL,
  payout REAL NOT NULL, PRIMARY KEY(mode, ticker));
CREATE TABLE IF NOT EXISTS lp_cash (
  mode TEXT NOT NULL, ts TEXT NOT NULL, balance REAL NOT NULL, holds REAL NOT NULL, PRIMARY KEY(mode, ts));
CREATE TABLE IF NOT EXISTS lp_pending (
  mode TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL, client_order_id TEXT NOT NULL, sent TEXT NOT NULL,
  PRIMARY KEY(mode, ticker, side));
CREATE TABLE IF NOT EXISTS lp_programs (
  mode TEXT NOT NULL, ticker TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, reward_usd REAL NOT NULL,
  PRIMARY KEY(mode, ticker, start));
CREATE TABLE IF NOT EXISTS lp_orders (
  mode TEXT NOT NULL, order_id TEXT NOT NULL, client_order_id TEXT, ticker TEXT NOT NULL, side TEXT NOT NULL,
  price REAL NOT NULL, placed TEXT NOT NULL, PRIMARY KEY(mode, order_id));
"""


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the website handles requests on worker threads and serializes engine use itself
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # ------------------------------------------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )

    # ---------------------------------------------------------------------------------------------------- events
    def upsert_events(self, events: Iterable[Event], now: datetime) -> None:
        rows = [
            (
                e.event_ticker,
                e.series_ticker,
                e.title,
                e.sub_title,
                e.category,
                int(e.mutually_exclusive),
                e.collateral_return_type,
                iso(now),
            )
            for e in events
        ]
        with self.tx() as db:
            db.executemany(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_ticker) DO UPDATE SET "
                "series_ticker=excluded.series_ticker, title=excluded.title, sub_title=excluded.sub_title, "
                "category=excluded.category, mutually_exclusive=excluded.mutually_exclusive, "
                "collateral_return_type=excluded.collateral_return_type, fetched_at=excluded.fetched_at",
                rows,
            )

    def load_events(self) -> dict[str, Event]:
        out = {}
        for r in self.db.execute("SELECT * FROM events"):
            out[r["event_ticker"]] = Event(
                r["event_ticker"],
                r["series_ticker"] or "",
                r["title"] or "",
                r["sub_title"] or "",
                r["category"] or "",
                bool(r["mutually_exclusive"]),
                r["collateral_return_type"] or "",
            )
        return out

    # ------------------------------------------------------------------------------------------------------ fees
    def diff_fee_terms(self, terms: dict[str, tuple[str, Decimal]], now: datetime) -> list[tuple[str, str, str]]:
        """Store the current per-series terms; return (series, old, new) for every series whose terms changed."""
        old = {
            r["series_ticker"]: (r["fee_type"], r["fee_multiplier"]) for r in self.db.execute("SELECT * FROM fee_terms")
        }
        first_run = not old
        changes = []
        rows = []
        for series, (fee_type, mult) in terms.items():
            new = (fee_type, f"{mult.normalize():f}")
            prev = old.get(series)
            if prev is not None and tuple(prev) != new:
                changes.append((series, f"{prev[0]} ×{prev[1]}", f"{new[0]} ×{new[1]}"))
            rows.append((series, new[0], new[1], iso(now), iso(now)))
        with self.tx() as db:
            db.executemany(
                "INSERT INTO fee_terms VALUES(?,?,?,?,?) ON CONFLICT(series_ticker) DO UPDATE SET "
                "fee_type=excluded.fee_type, fee_multiplier=excluded.fee_multiplier, last_seen=excluded.last_seen",
                rows,
            )
            for series, before, after in changes:
                db.execute(
                    "INSERT INTO fee_log(ts,kind,ticker,detail) VALUES(?,?,?,?)",
                    (iso(now), "series_terms_changed", series, f"{before} -> {after}"),
                )
            if first_run:
                db.execute(
                    "INSERT INTO fee_log(ts,kind,ticker,detail) VALUES(?,?,?,?)",
                    (iso(now), "baseline", None, f"recorded fee terms for {len(rows)} series"),
                )
        return changes

    def new_fee_changes(self, changes: Iterable[Any], now: datetime) -> list[Any]:
        """Record scheduled fee changes; return the ones never seen before."""
        seen = {r["id"] for r in self.db.execute("SELECT id FROM fee_changes_seen")}
        fresh = [c for c in changes if c.id not in seen]
        with self.tx() as db:
            for c in fresh:
                db.execute(
                    "INSERT INTO fee_changes_seen VALUES(?,?,?,?,?,?,?)",
                    (
                        c.id,
                        c.scope,
                        c.ticker,
                        c.fee_type,
                        None if c.multiplier is None else str(c.multiplier),
                        iso(c.scheduled),
                        iso(now),
                    ),
                )
                if seen:  # the very first load is a baseline, not news
                    db.execute(
                        "INSERT INTO fee_log(ts,kind,ticker,detail) VALUES(?,?,?,?)",
                        (iso(now), "scheduled_change", c.ticker, c.describe()),
                    )
        return fresh if seen else []

    def log_fee(self, kind: str, ticker: str | None, detail: str, now: datetime) -> None:
        self.db.execute("INSERT INTO fee_log(ts,kind,ticker,detail) VALUES(?,?,?,?)", (iso(now), kind, ticker, detail))

    def fee_log(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM fee_log ORDER BY id DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------------------------------------------- opportunities
    def observe(
        self,
        key: str,
        short_id: str,
        strategy: str,
        tier: str,
        event_ticker: str,
        title: str,
        profit: Decimal,
        payload: dict,
        now: datetime,
    ) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM opportunities WHERE key=?", (key,)).fetchone()
        body = json.dumps(payload, default=str)
        if row is None:
            self.db.execute(
                "INSERT INTO opportunities(key,short_id,strategy,tier,event_ticker,title,first_seen,last_seen,"
                "best_profit,last_profit,last_payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    short_id,
                    strategy,
                    tier,
                    event_ticker,
                    title,
                    iso(now),
                    iso(now),
                    str(profit),
                    str(profit),
                    body,
                ),
            )
        else:
            best = max(Decimal(row["best_profit"]), profit)
            # a play that disappeared and came back counts as fresh (first_seen kept for the audit trail)
            self.db.execute(
                "UPDATE opportunities SET last_seen=?, gone_at=NULL, times_seen=times_seen+1, best_profit=?, "
                "last_profit=?, last_payload=?, tier=? WHERE key=?",
                (iso(now), str(best), str(profit), body, tier, key),
            )
        return self.db.execute("SELECT * FROM opportunities WHERE key=?", (key,)).fetchone()

    def mark_alerted(self, key: str, profit: Decimal, channel: str, now: datetime) -> None:
        self.db.execute(
            "UPDATE opportunities SET alerted_at=?, alert_profit=?, alert_channel=? WHERE key=?",
            (iso(now), str(profit), channel, key),
        )

    def set_alert_message(self, key: str, message_id: str) -> None:
        self.db.execute("UPDATE opportunities SET alert_message_id=? WHERE key=?", (message_id, key))

    def live_opportunities(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM opportunities WHERE gone_at IS NULL"))

    def mark_gone(self, key: str, now: datetime) -> None:
        self.db.execute("UPDATE opportunities SET gone_at=? WHERE key=?", (iso(now), key))

    # -------------------------------------------------------------------------------------------------- paper
    def open_paper(
        self,
        key: str,
        short_id: str,
        strategy: str,
        tier: str,
        now: datetime,
        settles_at: datetime,
        sets: Decimal,
        capital: Decimal,
        fees: Decimal,
        expected_profit: Decimal,
        payout_min: Decimal,
        legs: list[dict],
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO paper_positions(opportunity_key,short_id,strategy,tier,opened_at,settles_at,sets,capital,"
            "fees,expected_profit,payout_min,legs_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                short_id,
                strategy,
                tier,
                iso(now),
                iso(settles_at),
                str(sets),
                str(capital),
                str(fees),
                str(expected_profit),
                str(payout_min),
                json.dumps(legs, default=str),
            ),
        )
        return int(cur.lastrowid or 0)

    def has_open_paper(self, key: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM paper_positions WHERE opportunity_key=? AND status='open'", (key,)
            ).fetchone()
            is not None
        )

    def open_paper_positions(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM paper_positions WHERE status='open'"))

    def settle_paper(self, pid: int, payout: Decimal, pnl: Decimal, now: datetime) -> None:
        self.db.execute(
            "UPDATE paper_positions SET status='settled', settled_at=?, payout=?, pnl=? WHERE id=?",
            (iso(now), str(payout), str(pnl), pid),
        )

    # ---------------------------------------------------------------------------------------------- predictions
    def last_prediction_ts(self, model: str, ticker: str) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(ts) t FROM predictions WHERE model=? AND ticker=?", (model, ticker)
        ).fetchone()
        return from_iso(row["t"]) if row and row["t"] else None

    def add_prediction(
        self,
        model: str,
        ticker: str,
        event_ticker: str,
        now: datetime,
        side: str,
        p_model: float,
        p_conservative: float,
        price: Decimal,
        fee: Decimal,
        edge: float,
        would_alert: bool,
        close_time: datetime,
        features: dict,
    ) -> None:
        self.db.execute(
            "INSERT INTO predictions(model,ticker,event_ticker,ts,side,p_model,p_conservative,price,fee,edge,"
            "would_alert,close_time,features_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                model,
                ticker,
                event_ticker,
                iso(now),
                side,
                p_model,
                p_conservative,
                str(price),
                str(fee),
                edge,
                int(would_alert),
                iso(close_time),
                json.dumps(features, default=str),
            ),
        )

    def unresolved_prediction_tickers(self, now: datetime) -> list[str]:
        return [
            r["ticker"]
            for r in self.db.execute(
                "SELECT DISTINCT ticker FROM predictions WHERE outcome IS NULL AND close_time <= ?", (iso(now),)
            )
        ]

    def resolve_predictions(self, ticker: str, yes_payout: Decimal, now: datetime) -> int:
        cur = self.db.execute(
            "UPDATE predictions SET outcome=?, settled_at=? WHERE ticker=? AND outcome IS NULL",
            (float(yes_payout), iso(now), ticker),
        )
        return cur.rowcount

    def resolved_predictions(self, model: str) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM predictions WHERE model=? AND outcome IS NOT NULL ORDER BY ts", (model,))
        )

    def model_state(self, model: str) -> str:
        row = self.db.execute("SELECT state FROM model_status WHERE model=?", (model,)).fetchone()
        return row["state"] if row else "shadow"

    def set_model_state(self, model: str, state: str, note: str, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO model_status VALUES(?,?,?,?) ON CONFLICT(model) DO UPDATE SET state=excluded.state,"
            " changed_at=excluded.changed_at, note=excluded.note",
            (model, state, iso(now), note),
        )

    # --------------------------------------------------------------------------------------------------- runs
    def start_run(self, kind: str, now: datetime) -> int:
        cur = self.db.execute("INSERT INTO runs(kind, started_at) VALUES(?,?)", (kind, iso(now)))
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, now: datetime, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE runs SET finished_at=?{', ' + cols if cols else ''} WHERE id=?",
            (iso(now), *fields.values(), run_id),
        )

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------------------------------------------------- outbox
    def enqueue(
        self, channel: str, payload: dict, purpose: str, ref: str | None, now: datetime, edit_of: str | None = None
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO outbox(channel,payload,purpose,ref,created_at,edit_of) VALUES(?,?,?,?,?,?)",
            (channel, json.dumps(payload), purpose, ref, iso(now), edit_of),
        )
        return int(cur.lastrowid or 0)

    def pending_outbox(
        self, max_attempts: int = 8, purposes: set[str] | None = None, exclude: set[str] | None = None
    ) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY id", (max_attempts,)
        )
        return [
            r
            for r in rows
            if (purposes is None or r["purpose"] in purposes) and (exclude is None or r["purpose"] not in exclude)
        ]

    def outbox_sent(self, oid: int, message_id: str | None, now: datetime) -> None:
        self.db.execute(
            "UPDATE outbox SET sent_at=?, message_id=?, attempts=attempts+1 WHERE id=?", (iso(now), message_id, oid)
        )

    def outbox_failed(self, oid: int, error: str) -> None:
        self.db.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (error[:500], oid))

    def outbox_stats(self) -> dict[str, int]:
        row = self.db.execute(
            "SELECT SUM(sent_at IS NULL AND attempts < 8) pending, SUM(sent_at IS NULL AND attempts >= 8) dead "
            "FROM outbox"
        ).fetchone()
        return {"pending": row["pending"] or 0, "dead": row["dead"] or 0}

    # --------------------------------------------------------------------------------------------- executions
    def record_execution(
        self, key: str, mode: str, now: datetime, ticker: str, request: dict, response: dict, status: str, cost: Decimal
    ) -> None:
        self.db.execute(
            "INSERT INTO executions(opportunity_key,mode,ts,leg_ticker,request_json,response_json,status,cost) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (key, mode, iso(now), ticker, json.dumps(request), json.dumps(response, default=str), status, str(cost)),
        )

    def spent_since(self, since: datetime, mode: str) -> Decimal:
        rows = self.db.execute(
            "SELECT cost FROM executions WHERE ts >= ? AND mode=? AND status IN ('filled','partial')",
            (iso(since), mode),
        )
        return sum((Decimal(r["cost"] or "0") for r in rows), Decimal(0))

    # ---------------------------------------------------------------------------------- liquidity provision
    def lp_record_fill(self, mode: str, fill: Any) -> bool:
        """Store a fill once; returns False if it was already recorded."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO lp_fills(mode,ts,ticker,side,price,qty,fee,order_id,trade_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (mode, iso(fill.ts), fill.ticker, fill.side, fill.price, fill.qty, fill.fee, fill.order_id, fill.trade_id),
        )
        return cur.rowcount > 0

    def lp_add_reward(self, mode: str, now: datetime, ticker: str, usd: float, seconds: float) -> None:
        hour = now.astimezone(UTC).strftime("%Y-%m-%dT%H:00")
        self.db.execute(
            "INSERT INTO lp_rewards(mode,hour,ticker,usd,seconds) VALUES(?,?,?,?,?) "
            "ON CONFLICT(mode,hour,ticker) DO UPDATE SET usd=usd+excluded.usd, seconds=seconds+excluded.seconds",
            (mode, hour, ticker, usd, seconds),
        )

    def lp_positions(self, mode: str) -> dict[str, dict[str, list[float]]]:
        """Unsettled inventory: ticker -> side -> [contracts, cost including fees]."""
        out: dict[str, dict[str, list[float]]] = {}
        rows = self.db.execute(
            "SELECT ticker, side, SUM(qty) q, SUM(qty*price + fee) c FROM lp_fills f WHERE mode=? AND NOT EXISTS "
            "(SELECT 1 FROM lp_settlements s WHERE s.mode=f.mode AND s.ticker=f.ticker) GROUP BY ticker, side",
            (mode,),
        )
        for r in rows:
            out.setdefault(r["ticker"], {"yes": [0.0, 0.0], "no": [0.0, 0.0]})[r["side"]] = [r["q"], r["c"]]
        return out

    def lp_settle(self, mode: str, ticker: str, yes_payout: float, now: datetime) -> float:
        """Settle our inventory in a finalized market (``yes_payout`` per YES contract, NO gets the rest)."""
        pos = self.lp_positions(mode).get(ticker)
        if not pos:
            return 0.0
        cost = pos["yes"][1] + pos["no"][1]
        payout = pos["yes"][0] * yes_payout + pos["no"][0] * (1 - yes_payout)
        self.db.execute(
            "INSERT OR IGNORE INTO lp_settlements(mode,ticker,result,ts,cost,payout) VALUES(?,?,?,?,?,?)",
            (mode, ticker, f"{yes_payout:.4f}", iso(now), cost, payout),
        )
        return payout - cost

    def lp_summary(self, mode: str, since: datetime | None = None) -> dict[str, float]:
        cut = iso(since) if since else ""
        hour_cut = since.astimezone(UTC).strftime("%Y-%m-%dT%H:00") if since else ""
        rew = self.db.execute(
            "SELECT COALESCE(SUM(usd),0) usd, COUNT(DISTINCT ticker) markets FROM lp_rewards "
            "WHERE mode=? AND hour >= ?",
            (mode, hour_cut),
        ).fetchone()
        fills = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(qty),0) q, COALESCE(SUM(qty*price),0) notional, COALESCE(SUM(fee),0) fee "
            "FROM lp_fills WHERE mode=? AND ts >= ?",
            (mode, cut),
        ).fetchone()
        settled = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(payout - cost),0) pnl FROM lp_settlements WHERE mode=? AND ts >= ?",
            (mode, cut),
        ).fetchone()
        return {
            "reward_usd": float(rew["usd"]),
            "reward_markets": float(rew["markets"]),
            "fills": float(fills["n"]),
            "contracts": float(fills["q"]),
            "notional": float(fills["notional"]),
            "fees": float(fills["fee"]),
            "settled_markets": float(settled["n"]),
            "settled_pnl": float(settled["pnl"]),
        }

    def lp_add_order(
        self, mode: str, order_id: str, client_order_id: str, ticker: str, side: str, price: float, placed: datetime
    ) -> None:
        """Every order the live venue placed or adopted: fills are ours only if their order id is here."""
        self.db.execute(
            "INSERT OR IGNORE INTO lp_orders(mode,order_id,client_order_id,ticker,side,price,placed) "
            "VALUES(?,?,?,?,?,?,?)",
            (mode, order_id, client_order_id, ticker, side, price, iso(placed)),
        )

    def lp_pending_add(self, mode: str, ticker: str, side: str, client_order_id: str, sent: datetime) -> None:
        """A create whose outcome is unknown (timeout, 5xx): looked up by client order id, even after a restart."""
        self.db.execute(
            "INSERT OR REPLACE INTO lp_pending(mode,ticker,side,client_order_id,sent) VALUES(?,?,?,?,?)",
            (mode, ticker, side, client_order_id, iso(sent)),
        )

    def lp_pending_done(self, mode: str, ticker: str, side: str) -> None:
        self.db.execute("DELETE FROM lp_pending WHERE mode=? AND ticker=? AND side=?", (mode, ticker, side))

    def lp_pending(self, mode: str) -> dict[tuple[str, str], tuple[str, datetime]]:
        rows = self.db.execute("SELECT ticker, side, client_order_id, sent FROM lp_pending WHERE mode=?", (mode,))
        return {(r["ticker"], r["side"]): (r["client_order_id"], datetime.fromisoformat(r["sent"])) for r in rows}

    def lp_order_ids(self, mode: str) -> set[str]:
        return {r["order_id"] for r in self.db.execute("SELECT order_id FROM lp_orders WHERE mode=?", (mode,))}

    def lp_last_activity(self, mode: str) -> datetime | None:
        """The latest fill, order or cash snapshot on record: where a restarted venue resumes reading fills."""
        row = self.db.execute(
            "SELECT MAX(t) t FROM (SELECT MAX(ts) t FROM lp_fills WHERE mode=? UNION ALL "
            "SELECT MAX(placed) FROM lp_orders WHERE mode=? UNION ALL SELECT MAX(ts) FROM lp_cash WHERE mode=?)",
            (mode, mode, mode),
        ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def lp_add_cash(self, mode: str, ts: datetime, balance: float, holds: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO lp_cash(mode,ts,balance,holds) VALUES(?,?,?,?)", (mode, iso(ts), balance, holds)
        )

    def lp_cash(self, mode: str, since: datetime | None = None) -> list[tuple[datetime, float, float]]:
        rows = self.db.execute(
            "SELECT ts, balance, holds FROM lp_cash WHERE mode=? AND ts >= ? ORDER BY ts",
            (mode, iso(since) if since else ""),
        )
        return [(datetime.fromisoformat(r["ts"]), float(r["balance"]), float(r["holds"])) for r in rows]

    def lp_reward_between(self, mode: str, start: datetime, end: datetime) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(usd),0) usd FROM lp_rewards WHERE mode=? AND hour >= ? AND hour < ?",
            (mode, start.astimezone(UTC).strftime("%Y-%m-%dT%H:00"), end.astimezone(UTC).strftime("%Y-%m-%dT%H:00")),
        ).fetchone()
        return float(row["usd"])

    def lp_record_programs(self, mode: str, programs: Iterable[Any]) -> None:
        """Remember each reward period quoted (Kalshi pays per period, a day after it ends)."""
        self.db.executemany(
            "INSERT OR IGNORE INTO lp_programs(mode,ticker,start,end,reward_usd) VALUES(?,?,?,?,?)",
            [(mode, p.ticker, iso(p.start), iso(p.end), p.reward_usd) for p in programs],
        )

    def lp_programs_ended(self, mode: str, after: datetime, until: datetime) -> list[tuple[str, datetime, datetime]]:
        """Reward periods that ended in (after, until]: (ticker, start, end)."""
        rows = self.db.execute(
            "SELECT ticker, start, end FROM lp_programs WHERE mode=? AND end > ? AND end <= ? ORDER BY end",
            (mode, iso(after), iso(until)),
        )
        return [(r["ticker"], datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])) for r in rows]

    def lp_reward_in_period(self, mode: str, ticker: str, start: datetime, end: datetime) -> float:
        """OMEN's estimate for one ticker over one reward period (hourly buckets, both ends included)."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(usd),0) usd FROM lp_rewards WHERE mode=? AND ticker=? AND hour >= ? AND hour <= ?",
            (
                mode,
                ticker,
                start.astimezone(UTC).strftime("%Y-%m-%dT%H:00"),
                end.astimezone(UTC).strftime("%Y-%m-%dT%H:00"),
            ),
        ).fetchone()
        return float(row["usd"])

    def lp_net_spent(self, mode: str) -> float:
        """What fills (with fees) cost minus what settlements paid back, before any reward."""
        spent = self.db.execute(
            "SELECT COALESCE(SUM(qty*price + fee),0) c FROM lp_fills WHERE mode=?", (mode,)
        ).fetchone()["c"]
        back = self.db.execute("SELECT COALESCE(SUM(payout),0) p FROM lp_settlements WHERE mode=?", (mode,)).fetchone()
        return float(spent) - float(back["p"])

    def lp_reward_for(self, mode: str, ticker: str, since: datetime) -> float:
        hour_cut = since.astimezone(UTC).strftime("%Y-%m-%dT%H:00")
        row = self.db.execute(
            "SELECT COALESCE(SUM(usd),0) usd FROM lp_rewards WHERE mode=? AND ticker=? AND hour >= ?",
            (mode, ticker, hour_cut),
        ).fetchone()
        return float(row["usd"])

    def lp_reward_by_series(self, mode: str, since: datetime) -> list[tuple[str, float]]:
        hour_cut = since.astimezone(UTC).strftime("%Y-%m-%dT%H:00")
        totals: dict[str, float] = {}
        for r in self.db.execute("SELECT ticker, usd FROM lp_rewards WHERE mode=? AND hour >= ?", (mode, hour_cut)):
            series = r["ticker"].split("-", 1)[0]
            totals[series] = totals.get(series, 0.0) + r["usd"]
        return sorted(totals.items(), key=lambda kv: -kv[1])

    def lp_hourly(self, mode: str, since: datetime) -> list[tuple[str, float]]:
        hour_cut = since.astimezone(UTC).strftime("%Y-%m-%dT%H:00")
        rows = self.db.execute(
            "SELECT hour, SUM(usd) usd FROM lp_rewards WHERE mode=? AND hour >= ? GROUP BY hour ORDER BY hour",
            (mode, hour_cut),
        )
        return [(r["hour"], float(r["usd"])) for r in rows]

    def lp_recent_fills(self, mode: str, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, ticker, side, price, qty, fee FROM lp_fills WHERE mode=? ORDER BY ts DESC LIMIT ?",
            (mode, limit),
        )
        return [dict(r) for r in rows]

    def lp_recent_settlements(self, mode: str, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT ticker, result, ts, cost, payout FROM lp_settlements WHERE mode=? ORDER BY ts DESC LIMIT ?",
            (mode, limit),
        )
        return [dict(r) for r in rows]
