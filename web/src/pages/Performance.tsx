import { FlaskConical, Wallet } from "lucide-react";
import { useMemo } from "react";
import { PageHeader } from "../App";
import { type Dashboard, type PaperRow, api } from "../api";
import { TimeChart } from "../components/charts";
import { Card, EmptyState, Pill, Stat, TierBadge } from "../components/ui";
import { ago, cx, num, signedUsd, until, usd, when } from "../lib/format";
import { usePoll } from "../lib/hooks";

export default function Performance({ dash }: { dash: Dashboard | null }) {
  const feed = dash?.feed;
  const history = usePoll((s) => api.history(s), 0);
  const totals = feed?.paper.totals ?? [];
  const open = totals.find((t) => t.status === "open");
  const settled = totals.find((t) => t.status === "settled");
  const settledRows = feed?.paper.settled ?? [];
  const wins = settledRows.filter((r) => (r.pnl ?? 0) > 0).length;

  const pnlPoints = useMemo(() => {
    const rows = [...settledRows].filter((r) => r.settled_at).sort((a, b) => a.settled_at!.localeCompare(b.settled_at!));
    let cum = 0;
    return rows.map((r) => {
      cum += r.pnl ?? 0;
      return { t: new Date(r.settled_at!).getTime(), y: Math.round(cum * 100) / 100 };
    });
  }, [settledRows]);

  const bestPoints = (history.data?.points ?? []).map((p) => ({ t: new Date(p.t).getTime(), y: p.best_profit }));

  return (
    <>
      <PageHeader
        title="Performance"
        subtitle="Every alerted play is paper-traded on the Pi at the prices it was found at, then settled with Kalshi's actual results, so you can see what following OMEN would have earned before risking real money."
      />
      {!feed && (
        <Card>
          <EmptyState icon={<Wallet size={20} />} title="No paper ledger yet">
            Performance comes from the Pi. Once it pushes its live feed here (see System), paper positions and settled P&L appear on this page.
          </EmptyState>
        </Card>
      )}
      {feed && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Settled P&L" value={<span className="num">{signedUsd(settled?.pnl ?? 0)}</span>} sub={`${num(settled?.n ?? 0)} positions`} tone={(settled?.pnl ?? 0) >= 0 ? "lock" : "crit"} />
            <Stat label="Win rate" value={<span className="num">{settledRows.length ? `${Math.round((wins / settledRows.length) * 100)}%` : "—"}</span>} sub={`${wins} of ${settledRows.length} recent settled`} />
            <Stat label="Open positions" value={<span className="num">{num(open?.n ?? 0)}</span>} sub={`${usd(open?.cap ?? 0, false)} capital at work`} />
            <Stat label="Expected on open" value={<span className="num">{signedUsd(open?.exp ?? 0)}</span>} sub="profit if they settle as priced" />
          </div>

          <div className="mt-6 grid gap-6 lg:grid-cols-2">
            <Card title="Cumulative settled P&L" subtitle="Paper trading, after all fees">
              {pnlPoints.length > 1 ? (
                <TimeChart points={pnlPoints} step zeroLine height={200} yFormat={(v) => usd(v, false)} tooltip={(p) => signedUsd(p.y)} label="Cumulative settled P&L" />
              ) : (
                <p className="text-[13px] text-muted">Appears once at least two paper positions have settled.</p>
              )}
            </Card>
            <Card title="Best play available" subtitle="Largest profit on offer at each Pi push or scan">
              {bestPoints.length > 1 ? (
                <TimeChart points={bestPoints} height={200} yFormat={(v) => usd(v, false)} tooltip={(p) => usd(p.y)} label="Best play available over time" />
              ) : (
                <p className="text-[13px] text-muted">Builds up as the Pi pushes updates.</p>
              )}
            </Card>
          </div>

          <Card className="mt-6" title="Models" subtitle="MODEL plays stay in shadow until every calibration gate passes" action={<FlaskConical size={15} className="text-muted" />} pad={false}>
            {feed.models.length ? (
              <ul className="divide-y divide-[var(--border)]">
                {feed.models.map((m) => (
                  <li key={m.name} className="flex items-start gap-3 px-5 py-3">
                    <div className="min-w-0 flex-1">
                      <div className="mono text-[13px] text-ink">{m.name}</div>
                      <div className="mt-0.5 text-xs leading-relaxed whitespace-pre-line text-muted">{m.summary}</div>
                    </div>
                    <Pill tone={m.state === "live" ? "model" : m.passed ? "lock" : "neutral"}>{m.state === "live" ? "live" : m.passed ? "gates passed" : m.state}</Pill>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-5 pb-5 text-[13px] text-muted">No models reported.</p>
            )}
          </Card>

          <PaperTable title="Open paper positions" rows={feed.paper.open} open />
          <PaperTable title="Settled paper positions" rows={feed.paper.settled} />
        </>
      )}
    </>
  );
}

function PaperTable({ title, rows, open }: { title: string; rows: PaperRow[]; open?: boolean }) {
  return (
    <Card className="mt-6" title={title} subtitle={`${rows.length} shown`} pad={false}>
      {rows.length ? (
        <div className="max-h-[440px] overflow-auto">
          <table className="w-full min-w-[640px] text-left text-xs">
            <thead className="sticky top-0 bg-surface text-[11px] text-muted">
              <tr className="border-b border-line">
                <th className="px-5 py-2 font-medium">Play</th>
                <th className="px-2 py-2 font-medium">Opened</th>
                <th className="px-2 py-2 font-medium">{open ? "Settles" : "Settled"}</th>
                <th className="px-2 py-2 text-right font-medium">Capital</th>
                <th className="px-2 py-2 text-right font-medium">Fees</th>
                <th className="px-5 py-2 text-right font-medium">{open ? "Expected" : "P&L"}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const pnl = open ? r.expected_profit : r.pnl ?? 0;
                return (
                  <tr key={r.id + r.opened_at} className="border-b border-line last:border-0">
                    <td className="px-5 py-1.5">
                      <span className="flex items-center gap-2">
                        <TierBadge tier={r.tier} />
                        <span className="mono text-ink">#{r.id}</span>
                        <span className="text-muted">{r.strategy}</span>
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-ink-2">{ago(r.opened_at)}</td>
                    <td className="px-2 py-1.5 text-ink-2">{open ? until(r.settles_at) : when(r.settled_at)}</td>
                    <td className="num px-2 py-1.5 text-right text-ink-2">{usd(r.capital)}</td>
                    <td className="num px-2 py-1.5 text-right text-ink-2">{usd(r.fees)}</td>
                    <td className={cx("num px-5 py-1.5 text-right font-medium", pnl >= 0 ? "text-ink" : "text-crit")}>{signedUsd(pnl)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="px-5 pb-5 text-[13px] text-muted">None.</p>
      )}
    </Card>
  );
}
