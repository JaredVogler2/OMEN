import { Coins, Droplets, Layers, Wallet } from "lucide-react";
import { useMemo } from "react";
import { PageHeader } from "../App";
import type { Dashboard, Liquidity } from "../api";
import { TimeChart } from "../components/charts";
import { Card, EmptyState, Pill, Stat } from "../components/ui";
import { ago, num, signedUsd, usd } from "../lib/format";

export default function Rewards({ dash }: { dash: Dashboard | null }) {
  const lp = dash?.feed?.liquidity;
  return (
    <>
      <PageHeader
        title="Rewards"
        subtitle="Kalshi pays people who keep resting orders in its books. OMEN rests small bids on the cheap side of rewarded markets, avoids the series where resting orders get picked off, and counts every reward, fill and settlement. In paper mode the orders are virtual and fill from the public tape."
      />
      {!lp ? (
        <Card>
          <EmptyState icon={<Droplets size={20} />} title="No liquidity data yet">
            Install the quoting service on the Pi with <code className="mono">scripts/install-pi.sh --with-lp</code> (paper by default). Its rewards and fills
            appear here with the Pi's next feed.
          </EmptyState>
        </Card>
      ) : (
        <Body lp={lp} />
      )}
    </>
  );
}

function Body({ lp }: { lp: Liquidity }) {
  const net = lp.total.reward_usd + lp.total.settled_pnl; // settled P&L already includes the fees paid
  const inventoryCost = lp.inventory.reduce((a, r) => a + r.cost, 0);
  const cumulative = useMemo(() => {
    let cum = 0;
    return lp.hourly.map((h) => {
      cum += h.usd;
      return { t: new Date(`${h.hour}:00Z`).getTime(), y: Math.round(cum * 100) / 100 };
    });
  }, [lp.hourly]);
  const topSeries = Math.max(...lp.by_series.map((s) => s.usd), 0.0001);

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted">
        <Pill tone={lp.mode === "live" ? "warn" : "neutral"}>{lp.mode === "live" ? "LIVE orders" : "Paper"}</Pill>
        <span>
          {lp.settings.sides === "cheap" ? "Cheap side only" : "Both sides"} · up to {usd(lp.settings.capital_per_market_usd, false)} per market ·{" "}
          {usd(lp.settings.max_total_capital_usd, false)} total · {num(lp.settings.max_markets)} markets
        </span>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Rewards, 24 hours" icon={<Coins size={14} />} tone="lock" value={usd(lp.day.reward_usd)} sub={`${num(lp.day.reward_markets)} markets`} />
        <Stat label="Rewards, 7 days" value={usd(lp.week.reward_usd)} sub={`${num(lp.week.fills)} fills · ${num(lp.week.contracts)} contracts`} />
        <Stat label="Open inventory" icon={<Layers size={14} />} value={usd(inventoryCost)} sub={`${lp.inventory.length} positions, at cost`} />
        <Stat
          label="Net, all time"
          icon={<Wallet size={14} />}
          tone={net >= 0 ? "lock" : "crit"}
          value={signedUsd(net)}
          sub="rewards + settled P&L, after fees"
        />
      </div>

      <Card className="mt-6" title="Cumulative rewards" subtitle="Accrued each minute with Kalshi's filed scoring formula; Kalshi pays each period after it ends">
        {cumulative.length > 1 ? (
          <TimeChart label="Cumulative liquidity rewards" points={cumulative} yFormat={(v) => usd(v, v < 10)} step />
        ) : (
          <p className="text-[13px] text-muted">The chart starts after the first hours of quoting.</p>
        )}
      </Card>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card title="Rewards by series" subtitle="Last 7 days">
          {lp.by_series.length ? (
            <ul className="space-y-2">
              {lp.by_series.map((s) => (
                <li key={s.series} className="text-xs">
                  <div className="flex justify-between gap-3">
                    <span className="mono truncate text-ink">{s.series}</span>
                    <span className="num text-ink-2">{usd(s.usd)}</span>
                  </div>
                  <div className="mt-1 h-1.5 rounded-full bg-surface-3">
                    <div className="h-1.5 rounded-full bg-[var(--s1)]" style={{ width: `${Math.max(2, (s.usd / topSeries) * 100)}%` }} />
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[13px] text-muted">No rewards yet.</p>
          )}
        </Card>
        <Card title="Open inventory" subtitle="Bids that filled, held to settlement" pad={false}>
          <Table
            empty="Nothing filled yet."
            head={["Market", "Side", "Contracts", "Avg cost"]}
            rows={lp.inventory.map((r) => [r.ticker, r.side.toUpperCase(), num(r.qty), usd(r.cost / Math.max(r.qty, 1e-9))])}
          />
        </Card>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card title="Recent fills" pad={false}>
          <Table
            empty="No fills yet."
            head={["When", "Market", "Bought", "Price"]}
            rows={lp.fills.map((f) => [ago(f.ts), f.ticker, `${num(f.qty)} ${f.side.toUpperCase()}`, usd(f.price)])}
          />
        </Card>
        <Card title="Settled" pad={false}>
          <Table
            empty="No inventory has settled yet."
            head={["When", "Market", "YES paid", "P&L"]}
            rows={lp.settled.map((r) => [ago(r.ts), r.ticker, usd(Number(r.result)), signedUsd(r.payout - r.cost)])}
          />
        </Card>
      </div>

      <Card className="mt-6" title="How the reward is computed" subtitle="Kalshi's filing with the CFTC, October 2026 amendment">
        <ol className="list-decimal space-y-2 pl-5 text-[13px] leading-relaxed text-ink-2">
          <li>Every second Kalshi snapshots the book. On each side it walks down from the best bid until the resting size reaches the target (usually 1,000 contracts).</li>
          <li>Bids at or above the level holding the first fifth of the target count in full. Each tick below it halves their weight. If either side never reaches the target, nobody is paid for that second.</li>
          <li>Your share of a side is your weight over everyone's. Your reward is your average share over the period, times the period's reward.</li>
          <li>A bid's reward doesn't depend on its side, but its cost and its worst loss are its price. That's why OMEN bids the cheap side.</li>
        </ol>
      </Card>
    </>
  );
}

function Table({ head, rows, empty }: { head: string[]; rows: string[][]; empty: string }) {
  if (!rows.length) return <p className="px-5 py-4 text-[13px] text-muted">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[420px] text-left text-xs">
        <thead className="text-[11px] text-muted">
          <tr className="border-b border-line">
            {head.map((h, i) => (
              <th key={h} className={i === 0 ? "px-5 py-2 font-medium" : "px-3 py-2 text-right font-medium"}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-line last:border-0">
              {r.map((c, j) => (
                <td key={j} className={j === 0 ? "px-5 py-1.5 text-ink" : j === 1 ? "mono px-3 py-1.5 text-right text-ink-2" : "num px-3 py-1.5 text-right text-ink"}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
