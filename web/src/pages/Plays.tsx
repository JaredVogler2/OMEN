import { Bell, CircleX, Clock, Inbox, Lock, Radar, RefreshCw, ScanSearch, Search, Telescope, TriangleAlert, Wallet } from "lucide-react";
import { useMemo, useState } from "react";
import { feedStatus, PageHeader } from "../App";
import { type Activity, type Dashboard, type Play, api } from "../api";
import { TimeChart } from "../components/charts";
import { PlayCard, PlayDrawer } from "../components/plays";
import { Button, Card, EmptyState, NoticeBanner, Pill, Segmented, Skeleton, Stat, inputClass } from "../components/ui";
import { ago, clean, cx, kalshiUrl, num, signedUsd, usd } from "../lib/format";
import { navigate, usePoll, useStored } from "../lib/hooks";

type TierFilter = "all" | "LOCK" | "MODEL";
type SortKey = "profit" | "return" | "apr" | "soonest";

export default function Plays({
  dash,
  loading,
  error,
  now,
  onRefresh,
  updatedAt,
}: {
  dash: Dashboard | null;
  loading: boolean;
  error: Error | null;
  now: number;
  onRefresh: () => void;
  updatedAt: number | null;
}) {
  const [tier, setTier] = useStored<TierFilter>("omen-plays-tier", "all");
  const [sort, setSort] = useStored<SortKey>("omen-plays-sort", "profit");
  const [hideFlagged, setHideFlagged] = useStored("omen-plays-hide-flagged", false);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Play | null>(null);
  const history = usePoll((s) => api.history(s), 120000);

  const plays = dash?.plays ?? [];
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rows = plays.filter(
      (p) =>
        (tier === "all" || p.tier === tier) &&
        (!hideFlagged || !p.suspicious) &&
        (!needle || p.title.toLowerCase().includes(needle) || p.event_ticker.toLowerCase().includes(needle)),
    );
    const by: Record<SortKey, (a: Play, b: Play) => number> = {
      profit: (a, b) => b.profit - a.profit,
      return: (a, b) => b.return_pct - a.return_pct,
      apr: (a, b) => b.apr_pct - a.apr_pct,
      soonest: (a, b) => (a.settles_at ?? "9").localeCompare(b.settles_at ?? "9"),
    };
    return [...rows].sort((a, b) => Number(a.suspicious) - Number(b.suspicious) || by[sort](a, b));
  }, [plays, tier, sort, hideFlagged, q]);

  const locks = plays.filter((p) => p.tier === "LOCK" && !p.suspicious);
  const models = plays.filter((p) => p.tier === "MODEL");
  const lockedTotal = locks.reduce((s, p) => s + p.profit, 0);
  const best = locks.reduce<Play | null>((b, p) => (!b || p.profit > b.profit ? p : b), null);
  const settled = dash?.feed?.paper.totals.find((t) => t.status === "settled");
  const status = feedStatus(dash);
  const lastScan = dash?.last_scan;
  const nearMisses = lastScan?.near_misses ?? [];
  const points = (history.data?.points ?? []).map((p) => ({ t: new Date(p.t).getTime(), y: p.plays }));

  return (
    <>
      <PageHeader
        title="Plays worth playing"
        subtitle="Every play is re-priced against the full order book with Kalshi's current fee terms for its series. LOCK plays pay at least their cost after fees whatever happens; MODEL plays are statistical edges."
        actions={
          <>
            <span className="text-xs text-muted">{updatedAt ? `Updated ${ago(new Date(updatedAt).toISOString(), now)}` : ""}</span>
            <Button size="md" icon={<RefreshCw size={14} />} onClick={onRefresh} loading={loading}>
              Refresh
            </Button>
            <Button size="md" variant="primary" icon={<ScanSearch size={14} />} onClick={() => navigate("scan")}>
              Scan now
            </Button>
          </>
        }
      />

      {error && !dash && <NoticeBanner notice={{ severity: "critical", title: "Could not load the dashboard", body: error.message }} />}
      {dash && !dash.store_ok && (
        <div className="mb-4">
          <NoticeBanner notice={{ severity: "warning", title: "Storage unavailable", body: "Upstash Redis did not answer, so the Pi feed and saved scans can't be shown. See System for setup." }} />
        </div>
      )}
      {dash && status.tone !== "good" && (
        <div className="mb-5">
          <NoticeBanner
            notice={
              status.tone === "idle"
                ? {
                    severity: "info",
                    title: "Live feed not connected yet",
                    body: "Your Pi scans every market around the clock and pushes what it finds here. Until it's connected, run on-demand scans from this site. Setup steps are on the System page.",
                  }
                : { severity: status.tone === "crit" ? "critical" : "warning", title: status.label, body: `${status.detail}. Plays from the Pi may be out of date.` }
            }
          />
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {loading ? (
          Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[92px]" />)
        ) : (
          <>
            <Stat
              label="Live plays"
              icon={<Radar size={14} />}
              value={<span className="num">{plays.length}</span>}
              sub={`${locks.length} LOCK · ${models.length} MODEL`}
            />
            <Stat
              label="Locked profit available"
              icon={<Lock size={14} />}
              tone="lock"
              value={<span className="num">{usd(lockedTotal)}</span>}
              sub={best ? `best ${usd(best.profit)} · ${best.event_ticker}` : "none right now"}
            />
            <Stat
              label="Paper P&L (settled)"
              icon={<Wallet size={14} />}
              value={<span className="num">{settled ? signedUsd(settled.pnl ?? 0) : "—"}</span>}
              sub={settled ? `${num(settled.n)} settled positions` : "from the Pi's paper ledger"}
            />
            <Stat
              label="Last on-demand scan"
              icon={<Clock size={14} />}
              value={<span className="text-xl">{lastScan ? ago(lastScan.at, now) : "never"}</span>}
              sub={lastScan ? `${num(lastScan.stats.markets)} markets · ${lastScan.plays.length} plays` : "run one from Scan"}
            />
          </>
        )}
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-[1fr_340px]">
        <div className="min-w-0">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Segmented
              label="Tier"
              value={tier}
              onChange={setTier}
              options={[
                { value: "all", label: "All" },
                { value: "LOCK", label: "LOCK" },
                { value: "MODEL", label: "MODEL" },
              ]}
            />
            <Segmented
              label="Sort by"
              value={sort}
              onChange={setSort}
              options={[
                { value: "profit", label: "Profit" },
                { value: "return", label: "Return" },
                { value: "apr", label: "APR" },
                { value: "soonest", label: "Soonest" },
              ]}
            />
            <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2 select-none">
              <input type="checkbox" checked={hideFlagged} onChange={(e) => setHideFlagged(e.target.checked)} className="accent-[var(--accent)]" />
              Hide flagged
            </label>
            <div className="relative ml-auto w-full sm:w-56">
              <Search size={14} className="absolute top-1/2 left-3 -translate-y-1/2 text-muted" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by title or ticker" className={cx(inputClass, "h-8 pl-8 text-xs")} aria-label="Filter plays" />
            </div>
          </div>

          {loading ? (
            <div className="grid gap-3 md:grid-cols-2">
              {Array.from({ length: 4 }, (_, i) => (
                <Skeleton key={i} className="h-[196px]" />
              ))}
            </div>
          ) : shown.length ? (
            <div className="grid gap-3 md:grid-cols-2">
              {shown.map((p) => (
                <PlayCard key={p.key} play={p} now={now} onOpen={() => setOpen(p)} />
              ))}
            </div>
          ) : (
            <Card>
              <EmptyState
                icon={<Inbox size={20} />}
                title={plays.length ? "Nothing matches these filters" : "No plays clear the bar right now"}
                action={
                  !plays.length && (
                    <Button variant="primary" icon={<ScanSearch size={14} />} onClick={() => navigate("scan")}>
                      Run a scan
                    </Button>
                  )
                }
              >
                {plays.length
                  ? "Clear the search or switch the tier filter."
                  : "That's normal. Fee-proof arbitrage on Kalshi is rare and short-lived; OMEN only shows a play once it still pays after every fee at real order-book depth. The closest candidates are listed below."}
              </EmptyState>
            </Card>
          )}

          <Card
            className="mt-6"
            title="Closest to profitable"
            subtitle={
              lastScan
                ? `From the scan ${ago(lastScan.at, now)}. Edge is per $1 set at the best price before depth; negative means the set costs more than it pays.`
                : "Run a scan to see which structures came closest."
            }
            action={<Telescope size={16} className="text-muted" />}
            pad={false}
          >
            {nearMisses.length ? (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-[13px]">
                  <thead className="text-[11px] text-muted">
                    <tr className="border-b border-line">
                      <th className="px-5 py-2 font-medium">Event</th>
                      <th className="hidden px-2 py-2 font-medium sm:table-cell">Structure</th>
                      <th className="hidden px-2 py-2 text-right font-medium sm:table-cell">Legs</th>
                      <th className="px-5 py-2 text-right font-medium">Edge / set</th>
                    </tr>
                  </thead>
                  <tbody>
                    {nearMisses.map((n) => (
                      <tr key={n.strategy + n.event_ticker + (n.detail ?? "")} className="border-b border-line last:border-0 hover:bg-surface-2/60">
                        <td className="max-w-[180px] px-5 py-2 sm:max-w-[320px]">
                          <a href={kalshiUrl(n.event_ticker)} target="_blank" rel="noreferrer noopener" className="block truncate text-ink hover:underline" title={clean(n.title)}>
                            {clean(n.title)}
                          </a>
                          <div className="mono truncate text-[10px] text-muted">
                            <span className="font-sans sm:hidden">{STRUCT[n.strategy] ?? n.strategy} · </span>
                            {n.detail ?? n.event_ticker}
                          </div>
                        </td>
                        <td className="hidden px-2 py-2 text-ink-2 sm:table-cell">{STRUCT[n.strategy] ?? n.strategy}</td>
                        <td className="num hidden px-2 py-2 text-right text-ink-2 sm:table-cell">{n.legs}</td>
                        <td className={cx("num px-5 py-2 text-right font-medium", n.edge >= 0 ? "text-lock" : "text-ink-2")}>
                          {n.edge >= 0 ? "+" : "−"}
                          {Math.abs(n.edge * 100).toFixed(1)}¢
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="px-5 pb-5 text-[13px] text-muted">{lastScan ? "No near misses in the last scan." : "No scan yet."}</p>
            )}
          </Card>
        </div>

        <div className="space-y-6">
          <Card title="Plays found over time" subtitle="Pi pushes and on-demand scans, last 7 days">
            {points.length > 1 ? (
              <TimeChart points={points} height={150} step integer yFormat={(v) => num(v)} label="Plays found over time" tooltip={(p) => `${num(p.y)} plays`} />
            ) : (
              <p className="text-[13px] text-muted">The chart fills in as the Pi pushes updates and scans run.</p>
            )}
          </Card>
          <ActivityFeed items={dash?.feed?.activity ?? []} now={now} />
        </div>
      </div>

      <PlayDrawer play={open} onClose={() => setOpen(null)} />
    </>
  );
}

const STRUCT: Record<string, string> = { no_basket: "NO basket", yes_partition: "YES basket", ladder: "Ladder" };

function ActivityFeed({ items, now }: { items: Activity[]; now: number }) {
  return (
    <Card title="Activity" subtitle="Alerts, expiries and fee changes seen by the Pi" pad={false}>
      {items.length ? (
        <ol className="max-h-[520px] overflow-y-auto px-5 pb-4">
          {items.slice(0, 40).map((a, i) => (
            <li key={i} className="flex gap-3 border-b border-line py-2.5 last:border-0">
              <span
                className={cx(
                  "mt-0.5 grid size-6 shrink-0 place-items-center rounded-md",
                  a.kind === "alert" ? (a.tier === "MODEL" ? "bg-model-soft text-model" : "bg-lock-soft text-lock") : a.kind === "error" ? "bg-crit-soft text-crit" : a.kind === "fee" ? "bg-warn-soft text-warn" : "bg-surface-2 text-muted",
                )}
              >
                {a.kind === "alert" ? <Bell size={12} /> : a.kind === "error" ? <TriangleAlert size={12} /> : a.kind === "fee" ? <Wallet size={12} /> : <CircleX size={12} />}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span className="text-[11px] font-medium text-ink-2">
                    {a.kind === "alert" ? "Alerted" : a.kind === "gone" ? "Gone" : a.kind === "fee" ? "Fee change" : "Error"}
                  </span>
                  {a.tier && <Pill tone={a.tier === "LOCK" ? "lock" : "model"}>{a.tier}</Pill>}
                  <span className="ml-auto shrink-0 text-[11px] text-muted">{ago(a.ts, now)}</span>
                </div>
                <div className="mt-0.5 line-clamp-2 text-[12px] leading-snug text-ink">{clean(a.title)}</div>
                {a.profit != null && a.kind !== "fee" && <div className="num text-[11px] text-muted">{usd(a.profit)} profit</div>}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="px-5 pb-5 text-[13px] text-muted">Nothing yet. Activity appears once the Pi is connected.</p>
      )}
    </Card>
  );
}
