import { FlaskConical, Scale, Target } from "lucide-react";
import { useMemo } from "react";
import { PageHeader } from "../App";
import { type AtlasCell, api } from "../api";
import { IntervalChart, LegendItem } from "../components/charts";
import { Card, EmptyState, NoticeBanner, Pill, Segmented, Skeleton, Stat } from "../components/ui";
import { cx, day, num } from "../lib/format";
import { usePoll, useStored } from "../lib/hooks";

const HORIZONS = ["1h", "6h", "24h", "72h"] as const;
type Horizon = (typeof HORIZONS)[number];

const c = (v: number) => `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(1)}¢`;
const band = (b: [number, number]) => `${Math.round(b[0] * 100)}–${Math.round(b[1] * 100)}¢`;

export default function Edges() {
  const atlas = usePoll((s) => api.atlas(s), 0);
  const [horizon, setHorizon] = useStored<Horizon>("omen-edges-horizon", "24h");
  const [category, setCategory] = useStored<string>("omen-edges-category", "All");
  const d = atlas.data;

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const cell of d?.cells ?? []) counts.set(cell.category, (counts.get(cell.category) ?? 0) + cell.discovery.n);
    return ["All", ...[...counts.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k)];
  }, [d]);

  const rows: AtlasCell[] = useMemo(() => {
    const pool = category === "All" ? d?.overall ?? [] : (d?.cells ?? []).filter((x) => x.category === category);
    return pool.filter((x) => x.horizon === horizon).sort((a, b) => a.band[0] - b.band[0]);
  }, [d, category, horizon]);

  return (
    <>
      <PageHeader
        title="Edges"
        subtitle="Where Kalshi's prices have been wrong after fees, measured on settled markets. Each cell asks: when a contract cost this much this long before the close, how often did it actually win, and what did a buyer make per contract after the taker fee? Only cells profitable with 95% confidence and again on held-out weeks become edges."
      />
      {atlas.error && <NoticeBanner notice={{ severity: "critical", title: "Could not load the atlas", body: atlas.error.message }} />}
      {!d ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-[92px]" />
          ))}
        </div>
      ) : d.source === "none" || !d.markets ? (
        <Card>
          <EmptyState icon={<FlaskConical size={20} />} title="No atlas yet">
            Run <code className="mono">omen atlas</code> on the Pi (or install the weekly timer with{" "}
            <code className="mono">scripts/install-pi.sh --with-atlas-timer</code>). It reads Kalshi's settled markets and publishes the result here.
          </EmptyState>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Validated edges" icon={<Target size={14} />} value={<span className="num">{d.edges.length}</span>} sub={d.edges.length ? "profitable twice, after fees" : "none right now"} />
            <Stat label="Markets measured" value={<span className="num">{num(d.markets)}</span>} sub={`${num(d.series ?? 0)} series · ${num(d.observations ?? 0)} prices`} />
            <Stat label="Held-out weeks from" value={<span className="text-xl">{day(d.validation_from)}</span>} sub="never used to find edges" />
            <Stat label="Built" value={<span className="text-xl">{day(d.generated_at)}</span>} sub={d.source === "pi" ? "by the Pi (weekly)" : "snapshot in the repository"} />
          </div>

          <Card className="mt-6" title="Validated edges" subtitle="Live markets in these cells are scored by the calibration-edge model (shadow until promoted)">
            {d.edges.length ? (
              <ul className="divide-y divide-[var(--border)]">
                {d.edges.map((e) => (
                  <EdgeRow key={`${e.category}${e.horizon}${e.band}`} cell={e} />
                ))}
              </ul>
            ) : (
              <EmptyState icon={<Scale size={20} />} title="No price band beat the market after fees">
                In every category, time-to-close and price band measured, buying at Kalshi's prices lost money or only broke even once the taker fee and
                spread were paid. That is useful too: the plays that pay are structural locks, not cheap-looking contracts. The atlas is rebuilt weekly, and an
                edge appears here as soon as one holds up on held-out weeks.
              </EmptyState>
            )}
          </Card>

          <Card
            className="mt-6"
            title="Profit per contract after fees, by price"
            subtitle="Buying at the ask (YES) or at 1 − bid (NO) at that time before the close. Dot: older weeks with 95% interval (resampling whole series). Ring: held-out recent weeks."
            action={
              <div className="hidden items-center gap-3 sm:flex">
                <LegendItem color="var(--s1)" label="Discovery ±95%" />
                <LegendItem color="var(--s2)" label="Held-out weeks" />
              </div>
            }
          >
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <Segmented label="Time before close" value={horizon} onChange={setHorizon} options={HORIZONS.map((h) => ({ value: h, label: `${h} before` }))} />
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="focus-ring h-8 rounded-lg border border-line bg-surface-2 px-2 text-xs text-ink"
                aria-label="Category"
              >
                {categories.map((x) => (
                  <option key={x}>{x}</option>
                ))}
              </select>
            </div>
            {rows.length ? (
              <IntervalChart
                label="Profit per contract after fees by price band"
                format={(v) => `${v >= 0 ? "" : "−"}${Math.abs(v).toFixed(0)}¢`}
                rows={rows.map((r) => ({
                  key: r.band.join("-"),
                  label: band(r.band),
                  mid: r.discovery.ev * 100,
                  lo: r.discovery.ev_lo * 100,
                  hi: r.discovery.ev_hi * 100,
                  second: r.validation ? r.validation.ev * 100 : null,
                  highlight: r.edge,
                  detail: <CellDetail cell={r} />,
                }))}
              />
            ) : (
              <p className="text-[13px] text-muted">Not enough settled markets in this cell yet.</p>
            )}
            <div className="mt-2 flex flex-wrap items-center gap-3 sm:hidden">
              <LegendItem color="var(--s1)" label="Discovery ±95%" />
              <LegendItem color="var(--s2)" label="Held-out weeks" />
            </div>
          </Card>

          <Card className="mt-6" title="Every cell" subtitle={`${category} · ${horizon} before close`} pad={false}>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-left text-xs">
                <thead className="text-[11px] text-muted">
                  <tr className="border-b border-line">
                    <th className="px-5 py-2 font-medium">Price paid</th>
                    <th className="px-2 py-2 text-right font-medium">Contracts</th>
                    <th className="px-2 py-2 text-right font-medium">Series</th>
                    <th className="px-2 py-2 text-right font-medium">Won</th>
                    <th className="px-2 py-2 text-right font-medium">Avg price</th>
                    <th className="px-2 py-2 text-right font-medium">Profit / contract (95%)</th>
                    <th className="px-5 py-2 text-right font-medium">Held-out</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.band.join("-")} className={cx("border-b border-line last:border-0", r.edge && "bg-accent-soft")}>
                      <td className="px-5 py-1.5 text-ink">
                        {band(r.band)} {r.edge && <Pill tone="accent">edge</Pill>}
                      </td>
                      <td className="num px-2 py-1.5 text-right text-ink-2">{num(r.discovery.n)}</td>
                      <td className="num px-2 py-1.5 text-right text-ink-2">{num(r.discovery.series)}</td>
                      <td className="num px-2 py-1.5 text-right text-ink">{(r.discovery.win_rate * 100).toFixed(1)}%</td>
                      <td className="num px-2 py-1.5 text-right text-ink-2">{(r.discovery.avg_price * 100).toFixed(1)}¢</td>
                      <td className="num px-2 py-1.5 text-right text-ink">
                        {c(r.discovery.ev)} <span className="text-muted">[{c(r.discovery.ev_lo)}, {c(r.discovery.ev_hi)}]</span>
                      </td>
                      <td className="num px-5 py-1.5 text-right text-ink-2">{r.validation ? `${c(r.validation.ev)} (${r.validation.n})` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card className="mt-6" title="How confidence is scored" subtitle="For live markets that fall in a validated edge">
            <ol className="list-decimal space-y-2 pl-5 text-[13px] leading-relaxed text-ink-2">
              <li>
                The cell's measured mispricing (win rate − price) is <b>shrunk toward zero</b> in proportion to its uncertainty, so small samples and lucky
                streaks count for less (normal prior, standard deviation 3¢).
              </li>
              <li>
                That gives an estimated <b>true win probability</b> for the live contract and a 90% lower bound, plus <b>P(edge after fees &gt; 0)</b>. A signal
                needs at least 90% and an expected profit of at least 1¢ per contract after the fee.
              </li>
              <li>
                The <b>payout multiple</b> (what $1 staked returns if it wins) shows how asymmetric the bet is. The suggested <b>stake</b> is ¼ of the Kelly
                bet on your bankroll, capped, and never more than the order book supports at a profitable price.
              </li>
              <li>
                Every evaluation is logged and scored when the market settles. Signals stay in <b>shadow</b> (#kalshi-research) until the calibration gates
                pass on real outcomes and you run <code className="mono">omen promote calibration_edge</code>.
              </li>
            </ol>
          </Card>
        </>
      )}
    </>
  );
}

