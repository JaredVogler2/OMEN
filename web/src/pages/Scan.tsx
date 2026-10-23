import { Check, CircleX, Cpu, Inbox, LoaderCircle, Play as PlayIcon, ScanSearch, ShieldAlert, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { PageHeader } from "../App";
import { type Dashboard, type Play, type ScanEvent, type ScanResult, ApiError, api } from "../api";
import { PlayCard, PlayDrawer } from "../components/plays";
import { Button, Card, EmptyState, Field, Meter, NoticeBanner, Pill, Segmented, inputClass } from "../components/ui";
import { ago, clean, cx, kalshiUrl, num, short } from "../lib/format";
import { useNow, useStored } from "../lib/hooks";

const HORIZONS = [
  { value: "6", label: "6 h" },
  { value: "24", label: "24 h" },
  { value: "72", label: "3 d" },
  { value: "168", label: "7 d" },
  { value: "720", label: "30 d" },
  { value: "all", label: "All" },
] as const;
type Horizon = (typeof HORIZONS)[number]["value"];

const STRATEGIES = [
  { id: "no_basket", name: "NO basket", hint: "Buy NO on every outcome of a mutually exclusive event" },
  { id: "yes_partition", name: "YES basket", hint: "Buy YES on buckets that provably cover every outcome" },
  { id: "ladder", name: "Ladder", hint: "Sell the narrower strike, buy the wider one that must also pay" },
];

const STAGES = [
  { id: "fees", label: "Fee terms" },
  { id: "markets", label: "Markets" },
  { id: "screen", label: "Screen" },
  { id: "verify", label: "Order books" },
  { id: "done", label: "Done" },
];

interface Form {
  horizon: Horizon;
  strategies: string[];
  min_profit_usd: string;
  min_return_pct: string;
  min_apr_pct: string;
  max_capital_usd: string;
  near_misses: boolean;
}

const DEFAULTS: Form = {
  horizon: "168",
  strategies: STRATEGIES.map((s) => s.id),
  min_profit_usd: "1",
  min_return_pct: "0.75",
  min_apr_pct: "15",
  max_capital_usd: "1000",
  near_misses: true,
};

type Run =
  | { state: "idle" }
  | { state: "running"; stage: string; label: string; markets: number; candidates?: number; started: number }
  | { state: "done"; result: ScanResult }
  | { state: "error"; message: string };

export default function Scan({ dash, onResult }: { dash: Dashboard | null; onResult: () => void }) {
  const [form, setForm] = useStored<Form>("omen-scan-form", DEFAULTS);
  const [run, setRun] = useState<Run>({ state: "idle" });
  const [open, setOpen] = useState<Play | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  const now = useNow(1000);
  useEffect(() => () => ctrl.current?.abort(), []);

  const budget = dash?.budget;
  const result = run.state === "done" ? run.result : run.state === "idle" ? dash?.last_scan ?? null : null;
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm({ ...form, [k]: v });

  const start = async () => {
    const c = new AbortController();
    ctrl.current = c;
    const started = Date.now();
    setRun({ state: "running", stage: "fees", label: "Starting", markets: 0, started });
    const got: { result: ScanResult | null } = { result: null };
    try {
      await api.scan(
        {
          horizon_hours: form.horizon === "all" ? "all" : Number(form.horizon),
          strategies: form.strategies,
          min_profit_usd: Number(form.min_profit_usd),
          min_return_pct: Number(form.min_return_pct),
          min_apr_pct: Number(form.min_apr_pct),
          max_capital_usd: Number(form.max_capital_usd),
          near_misses: form.near_misses,
        },
        (e: ScanEvent) => {
          if (e.type === "stage")
            setRun((r) =>
              r.state === "running"
                ? {
                    ...r,
                    stage: e.stage.replace(/_done$/, ""),
                    label: e.label ?? r.label,
                    markets: e.markets ?? r.markets,
                    candidates: e.candidates ?? r.candidates,
                  }
                : r,
            );
          else if (e.type === "progress") setRun((r) => (r.state === "running" ? { ...r, markets: e.count } : r));
          else if (e.type === "result") got.result = e.result;
          else if (e.type === "error") throw new ApiError(500, e.message);
        },
        c.signal,
      );
      if (got.result) {
        setRun({ state: "done", result: got.result });
        onResult();
      } else setRun({ state: "error", message: "The scan ended without a result (the function may have hit its time limit). Try a shorter horizon." });
    } catch (e) {
      if (c.signal.aborted) setRun({ state: "idle" });
      else setRun({ state: "error", message: (e as Error).message });
    }
  };

  const running = run.state === "running";
  const invalid = !form.strategies.length;

  return (
    <>
      <PageHeader
        title="Scan on demand"
        subtitle="Pulls every matching market live from Kalshi, screens all three LOCK structures, then re-prices survivors against full order books with each series' current fee terms. The Pi does this continuously; use this for a fresh look or a custom bar."
      />

      <div className="grid gap-6 xl:grid-cols-[360px_1fr]">
        <div className="space-y-6">
          <Card title="Scope">
            <div className="space-y-5">
              <Field label="Markets closing within" hint={form.horizon === "all" ? "Every open market (~170k). Takes about 2 minutes." : "Shorter horizons finish faster and lock up capital for less time."}>
                <Segmented label="Horizon" value={form.horizon} onChange={(v) => set("horizon", v)} options={HORIZONS.map((h) => ({ value: h.value, label: h.label }))} />
              </Field>
              <fieldset>
                <legend className="text-xs font-medium text-ink-2">Structures</legend>
                <div className="mt-2 space-y-2">
                  {STRATEGIES.map((s) => (
                    <label key={s.id} className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-line px-3 py-2 hover:bg-surface-2/60">
                      <input
                        type="checkbox"
                        className="mt-0.5 accent-[var(--accent)]"
                        checked={form.strategies.includes(s.id)}
                        onChange={(e) => set("strategies", e.target.checked ? [...form.strategies, s.id] : form.strategies.filter((x) => x !== s.id))}
                      />
                      <span>
                        <span className="block text-[13px] font-medium text-ink">{s.name}</span>
                        <span className="block text-[11px] leading-snug text-muted">{s.hint}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
            </div>
          </Card>

          <Card title="Bar to clear" subtitle="A play must pass every threshold after all fees.">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Min profit ($)">
                <input className={inputClass} inputMode="decimal" value={form.min_profit_usd} onChange={(e) => set("min_profit_usd", e.target.value)} />
              </Field>
              <Field label="Min return (%)">
                <input className={inputClass} inputMode="decimal" value={form.min_return_pct} onChange={(e) => set("min_return_pct", e.target.value)} />
              </Field>
              <Field label="Min APR (%)">
                <input className={inputClass} inputMode="decimal" value={form.min_apr_pct} onChange={(e) => set("min_apr_pct", e.target.value)} />
              </Field>
              <Field label="Max capital ($)">
                <input className={inputClass} inputMode="decimal" value={form.max_capital_usd} onChange={(e) => set("max_capital_usd", e.target.value)} />
              </Field>
            </div>
            <label className="mt-4 flex cursor-pointer items-center gap-2 text-xs text-ink-2 select-none">
              <input type="checkbox" className="accent-[var(--accent)]" checked={form.near_misses} onChange={(e) => set("near_misses", e.target.checked)} />
              Also list the closest near misses
            </label>
            <div className="mt-5 flex gap-2">
              {running ? (
                <Button variant="danger" icon={<Square size={13} />} onClick={() => ctrl.current?.abort()} className="flex-1">
                  Stop watching
                </Button>
              ) : (
                <Button variant="primary" icon={<PlayIcon size={14} />} onClick={start} disabled={invalid} className="flex-1">
                  Run scan
                </Button>
              )}
              <Button variant="ghost" onClick={() => setForm(DEFAULTS)} disabled={running}>
                Reset
              </Button>
            </div>
          </Card>

          {budget && (
            <Card title="Free-plan budget" action={<Cpu size={15} className="text-muted" />} subtitle="Keeps on-demand scans inside Vercel Hobby's included compute.">
              <div className="space-y-4">
                <Meter label="Scans today" value={budget.scans_today} max={budget.daily_scan_cap} format={(v) => num(v)} />
                <Meter label="Compute this month" value={budget.cpu_seconds_month / 60} max={budget.monthly_cpu_budget_seconds / 60} format={(v) => `${num(v)} min`} />
              </div>
            </Card>
          )}
        </div>

        <div className="min-w-0 space-y-6">
          {run.state === "running" && <Progress run={run} now={now} />}
          {run.state === "error" && (
            <NoticeBanner notice={{ severity: "critical", title: "Scan failed", body: run.message }} onDismiss={() => setRun({ state: "idle" })} />
          )}
          {result ? (
            <Results result={result} onOpen={setOpen} fresh={run.state === "done"} />
          ) : (
            run.state !== "running" && (
              <Card>
                <EmptyState icon={<ScanSearch size={20} />} title="No scan yet">
                  Pick a horizon and run a scan. Results stream in as each stage finishes.
                </EmptyState>
              </Card>
            )
          )}
        </div>
      </div>
      <PlayDrawer play={open} onClose={() => setOpen(null)} />
    </>
  );
}

function Progress({ run, now }: { run: Extract<Run, { state: "running" }>; now: number }) {
  const idx = Math.max(0, STAGES.findIndex((s) => s.id === run.stage));
  const elapsed = Math.max(0, Math.round((now - run.started) / 1000));
  return (
    <Card>
      <div className="flex items-center gap-3">
        <LoaderCircle size={18} className="animate-spin text-accent" />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-ink">{run.label}…</div>
          <div className="num text-xs text-muted">
            {num(run.markets)} markets{run.candidates != null && ` · ${run.candidates} candidates`} · {elapsed}s
          </div>
        </div>
      </div>
      <ol className="mt-5 grid grid-cols-5 gap-2" aria-label="Scan progress">
        {STAGES.map((s, i) => (
          <li key={s.id} className="min-w-0">
            <div className={cx("h-1 rounded-full", i < idx ? "bg-accent" : i === idx ? "sheen bg-accent-soft" : "bg-surface-3")} />
            <div className={cx("mt-1.5 flex items-center gap-1 truncate text-[11px]", i <= idx ? "text-ink-2" : "text-muted")}>
              {i < idx && <Check size={11} className="shrink-0 text-accent" />}
              {s.label}
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function Results({ result, onOpen, fresh }: { result: ScanResult; onOpen: (p: Play) => void; fresh: boolean }) {
  const now = Date.now();
  const s = result.stats;
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px]">
        <span className="font-semibold text-ink">{fresh ? "Scan complete" : `Last scan · ${ago(result.at, now)}`}</span>
        <Metric k="markets" v={num(s.markets)} />
        <Metric k="candidates" v={num(s.candidates)} />
        <Metric k="verified plays" v={num(s.verified)} />
        <Metric k="API calls" v={short(s.requests)} />
        {s.seconds != null && <Metric k="time" v={`${Math.round(s.seconds)}s`} />}
        <span className="text-xs text-muted">
          {result.params.horizon_hours == null ? "all open markets" : `closing within ${result.params.horizon_hours} h`}
        </span>
      </div>
      {!result.trading_active && <NoticeBanner notice={{ severity: "warning", title: "Exchange not trading", body: "Kalshi reports trading is paused, so nothing was screened." }} />}
      {result.notices.map((n, i) => (
        <NoticeBanner key={i} notice={n} />
      ))}

      {result.plays.length ? (
        <div className="grid gap-3 md:grid-cols-2">
          {result.plays.map((p) => (
            <PlayCard key={p.key} play={p} now={now} onOpen={() => onOpen(p)} />
          ))}
        </div>
      ) : (
        <Card>
          <EmptyState icon={<Inbox size={20} />} title="No play cleared the bar">
            {s.candidates
              ? `${s.candidates} structure${s.candidates === 1 ? "" : "s"} looked profitable at the top of the book but failed on full depth, fees or your thresholds. Details below.`
              : "Nothing priced inside a fee-proof structure at the top of the book. That's the common case: real arbitrage gets taken quickly."}
          </EmptyState>
        </Card>
      )}

      {result.failed.length > 0 && (
        <Card title="Failed verification" subtitle="Top-of-book edge that disappeared on real depth, fees or your thresholds" pad={false}>
          <ul className="divide-y divide-[var(--border)]">
            {result.failed.map((f, i) => (
              <li key={i} className="flex items-start gap-3 px-5 py-3">
                <CircleX size={15} className="mt-0.5 shrink-0 text-muted" />
                <div className="min-w-0 flex-1">
                  <a href={kalshiUrl(f.event_ticker)} target="_blank" rel="noreferrer noopener" className="block truncate text-[13px] text-ink hover:underline">
                    {clean(f.title)}
                  </a>
                  <div className="text-xs text-muted">{f.reason}</div>
                </div>
                <Pill title="Edge per $1 set at the best price, before depth">+{(f.top_edge * 100).toFixed(1)}¢ top</Pill>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {result.near_misses.length > 0 && (
        <Card title="Near misses" subtitle="Closest structures by edge per $1 set at the best price (negative = cost above payout)" pad={false}>
          <ul className="divide-y divide-[var(--border)]">
            {result.near_misses.map((n, i) => (
              <li key={i} className="flex items-center gap-3 px-5 py-2.5">
                <div className="min-w-0 flex-1">
                  <a href={kalshiUrl(n.event_ticker)} target="_blank" rel="noreferrer noopener" className="block truncate text-[13px] text-ink hover:underline">
                    {clean(n.title)}
                  </a>
                  <div className="mono truncate text-[10px] text-muted">{n.detail ?? n.event_ticker}</div>
                </div>
                <span className="shrink-0 text-xs text-ink-2">{{ no_basket: "NO basket", yes_partition: "YES basket", ladder: "Ladder" }[n.strategy] ?? n.strategy}</span>
                <span className={cx("num w-16 shrink-0 text-right text-[13px] font-medium", n.edge >= 0 ? "text-lock" : "text-ink-2")}>
                  {n.edge >= 0 ? "+" : "−"}
                  {Math.abs(n.edge * 100).toFixed(1)}¢
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {result.rejected.length > 0 && (
        <details className="card group">
          <summary className="flex cursor-pointer items-center gap-2 px-5 py-3 text-[13px] font-medium text-ink-2 select-none">
            <ShieldAlert size={15} className="text-muted" />
            {result.rejected.length} structure{result.rejected.length === 1 ? "" : "s"} rejected by the safety checks
          </summary>
          <ul className="divide-y divide-[var(--border)] border-t border-line">
            {result.rejected.map((r, i) => (
              <li key={i} className="px-5 py-2 text-xs">
                <span className="mono text-ink">{r.event_ticker}</span> <span className="text-muted">· {r.reason}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

function Metric({ k, v }: { k: string; v: string }) {
  return (
    <span className="text-muted">
      <span className="num font-semibold text-ink">{v}</span> {k}
    </span>
  );
}
