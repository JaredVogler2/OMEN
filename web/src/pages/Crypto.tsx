import { FlaskConical, RefreshCw } from "lucide-react";
import { useMemo } from "react";
import { PageHeader } from "../App";
import { type CryptoView, api } from "../api";
import { LadderChart, LegendItem, type LadderPoint } from "../components/charts";
import { Button, Card, EmptyState, NoticeBanner, Pill, Segmented, Skeleton, Stat } from "../components/ui";
import { ago, cents, clean, cx, pct, price, prob, until } from "../lib/format";
import { usePoll, useStored } from "../lib/hooks";

const ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"] as const;
type Asset = (typeof ASSETS)[number];

export default function Crypto() {
  const [asset, setAsset] = useStored<Asset>("omen-crypto-asset", "BTC");
  const view = usePoll((s) => api.crypto(asset, s), 60000, [asset]);
  const v = view.data && view.data.asset === asset ? view.data : null;

  return (
    <>
      <PageHeader
        title="Crypto threshold model"
        subtitle="Prices Kalshi's daily crypto ladders from live spot (median of Coinbase, Kraken and Bitstamp) and realized plus implied volatility, taking the worst case across volatility, fat tails and settlement basis. It runs in shadow mode until its calibration gates pass on resolved predictions."
        actions={
          <>
            <Segmented label="Asset" value={asset} onChange={setAsset} options={ASSETS.map((a) => ({ value: a, label: a }))} />
            <Button icon={<RefreshCw size={14} />} onClick={view.refresh} loading={view.loading}>
              Refresh
            </Button>
          </>
        }
      />
      <div className="mb-5">
        <NoticeBanner
          notice={{
            severity: "info",
            title: "Shadow mode: model signals are not plays yet",
            body: "Every prediction is logged and scored when its market settles. MODEL plays only reach the Plays page and Discord after the calibration gates pass and you run `omen promote` on the Pi.",
          }}
        />
      </div>
      {view.error && <NoticeBanner notice={{ severity: "critical", title: "Could not load the model view", body: view.error.message }} />}

      {!v ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-[92px]" />
            ))}
          </div>
          <Skeleton className="h-[380px]" />
        </div>
      ) : (
        <Body v={v} />
      )}
    </>
  );
}

function Body({ v }: { v: CryptoView }) {
  const sources = Object.entries(v.sources);
  return (
    <>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label={`${v.asset} spot`} value={<span className="num">${price(v.spot)}</span>} sub={`median of ${sources.length} exchanges · as of ${ago(v.as_of)}`} />
        <Stat
          label="Exchange dispersion"
          value={<span className="num">{pct(v.dispersion_pct, 3)}</span>}
          sub={sources.map(([k, p]) => `${k} ${price(p)}`).join(" · ")}
          tone={v.dispersion_pct > 0.5 ? "warn" : undefined}
        />
        <Stat label="Volatility used" value={<span className="num">{pct(v.vol.central * 100, 1)}</span>} sub={`realized ${fmtVol(v.vol.short)} / ${fmtVol(v.vol.long)} · implied ${fmtVol(v.vol.implied)}`} />
        <Stat
          label="Alert bar"
          value={<span className="num">{cents(v.settings.min_edge)} edge</span>}
          sub={`p ≥ ${prob(v.settings.min_conservative_probability, 0)} worst case · vol ×${v.settings.vol_safety_multiplier}`}
        />
      </div>
      {v.series.length === 0 ? (
        <Card className="mt-6">
          <EmptyState icon={<FlaskConical size={20} />} title={`No open ${v.asset} ladders right now`}>
            Kalshi lists these daily; check back after the next one opens.
          </EmptyState>
        </Card>
      ) : (
        v.series.map((s) => <SeriesChart key={s.series} s={s} spot={v.spot} />)
      )}
    </>
  );
}

function fmtVol(x: number | null) {
  return x == null ? "n/a" : `${(x * 100).toFixed(0)}%`;
}

