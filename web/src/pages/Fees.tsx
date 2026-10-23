import { ArrowUpRight, CalendarClock, Calculator, Search, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "../App";
import { type FeeQuote, type SeriesHit, api } from "../api";
import { type Curve, FeeCurves, LegendItem } from "../components/charts";
import { Card, EmptyState, Field, Kv, NoticeBanner, Pill, Skeleton, inputClass } from "../components/ui";
import { cents, cx, day, num, pct, until, usd, when } from "../lib/format";
import { usePoll, useStored } from "../lib/hooks";

export default function Fees() {
  const info = usePoll((s) => api.fees(s), 0);
  const [series, setSeries] = useStored("omen-fee-series", "KXBTCD");
  const [query, setQuery] = useState(series);
  const [hits, setHits] = useState<SeriesHit[]>([]);
  const [showHits, setShowHits] = useState(false);
  const [priceC, setPriceC] = useStored("omen-fee-price", 50);
  const [count, setCount] = useStored("omen-fee-count", 100);
  const [quote, setQuote] = useState<FeeQuote | null>(null);
  const [quoteErr, setQuoteErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2 || q.toUpperCase() === series) {
      setHits([]);
      return;
    }
    const c = new AbortController();
    const t = window.setTimeout(() => {
      api
        .series(q, c.signal)
        .then((r) => setHits(r.series))
        .catch(() => undefined);
    }, 200);
    return () => {
      window.clearTimeout(t);
      c.abort();
    };
  }, [query, series]);

  useEffect(() => {
    if (!series || !(priceC > 0 && priceC < 100) || !(count > 0)) return;
    const t = window.setTimeout(() => {
      api
        .quote({ series, price: priceC / 100, count })
        .then((q) => {
          setQuote(q);
          setQuoteErr(null);
        })
        .catch((e) => {
          setQuote(null);
          setQuoteErr((e as Error).message);
        });
    }, 250);
    return () => window.clearTimeout(t);
  }, [series, priceC, count]);

  const d = info.data;
  const quad = d?.schedule.formulas["quadratic_with_maker_fees"] ?? d?.schedule.formulas["quadratic"];
  const curves: Curve[] = useMemo(() => {
    if (!quad) return [];
    const out: Curve[] = [
      { name: "Taker", color: "var(--s1)", f: (p) => quad.taker_rate * p * (1 - p) },
      { name: "Maker", color: "var(--s2)", f: (p) => quad.maker_rate * p * (1 - p) },
    ];
    const m = quote && quote.multiplier !== 1 ? quote.multiplier : d?.terms.find((t) => t.multiplier !== 1 && t.fee_type !== "flat")?.multiplier;
    if (m != null) {
      const rate = quote && quote.multiplier === m ? quote.taker_rate : quad.taker_rate;
      out.push({ name: `Taker ×${m}`, color: "var(--s3)", f: (p) => m * rate * p * (1 - p) });
    }
    return out;
  }, [quad, quote, d]);

  const nonstandard = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (d?.nonstandard ?? []).filter((s) => !f || s.ticker.toLowerCase().includes(f) || s.title.toLowerCase().includes(f) || s.category.toLowerCase().includes(f));
  }, [d, filter]);

  const pick = (h: SeriesHit) => {
    setSeries(h.ticker);
    setQuery(h.ticker);
    setShowHits(false);
  };

  return (
    <>
      <PageHeader
        title="Fees"
        subtitle="OMEN reads each series' fee type and multiplier, scheduled changes and per-event overrides live from Kalshi on every scan, and prices every fill with them. Nothing here is hard-coded except the formula constants from Kalshi's fee schedule."
      />
      {d?.notices.map((n, i) => (
        <div key={i} className="mb-4">
          <NoticeBanner notice={n} />
        </div>
      ))}
      {info.error && <NoticeBanner notice={{ severity: "critical", title: "Could not load fees", body: info.error.message }} />}

      <div className="grid gap-6 xl:grid-cols-[1fr_380px] xl:items-start">
        <Card
          title="Fee per contract by price"
          subtitle="fee = round up( multiplier × rate × contracts × price × (1 − price) ). Largest at 50¢, near zero at the extremes."
          action={
            <div className="hidden flex-wrap items-center gap-3 sm:flex">
              {curves.map((c) => (
                <LegendItem key={c.name} color={c.color} label={c.name} />
              ))}
            </div>
          }
        >
          {curves.length ? <FeeCurves curves={curves} marker={priceC / 100} height={320} /> : <Skeleton className="h-[320px]" />}
          <div className="mt-2 flex flex-wrap items-center gap-3 sm:hidden">
            {curves.map((c) => (
              <LegendItem key={c.name} color={c.color} label={c.name} />
            ))}
          </div>
        </Card>

        <Card title="Fee calculator" action={<Calculator size={15} className="text-muted" />} subtitle="Uses the live terms for the series you pick">
          <div className="space-y-4">
            <div className="relative">
              <Field label="Series">
                <div className="relative">
                  <Search size={14} className="absolute top-1/2 left-3 -translate-y-1/2 text-muted" />
                  <input
                    className={cx(inputClass, "mono pl-8 uppercase")}
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value);
                      setShowHits(true);
                    }}
                    onFocus={() => setShowHits(true)}
                    onBlur={() => window.setTimeout(() => setShowHits(false), 150)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        if (hits[0]) pick(hits[0]);
                        else setSeries(query.trim().toUpperCase());
                      }
                    }}
                    placeholder="e.g. KXBTCD or 'bitcoin'"
                    aria-autocomplete="list"
                  />
                </div>
              </Field>
              {showHits && hits.length > 0 && (
                <ul className="absolute inset-x-0 top-full z-20 mt-1 max-h-64 overflow-y-auto rounded-lg border border-line-strong bg-surface py-1 shadow-xl" role="listbox">
                  {hits.map((h) => (
                    <li key={h.ticker}>
                      <button onMouseDown={(e) => e.preventDefault()} onClick={() => pick(h)} className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-2">
                        <span className="mono shrink-0 text-xs text-ink">{h.ticker}</span>
                        <span className="min-w-0 flex-1 truncate text-xs text-muted">{h.title}</span>
                        {h.multiplier !== 1 && <Pill tone="warn">×{h.multiplier}</Pill>}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Price (¢)">
                <input type="number" min={1} max={99} className={cx(inputClass, "num")} value={priceC} onChange={(e) => setPriceC(Number(e.target.value))} />
              </Field>
              <Field label="Contracts">
                <input type="number" min={1} className={cx(inputClass, "num")} value={count} onChange={(e) => setCount(Number(e.target.value))} />
              </Field>
            </div>
            <input type="range" min={1} max={99} value={priceC} onChange={(e) => setPriceC(Number(e.target.value))} className="w-full accent-[var(--accent)]" aria-label="Price in cents" />
            {quoteErr && <p className="text-xs text-crit">{quoteErr}</p>}
            {quote && (
              <div className="rounded-xl bg-surface-2 px-4 py-3">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <div className="text-[11px] text-muted">Taker fee</div>
                    <div className="num text-xl font-semibold text-ink">{usd(quote.taker_fee)}</div>
                    <div className="num text-[11px] text-muted">{pct((quote.taker_fee / quote.cost) * 100)} of cost</div>
                  </div>
                  <div>
                    <div className="text-[11px] text-muted">Maker fee</div>
                    <div className="num text-xl font-semibold text-ink">{usd(quote.maker_fee)}</div>
                    <div className="num text-[11px] text-muted">resting orders</div>
                  </div>
                </div>
                <dl className="mt-3 border-t border-line pt-2">
                  <Kv k="Contracts cost" v={<span className="num">{usd(quote.cost)}</span>} />
                  <Kv k="All-in (taker)" v={<span className="num font-medium">{usd(quote.cost + quote.taker_fee)}</span>} />
                  <Kv k="Per contract (taker)" v={<span className="num">{cents(quote.taker_fee / quote.count)}</span>} />
                  <Kv k="Before rounding" v={<span className="num">${quote.taker_fee_unrounded.toFixed(4)}</span>} />
                  <Kv k="Terms" v={<span title={quote.describe}>{quote.fee_type} ×{quote.multiplier}</span>} />
                  <Kv k="Source" v={quote.source} />
                </dl>
              </div>
            )}
          </div>
        </Card>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card
          title="Scheduled changes"
          subtitle={`${d ? `${num(d.upcoming.length)} in the next 30 days. ` : ""}OMEN prices with the costlier terms from 60 minutes before a change.`}
          action={<CalendarClock size={15} className="text-muted" />}
          pad={false}
        >
          {d ? (
            d.upcoming.length ? (
              <ul className="max-h-[420px] divide-y divide-[var(--border)] overflow-y-auto">
                {d.upcoming.map((u, i) => (
                  <li key={i} className="flex items-start gap-3 px-5 py-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="mono truncate text-xs text-ink" title={u.describe}>
                        {u.ticker}
                      </div>
                      <div className="text-xs text-muted">
                        {u.scope === "event" ? "Event override" : "Series change"} →{" "}
                        {u.fee_type ? `${u.fee_type} ×${u.multiplier ?? 1}` : `×${u.multiplier ?? 1}`}
                      </div>
                    </div>
                    <div className="shrink-0 text-right text-xs">
                      <div className="text-ink">{until(u.scheduled)}</div>
                      <div className="text-muted">{when(u.scheduled)}</div>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState icon={<CalendarClock size={18} />} title="No changes scheduled">
                Kalshi hasn't announced fee changes for the next 30 days. OMEN checks on every scan.
              </EmptyState>
            )
          ) : (
            <div className="p-5">
              <Skeleton className="h-24" />
            </div>
          )}
        </Card>

        <Card title="Fee schedule" action={<ShieldCheck size={15} className="text-muted" />} subtitle={d ? `${num(d.series_count)} series priced live` : undefined}>
          {d ? (
            <>
              <dl className="divide-y divide-[var(--border)]">
                <Kv k="Schedule version" v={d.schedule.version} />
                <Kv k="Verified against the PDF" v={day(d.schedule.verified_on)} />
                <Kv k="Rounding" v={`up to ${usd(d.schedule.rounding_increment)} per fill`} />
                <Kv
                  k="Source"
                  v={
                    <a href={d.schedule.source_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-accent hover:underline">
                      Kalshi fee schedule <ArrowUpRight size={12} />
                    </a>
                  }
                />
              </dl>
              <table className="mt-4 w-full text-left text-xs">
                <thead className="text-[11px] text-muted">
                  <tr className="border-b border-line">
                    <th className="py-1.5 font-medium">Fee type</th>
                    <th className="py-1.5 text-right font-medium">Taker</th>
                    <th className="py-1.5 text-right font-medium">Maker</th>
                    <th className="py-1.5 text-right font-medium">Series</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(d.schedule.formulas).map(([name, f]) => {
                    const n = d.terms.filter((t) => t.fee_type === name).reduce((s, t) => s + t.series, 0);
                    return (
                      <tr key={name} className="border-b border-line last:border-0">
                        <td className="mono py-1.5 text-ink">{name}</td>
                        <td className="num py-1.5 text-right text-ink-2">{f.flat ? "table" : f.taker_rate}</td>
                        <td className="num py-1.5 text-right text-ink-2">{f.flat ? "table" : f.maker_rate}</td>
                        <td className="num py-1.5 text-right text-ink-2">{num(n)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {d.unknown_fee_types.length > 0 && (
                <p className="mt-3 text-xs text-warn">Unknown fee types (markets skipped, fail-closed): {d.unknown_fee_types.join(", ")}</p>
              )}
            </>
          ) : (
            <Skeleton className="h-40" />
          )}
        </Card>
      </div>

      <Card
        className="mt-6"
        title="Series with non-standard terms"
        subtitle="Anything other than plain quadratic at ×1: maker fees, multipliers or flat fees"
        action={
          <div className="relative w-48">
            <Search size={13} className="absolute top-1/2 left-2.5 -translate-y-1/2 text-muted" />
            <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter" className={cx(inputClass, "h-7 pl-7 text-xs")} aria-label="Filter series" />
          </div>
        }
        pad={false}
      >
        {d ? (
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full min-w-[560px] text-left text-xs">
              <thead className="sticky top-0 bg-surface text-[11px] text-muted">
                <tr className="border-b border-line">
                  <th className="px-5 py-2 font-medium">Series</th>
                  <th className="px-2 py-2 font-medium">Category</th>
                  <th className="px-2 py-2 font-medium">Fee type</th>
                  <th className="px-5 py-2 text-right font-medium">Multiplier</th>
                </tr>
              </thead>
              <tbody>
                {nonstandard.slice(0, 300).map((s) => (
                  <tr key={s.ticker} className="cursor-pointer border-b border-line last:border-0 hover:bg-surface-2/60" onClick={() => { setSeries(s.ticker); setQuery(s.ticker); window.scrollTo({ top: 0, behavior: "smooth" }); }}>
                    <td className="max-w-[340px] px-5 py-1.5">
                      <span className="mono text-ink">{s.ticker}</span>
                      <span className="ml-2 truncate text-muted">{s.title}</span>
                    </td>
                    <td className="px-2 py-1.5 text-ink-2">{s.category}</td>
                    <td className="mono px-2 py-1.5 text-ink-2">{s.fee_type}</td>
                    <td className="num px-5 py-1.5 text-right text-ink">{s.multiplier === 0 ? "×0 (no fee)" : `×${s.multiplier}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {nonstandard.length > 300 && <p className="px-5 py-2 text-[11px] text-muted">Showing 300 of {num(nonstandard.length)}. Filter to narrow.</p>}
          </div>
        ) : (
          <div className="p-5">
            <Skeleton className="h-40" />
          </div>
        )}
      </Card>
    </>
  );
}
