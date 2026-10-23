import { CircleCheck, CircleDashed, Database, Server, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { PageHeader, feedStatus } from "../App";
import { type Dashboard, api } from "../api";
import { Card, Kv, Meter, Pill, StatusDot } from "../components/ui";
import { ago, cx, duration, num, short, usd, when } from "../lib/format";
import { usePoll } from "../lib/hooks";

export default function System({ dash, error }: { dash: Dashboard | null; error: Error | null }) {
  const health = usePoll(() => api.health(), 0);
  const feed = dash?.feed;
  const status = feedStatus(dash);
  const h = health.data;
  const b = dash?.budget;

  const steps: { done: boolean | null; title: string; body: ReactNode }[] = [
    { done: true, title: "Site password set", body: "You're signed in, so OMEN_WEB_PASSWORD is configured." },
    {
      done: h ? h.store === "upstash" : null,
      title: "Upstash Redis connected",
      body: (
        <>
          Stores the Pi feed, scan results and budgets. Add it free from Vercel → Storage → Marketplace → <b>Upstash for Redis</b>; it
          injects <code className="mono">KV_REST_API_URL</code> and <code className="mono">KV_REST_API_TOKEN</code>.
          {h?.store === "memory" && " Right now data lives in memory and is lost when the function sleeps."}
        </>
      ),
    },
    {
      done: h ? h.ingest_enabled : null,
      title: "Ingest secret set",
      body: (
        <>
          Set the same random <code className="mono">OMEN_INGEST_SECRET</code> (16+ characters) in Vercel and in the Pi's <code className="mono">.env</code>, so only your Pi
          can publish here.
        </>
      ),
    },
    {
      done: dash ? !!dash.feed_meta : null,
      title: "Pi publishing",
      body: (
        <>
          On the Pi, set <code className="mono">OMEN_WEB_URL</code> to this site's address and restart OMEN (<code className="mono">sudo systemctl restart omen</code>). It pushes a
          signed snapshot every couple of minutes.
        </>
      ),
    },
  ];

  return (
    <>
      <PageHeader title="System" subtitle="Health of the Pi scanner, this site and the connection between them." />
      {error && (
        <div className="mb-4 rounded-xl border border-crit/30 bg-crit-soft px-4 py-3 text-[13px] text-ink">
          <TriangleAlert size={14} className="mr-1.5 inline text-crit" /> {error.message}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Setup checklist">
          <ol className="space-y-4">
            {steps.map((s) => (
              <li key={s.title} className="flex gap-3">
                <span className="mt-0.5 shrink-0">
                  {s.done ? <CircleCheck size={17} className="text-lock" /> : s.done === false ? <CircleDashed size={17} className="text-warn" /> : <CircleDashed size={17} className="text-muted" />}
                </span>
                <div>
                  <div className="flex items-center gap-2 text-[13px] font-semibold text-ink">
                    {s.title}
                    {s.done === false && <Pill tone="warn">to do</Pill>}
                  </div>
                  <p className="mt-0.5 text-xs leading-relaxed text-muted">{s.body}</p>
                </div>
              </li>
            ))}
          </ol>
        </Card>

        <Card title="Live feed" action={<Server size={15} className="text-muted" />}>
          <div className="mb-3 flex items-center gap-2 text-[13px] font-semibold text-ink">
            <StatusDot tone={status.tone} pulse={status.tone === "good"} />
            {status.label}
          </div>
          <dl className="divide-y divide-[var(--border)]">
            <Kv k="Host" v={dash?.feed_meta?.host ?? "—"} mono />
            <Kv k="Last push" v={dash?.feed_meta ? `${ago(dash.feed_meta.received_at)} (${when(dash.feed_meta.received_at)})` : "—"} />
            <Kv k="Snapshot size" v={dash?.feed_meta ? `${short(dash.feed_meta.bytes)}B` : "—"} />
            <Kv k="OMEN on the Pi" v={dash?.feed_meta?.omen_version ?? "—"} />
            <Kv k="OMEN on this site" v={h?.version ?? dash?.version ?? "—"} />
            <Kv k="Execution mode" v={feed ? <Pill tone={feed.health.execution_mode === "live" ? "crit" : "neutral"}>{feed.health.execution_mode}</Pill> : "—"} />
            <Kv k="Markets tracked" v={feed ? num(feed.health.markets_in_store) : "—"} />
            <Kv
              k="Cadence"
              v={
                feed
                  ? `hot ${duration(feed.health.cadence.hot_seconds)} (≤${feed.health.cadence.hot_horizon_hours} h) · full ${duration(feed.health.cadence.full_seconds)} · model ${duration(feed.health.cadence.model_seconds)}`
                  : "—"
              }
            />
            <Kv
              k="Discord outbox"
              v={feed ? Object.entries(feed.health.outbox).map(([k, v]) => `${k} ${v}`).join(" · ") || "empty" : "—"}
            />
          </dl>
        </Card>

        {feed && (
          <Card title="Thresholds on the Pi" subtitle="A LOCK play is alerted only if it clears all of these after fees">
            <dl className="divide-y divide-[var(--border)]">
              <Kv k="Min profit" v={usd(feed.health.thresholds.min_profit_usd)} />
              <Kv k="Min return" v={`${feed.health.thresholds.min_return_pct}%`} />
              <Kv k="Min APR" v={`${feed.health.thresholds.min_apr_pct}%`} />
              <Kv k="Max capital per play" v={usd(feed.health.thresholds.max_capital_usd, false)} />
              <Kv k="Plausibility limit" v={`${feed.health.thresholds.max_plausible_return_pct}% return (above = flagged)`} />
              <Kv k="Fee schedule" v={`${feed.fees.schedule_version} · verified ${feed.fees.verified_on}`} />
            </dl>
          </Card>
        )}

        {b && (
          <Card title="This site's budget" action={<Database size={15} className="text-muted" />} subtitle={`Store: ${b.store}. Limits are env vars (OMEN_WEB_DAILY_SCANS, OMEN_WEB_MONTHLY_CPU_SECONDS).`}>
            <div className="space-y-4">
              <Meter label="On-demand scans today" value={b.scans_today} max={b.daily_scan_cap} format={(v) => num(v)} />
              <Meter label="Scan compute this month" value={b.cpu_seconds_month / 60} max={b.monthly_cpu_budget_seconds / 60} format={(v) => `${num(v)} min`} />
              <p className="text-[11px] leading-relaxed text-muted">
                Vercel Hobby includes 4 hours of active CPU a month. Scans are capped at {duration(b.monthly_cpu_budget_seconds)} so page views and the API always have
                headroom. At least {b.min_scan_gap_seconds}s between scans.
              </p>
            </div>
          </Card>
        )}
      </div>

      {feed && feed.runs.length > 0 && (
        <Card className="mt-6" title="Recent scanner runs" subtitle="From the Pi" pad={false}>
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full min-w-[640px] text-left text-xs">
              <thead className="sticky top-0 bg-surface text-[11px] text-muted">
                <tr className="border-b border-line">
                  <th className="px-5 py-2 font-medium">Run</th>
                  <th className="px-2 py-2 font-medium">Started</th>
                  <th className="px-2 py-2 text-right font-medium">Took</th>
                  <th className="px-2 py-2 text-right font-medium">Markets</th>
                  <th className="px-2 py-2 text-right font-medium">Candidates</th>
                  <th className="px-2 py-2 text-right font-medium">Plays</th>
                  <th className="px-2 py-2 text-right font-medium">Alerts</th>
                  <th className="px-5 py-2 text-right font-medium">API calls</th>
                </tr>
              </thead>
              <tbody>
                {feed.runs.map((r, i) => {
                  const took = r.finished_at ? (new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000 : null;
                  return (
                    <tr key={i} className={cx("border-b border-line last:border-0", r.error && "bg-crit-soft")} title={r.error ?? undefined}>
                      <td className="px-5 py-1.5 text-ink">
                        {r.kind}
                        {r.error && <Pill tone="crit" className="ml-2">error</Pill>}
                      </td>
                      <td className="px-2 py-1.5 text-ink-2">{ago(r.started_at)}</td>
                      <td className="num px-2 py-1.5 text-right text-ink-2">{duration(took)}</td>
                      <td className="num px-2 py-1.5 text-right text-ink-2">{num(r.markets)}</td>
                      <td className="num px-2 py-1.5 text-right text-ink-2">{num(r.candidates)}</td>
                      <td className="num px-2 py-1.5 text-right text-ink">{num(r.opportunities)}</td>
                      <td className="num px-2 py-1.5 text-right text-ink-2">{num(r.alerts)}</td>
                      <td className="num px-5 py-1.5 text-right text-ink-2">{num(r.requests)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