function EdgeRow({ cell }: { cell: AtlasCell }) {
  const dd = cell.discovery;
  const v = cell.validation;
  const mult = (1 - dd.avg_price) / dd.avg_price;
  return (
    <li className="flex flex-wrap items-center gap-x-6 gap-y-1 py-3">
      <div className="min-w-[180px]">
        <div className="text-[13px] font-semibold text-ink">
          {cell.category} · {band(cell.band)}
        </div>
        <div className="text-xs text-muted">{cell.horizon} before close</div>
      </div>
      <Metric k="won" v={`${(dd.win_rate * 100).toFixed(1)}% at ${(dd.avg_price * 100).toFixed(1)}¢`} />
      <Metric k="profit / contract" v={`${c(dd.ev)} [${c(dd.ev_lo)}, ${c(dd.ev_hi)}]`} />
      <Metric k="held-out" v={v ? `${c(v.ev)} over ${num(v.n)}` : "—"} />
      <Metric k="pays if it wins" v={`${mult.toFixed(1)}×`} />
      <Metric k="sample" v={`${num(dd.n)} · ${num(dd.series)} series`} />
    </li>
  );
}

function Metric({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <div className="text-[11px] text-muted">{k}</div>
      <div className="num text-[13px] text-ink">{v}</div>
    </div>
  );
}

function CellDetail({ cell }: { cell: AtlasCell }) {
  const dd = cell.discovery;
  return (
    <div className="space-y-0.5">
      <div className="font-semibold text-ink">
        {band(cell.band)} · {cell.horizon} before close
      </div>
      <Line k="Won" v={`${(dd.win_rate * 100).toFixed(1)}% vs ${(dd.avg_price * 100).toFixed(1)}¢ paid`} />
      <Line k="Profit / contract" v={c(dd.ev)} />
      <Line k="95% interval" v={`${c(dd.ev_lo)} to ${c(dd.ev_hi)}`} />
      <Line k="Held-out weeks" v={cell.validation ? `${c(cell.validation.ev)} (${cell.validation.n})` : "—"} />
      <Line k="Sample" v={`${num(dd.n)} contracts · ${num(dd.series)} series`} />
    </div>
  );
}

function Line({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-4 text-ink-2">
      <span>{k}</span>
      <span className="num text-ink">{v}</span>
    </div>
  );
}