function SeriesChart({ s, spot }: { s: CryptoView["series"][number]; spot: number }) {
  const rows: LadderPoint[] = useMemo(
    () =>
      s.markets.map((r) => ({
        key: r.ticker,
        label: shortLabel(r.lo, r.hi),
        bid: r.yes_bid,
        ask: r.yes_ask,
        lo: r.p_low,
        hi: r.p_high,
        central: r.p_central,
        signal: r.signal && r.signal.edge > 0 ? r.signal.side : null,
        alert: !!r.signal?.would_alert,
        detail: (
          <div className="space-y-0.5">
            <div className="font-semibold text-ink">{r.label}</div>
            <div className="flex justify-between gap-4 text-ink-2">
              <span>Market bid–ask</span>
              <span className="num text-ink">
                {cents(r.yes_bid)} – {cents(r.yes_ask)}
              </span>
            </div>
            <div className="flex justify-between gap-4 text-ink-2">
              <span>Model band</span>
              <span className="num text-ink">
                {prob(r.p_low)} – {prob(r.p_high)}
              </span>
            </div>
            <div className="flex justify-between gap-4 text-ink-2">
              <span>Model central</span>
              <span className="num text-ink">{prob(r.p_central)}</span>
            </div>
            {r.signal && (
              <div className="flex justify-between gap-4 text-ink-2">
                <span>Best side after fees</span>
                <span className="num text-ink">
                  {r.signal.side.toUpperCase()} {r.signal.edge >= 0 ? "+" : "−"}
                  {Math.abs(r.signal.edge * 100).toFixed(1)}¢
                </span>
              </div>
            )}
          </div>
        ),
      })),
    [s],
  );
  // Only the strikes where something is uncertain (model or market between ~1% and ~99%), plus two on each side;
  // the deep in/out-of-the-money tails carry no information.
  const live = (r: LadderPoint) =>
    (r.hi > 0.01 && r.lo < 0.99) || [r.bid, r.ask].some((p) => p != null && p > 0.02 && p < 0.98);
  const first = rows.findIndex(live);
  const last = rows.length - 1 - [...rows].reverse().findIndex(live);
  const visible = first >= 0 ? rows.slice(Math.max(0, first - 2), Math.min(rows.length, last + 3)) : rows;
  const spotIdx = visible.findIndex((r) => {
    const m = s.markets.find((x) => x.ticker === r.key);
    const strike = m?.lo ?? m?.hi ?? null;
    return strike != null && strike > spot;
  });
  const signals = s.markets.filter((r) => r.signal && r.signal.edge > 0);
  const alerts = s.markets.filter((r) => r.signal?.would_alert);

  return (
    <Card
      className="mt-6"
      title={
        <span className="flex flex-wrap items-center gap-2">
          {clean(s.title)}
          <span className="mono text-[11px] font-normal text-muted">{s.event_ticker}</span>
        </span>
      }
      subtitle={`${s.kind === "range" ? "Range buckets" : "Above/below thresholds"} · showing ${visible.length} of ${s.markets.length} strikes · settles ${until(s.close_time)}`}
      action={
        alerts.length ? <Pill tone="model">{alerts.length} would alert</Pill> : signals.length ? <Pill>{signals.length} positive, below bar</Pill> : <Pill>no edge</Pill>
      }
    >
      <div className="mb-2 flex flex-wrap items-center gap-4">
        <LegendItem color="var(--s1)" label="Model worst-case band (dot = central)" />
        <LegendItem color="var(--s2)" label="Market YES bid–ask" />
        <span className="text-xs text-ink-2">▲/▼ positive edge buying YES/NO</span>
      </div>
      <LadderChart rows={visible} spotIndex={spotIdx >= 0 ? spotIdx : null} height={300} />
      {signals.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[520px] text-left text-xs">
            <thead className="text-[11px] text-muted">
              <tr className="border-b border-line">
                <th className="py-1.5 font-medium">Strike</th>
                <th className="py-1.5 font-medium">Side</th>
                <th className="py-1.5 text-right font-medium">Edge after fees</th>
                <th className="py-1.5 text-right font-medium">Worst-case p</th>
                <th className="py-1.5 text-right font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {signals
                .sort((a, b) => (b.signal?.edge ?? 0) - (a.signal?.edge ?? 0))
                .slice(0, 8)
                .map((r) => (
                  <tr key={r.ticker} className="border-b border-line last:border-0">
                    <td className="py-1.5 text-ink">{r.label}</td>
                    <td className="py-1.5 text-ink-2">{r.signal!.side.toUpperCase()}</td>
                    <td className="num py-1.5 text-right text-ink">+{(r.signal!.edge * 100).toFixed(1)}¢</td>
                    <td className="num py-1.5 text-right text-ink-2">{prob(r.signal!.p_conservative)}</td>
                    <td className={cx("py-1.5 text-right", r.signal!.would_alert ? "text-model" : "text-muted")}>{r.signal!.would_alert ? "would alert" : "below bar"}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function shortLabel(lo: number | null, hi: number | null): string {
  const f = (x: number) => (x >= 10000 ? `${(x / 1000).toFixed(x % 1000 ? 2 : 0)}k` : x >= 100 ? x.toFixed(0) : x >= 1 ? x.toFixed(2) : x.toPrecision(3));
  if (lo != null && hi != null) return f(lo);
  if (lo != null) return `≥${f(lo)}`;
  if (hi != null) return `≤${f(hi)}`;
  return "?";
}
