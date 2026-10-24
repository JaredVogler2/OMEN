// Small SVG charts following the house data-viz rules: 2px lines, hairline grid, recessive axes, crosshair tooltips,
// a legend whenever there are two or more series (plus direct end labels), text always in ink colours.
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { cx } from "../lib/format";

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => setW(Math.floor(entries[0].contentRect.width)));
    ro.observe(el);
    setW(Math.floor(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export function niceTicks(min: number, max: number, count = 5, minStep = 0): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) {
    const pad = Math.abs(min) || 1;
    min -= pad;
    max += pad;
  }
  const span = max - min;
  const step0 = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(step0));
  const err = step0 / mag;
  const step = Math.max(minStep, (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag);
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(Math.round(v / step) * step);
  return out;
}

function Tooltip({ x, y, width, children }: { x: number; y: number; width: number; children: ReactNode }) {
  const left = x > width * 0.6;
  return (
    <div
      className="pointer-events-none absolute z-10 min-w-[140px] rounded-lg border border-line-strong bg-surface px-3 py-2 text-xs shadow-lg"
      style={{ top: Math.max(0, y - 8), ...(left ? { right: width - x + 12 } : { left: x + 12 }) }}
    >
      {children}
    </div>
  );
}

export function LegendItem({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-2">
      <svg width="16" height="8" aria-hidden>
        <line x1="1" y1="4" x2="15" y2="4" stroke={color} strokeWidth="2" strokeLinecap="round" strokeDasharray={dashed ? "3 3" : undefined} />
      </svg>
      {label}
    </span>
  );
}

// ------------------------------------------------------------------------------------------------ time series
export interface TimePoint {
  t: number; // ms
  y: number;
}

export function TimeChart({
  points,
  height = 180,
  yFormat,
  tooltip,
  color = "var(--s1)",
  zeroLine,
  step,
  label,
  integer,
}: {
  points: TimePoint[];
  height?: number;
  yFormat: (v: number) => string;
  tooltip?: (p: TimePoint) => ReactNode;
  color?: string;
  zeroLine?: boolean;
  step?: boolean;
  label: string;
  integer?: boolean; // counts: ticks on whole numbers only
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const m = { l: 44, r: 12, t: 10, b: 24 };
  const iw = Math.max(10, width - m.l - m.r);
  const ih = height - m.t - m.b;
  const geom = useMemo(() => {
    if (!points.length) return null;
    const t0 = points[0].t;
    const t1 = points[points.length - 1].t;
    let lo = Math.min(...points.map((p) => p.y));
    let hi = Math.max(...points.map((p) => p.y));
    if (zeroLine) {
      lo = Math.min(lo, 0);
      hi = Math.max(hi, 0);
    }
    const ticks = niceTicks(lo, hi, 4, integer ? 1 : 0);
    const y0 = Math.min(lo, ticks[0] ?? lo);
    const y1 = Math.max(hi, ticks[ticks.length - 1] ?? hi);
    const sx = (t: number) => (t1 === t0 ? iw / 2 : ((t - t0) / (t1 - t0)) * iw);
    const sy = (v: number) => (y1 === y0 ? ih / 2 : ih - ((v - y0) / (y1 - y0)) * ih);
    let d = "";
    points.forEach((p, i) => {
      const x = sx(p.t);
      const y = sy(p.y);
      if (i === 0) d += `M${x},${y}`;
      else if (step) d += `H${x}V${y}`;
      else d += `L${x},${y}`;
    });
    const span = t1 - t0;
    const fmt: Intl.DateTimeFormatOptions =
      span > 3 * 86400e3 ? { month: "short", day: "numeric" } : { hour: "numeric", minute: "2-digit" };
    const xt = Math.max(2, Math.min(6, Math.floor(iw / 90)));
    const xTicks = Array.from({ length: xt }, (_, i) => t0 + (span * i) / (xt - 1));
    return { sx, sy, d, ticks, xTicks, fmt };
  }, [points, iw, ih, zeroLine, step, integer]);

  const onMove = (e: React.PointerEvent<SVGRectElement>) => {
    if (!geom) return;
    const r = (e.currentTarget as SVGRectElement).getBoundingClientRect();
    const x = e.clientX - r.left;
    let best = 0;
    let bd = Infinity;
    points.forEach((p, i) => {
      const dd = Math.abs(geom.sx(p.t) - x);
      if (dd < bd) {
        bd = dd;
        best = i;
      }
    });
    setHover(best);
  };

  const hp = hover != null ? points[hover] : null;
  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 && geom && (
        <svg width={width} height={height} role="img" aria-label={label}>
          <g transform={`translate(${m.l},${m.t})`}>
            {geom.ticks.map((v) => (
              <g key={v}>
                <line x1={0} x2={iw} y1={geom.sy(v)} y2={geom.sy(v)} stroke="var(--grid)" strokeWidth={1} />
                <text x={-8} y={geom.sy(v)} dy="0.32em" textAnchor="end" className="num fill-muted text-[10px]">
                  {yFormat(v)}
                </text>
              </g>
            ))}
            {zeroLine && <line x1={0} x2={iw} y1={geom.sy(0)} y2={geom.sy(0)} stroke="var(--axis)" strokeWidth={1} />}
            {geom.xTicks.map((t, i) => (
              <text
                key={i}
                x={geom.sx(t)}
                y={ih + 16}
                textAnchor={i === 0 ? "start" : i === geom.xTicks.length - 1 ? "end" : "middle"}
                className="fill-muted text-[10px]"
              >
                {new Date(t).toLocaleString(undefined, geom.fmt)}
              </text>
            ))}
            <path d={geom.d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {hp && (
              <>
                <line x1={geom.sx(hp.t)} x2={geom.sx(hp.t)} y1={0} y2={ih} stroke="var(--axis)" strokeWidth={1} />
                <circle cx={geom.sx(hp.t)} cy={geom.sy(hp.y)} r={4.5} fill={color} stroke="var(--surface)" strokeWidth={2} />
              </>
            )}
            <rect
              width={iw}
              height={ih}
              fill="transparent"
              onPointerMove={onMove}
              onPointerLeave={() => setHover(null)}
              style={{ touchAction: "pan-y" }}
            />
          </g>
        </svg>
      )}
      {hp && geom && (
        <Tooltip x={m.l + geom.sx(hp.t)} y={m.t + geom.sy(hp.y)} width={width}>
          <div className="text-muted">{new Date(hp.t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</div>
          <div className="num mt-0.5 font-semibold text-ink">{tooltip ? tooltip(hp) : yFormat(hp.y)}</div>
        </Tooltip>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------- fee curves
export interface Curve {
  name: string;
  color: string;
  dashed?: boolean;
  f: (p: number) => number; // price (0–1) → fee per contract in dollars
}

/** Fee per contract across the price range, one line per fee term; optional marker at the quoted price. */
export function FeeCurves({ curves, marker, height = 240 }: { curves: Curve[]; marker?: number; height?: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const m = { l: 44, r: 92, t: 12, b: 28 };
  const iw = Math.max(10, width - m.l - m.r);
  const ih = height - m.t - m.b;
  const xs = useMemo(() => Array.from({ length: 99 }, (_, i) => (i + 1) / 100), []);
  const ymax = Math.max(0.005, ...curves.flatMap((c) => xs.map((x) => c.f(x))));
  const ticks = niceTicks(0, ymax, 4);
  const top = Math.max(ymax, ticks[ticks.length - 1] ?? ymax);
  const sx = (p: number) => p * iw;
  const sy = (v: number) => ih - (v / top) * ih;
  const path = (c: Curve) => xs.map((x, i) => `${i ? "L" : "M"}${sx(x)},${sy(c.f(x))}`).join("");

  // direct end labels, nudged apart so they never collide
  const labels = curves
    .map((c) => ({ c, y: sy(c.f(0.5)) }))
    .sort((a, b) => a.y - b.y)
    .reduce<{ c: Curve; y: number }[]>((acc, l) => {
      const prev = acc[acc.length - 1];
      acc.push({ ...l, y: prev && l.y - prev.y < 13 ? prev.y + 13 : l.y });
      return acc;
    }, []);

  const hx = hover;
  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label="Fee per contract by price">
          <g transform={`translate(${m.l},${m.t})`}>
            {ticks.map((v) => (
              <g key={v}>
                <line x1={0} x2={iw} y1={sy(v)} y2={sy(v)} stroke="var(--grid)" strokeWidth={1} />
                <text x={-8} y={sy(v)} dy="0.32em" textAnchor="end" className="num fill-muted text-[10px]">
                  {Number.isInteger(Math.round(v * 1000) / 10) ? (v * 100).toFixed(0) : (v * 100).toFixed(1)}¢
                </text>
              </g>
            ))}
            {[0.1, 0.3, 0.5, 0.7, 0.9].map((p) => (
              <text key={p} x={sx(p)} y={ih + 16} textAnchor="middle" className="num fill-muted text-[10px]">
                {Math.round(p * 100)}¢
              </text>
            ))}
            <text x={iw / 2} y={ih + 27} textAnchor="middle" className="fill-muted text-[10px]">
              contract price
            </text>
            {curves.map((c) => (
              <path
                key={c.name}
                d={path(c)}
                fill="none"
                stroke={c.color}
                strokeWidth={2}
                strokeDasharray={c.dashed ? "5 4" : undefined}
                strokeLinejoin="round"
              />
            ))}
            {labels.map(({ c, y }) => (
              <text key={c.name} x={iw + 8} y={y} dy="0.32em" className="fill-ink-2 text-[10px]">
                {c.name}
              </text>
            ))}
            {marker != null && marker > 0 && marker < 1 && (
              <line x1={sx(marker)} x2={sx(marker)} y1={0} y2={ih} stroke="var(--text-2)" strokeDasharray="2 3" strokeWidth={1} />
            )}
            {hx != null && (
              <>
                <line x1={sx(hx)} x2={sx(hx)} y1={0} y2={ih} stroke="var(--axis)" strokeWidth={1} />
                {curves.map((c) => (
                  <circle key={c.name} cx={sx(hx)} cy={sy(c.f(hx))} r={4} fill={c.color} stroke="var(--surface)" strokeWidth={2} />
                ))}
              </>
            )}
            <rect
              width={iw}
              height={ih}
              fill="transparent"
              onPointerMove={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                setHover(Math.min(0.99, Math.max(0.01, Math.round(((e.clientX - r.left) / iw) * 100) / 100)));
              }}
              onPointerLeave={() => setHover(null)}
              style={{ touchAction: "pan-y" }}
            />
          </g>
        </svg>
      )}
      {hx != null && (
        <Tooltip x={m.l + sx(hx)} y={m.t + 4} width={width}>
          <div className="text-muted">
            price <span className="num text-ink">{Math.round(hx * 100)}¢</span> · per contract
          </div>
          {curves.map((c) => (
            <div key={c.name} className="mt-1 flex items-center justify-between gap-4">
              <span className="inline-flex items-center gap-1.5 text-ink-2">
                <span className="inline-block h-0.5 w-3 rounded" style={{ background: c.color }} />
                {c.name}
              </span>
              <span className="num font-medium text-ink">{(c.f(hx) * 100).toFixed(2)}¢</span>
            </div>
          ))}
        </Tooltip>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------------------------- crypto ladder
export interface LadderPoint {
  key: string;
  label: string;
  bid: number | null;
  ask: number | null;
  lo: number;
  hi: number;
  central: number;
  signal: "yes" | "no" | null;
  alert: boolean;
  detail: ReactNode;
}

/** Per strike: the market's bid–ask range (orange bar) against the model's worst-case band (blue band + dot). */
export function LadderChart({ rows, height = 300, spotIndex }: { rows: LadderPoint[]; height?: number; spotIndex?: number | null }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const m = { l: 40, r: 12, t: 12, b: 46 };
  const iw = Math.max(10, width - m.l - m.r);
  const ih = height - m.t - m.b;
  const n = rows.length;
  const bw = n ? iw / n : iw;
  const cxOf = (i: number) => bw * i + bw / 2;
  const sy = (p: number) => ih - p * ih;
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(iw / 64))));
  const bandW = Math.max(4, Math.min(18, bw * 0.55));
  const barW = Math.max(3, Math.min(8, bw * 0.22));
  const h = hover != null ? rows[hover] : null;
  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label="Market prices versus model probability by strike">
          <g transform={`translate(${m.l},${m.t})`}>
            {[0, 0.25, 0.5, 0.75, 1].map((v) => (
              <g key={v}>
                <line x1={0} x2={iw} y1={sy(v)} y2={sy(v)} stroke="var(--grid)" strokeWidth={1} />
                <text x={-8} y={sy(v)} dy="0.32em" textAnchor="end" className="num fill-muted text-[10px]">
                  {Math.round(v * 100)}%
                </text>
              </g>
            ))}
            {spotIndex != null && spotIndex >= 0 && (
              <g>
                <line x1={spotIndex * bw} x2={spotIndex * bw} y1={0} y2={ih} stroke="var(--text-2)" strokeDasharray="2 3" />
                <text x={spotIndex * bw + 4} y={10} className="fill-ink-2 text-[10px]">
                  spot
                </text>
              </g>
            )}
            {hover != null && <rect x={bw * hover} width={bw} height={ih} fill="var(--surface-2)" />}
            {rows.map((r, i) => (
              <g key={r.key}>
                <rect
                  x={cxOf(i) - bandW / 2}
                  y={sy(r.hi)}
                  width={bandW}
                  height={Math.max(2, sy(r.lo) - sy(r.hi))}
                  rx={4}
                  fill="var(--s1)"
                  opacity={0.28}
                />
                <circle cx={cxOf(i)} cy={sy(r.central)} r={4} fill="var(--s1)" stroke="var(--surface)" strokeWidth={2} />
                {r.bid != null && r.ask != null && (
                  <rect
                    x={cxOf(i) + bandW / 2 + 2}
                    y={sy(r.ask)}
                    width={barW}
                    height={Math.max(2, sy(r.bid) - sy(r.ask))}
                    rx={Math.min(4, barW / 2)}
                    fill="var(--s2)"
                    stroke="var(--surface)"
                    strokeWidth={1}
                  />
                )}
                {r.signal && (
                  <text
                    x={cxOf(i)}
                    y={sy(r.signal === "yes" ? r.hi : r.lo) + (r.signal === "yes" ? -8 : 14)}
                    textAnchor="middle"
                    className={cx("text-[10px] font-semibold", r.alert ? "fill-ink" : "fill-muted")}
                  >
                    {r.signal === "yes" ? "▲" : "▼"}
                  </text>
                )}
                {i % every === 0 && (
                  <text
                    x={cxOf(i)}
                    y={ih + 14}
                    textAnchor="end"
                    transform={`rotate(-30 ${cxOf(i)} ${ih + 14})`}
                    className="num fill-muted text-[10px]"
                  >
                    {r.label}
                  </text>
                )}
                <rect
                  x={bw * i}
                  width={bw}
                  height={ih}
                  fill="transparent"
                  onPointerEnter={() => setHover(i)}
                  onPointerLeave={() => setHover(null)}
                />
              </g>
            ))}
          </g>
        </svg>
      )}
      {h && hover != null && (
        <Tooltip x={m.l + cxOf(hover)} y={m.t + sy(h.hi)} width={width}>
          {h.detail}
        </Tooltip>
      )}
    </div>
  );
}

/** Tiny inline trend line (no axes) for KPI tiles. */
export function Sparkline({ values, color = "var(--s1)", height = 28 }: { values: number[]; color?: string; height?: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  if (values.length < 2) return <div ref={ref} style={{ height }} />;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const sx = (i: number) => (i / (values.length - 1)) * (width - 4) + 2;
  const sy = (v: number) => (hi === lo ? height / 2 : height - 3 - ((v - lo) / (hi - lo)) * (height - 6));
  const d = values.map((v, i) => `${i ? "L" : "M"}${sx(i)},${sy(v)}`).join("");
  return (
    <div ref={ref} style={{ height }} aria-hidden>
      {width > 0 && (
        <svg width={width} height={height}>
          <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        </svg>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------- interval (forest)
export interface IntervalRow {
  key: string;
  label: string;
  mid: number;
  lo: number;
  hi: number;
  second?: number | null; // e.g. the held-out estimate
  highlight?: boolean;
  detail: ReactNode;
}

/** Estimates with 95% intervals per category on a shared value axis, a zero reference, and an optional second
 * point per row (hollow) for a comparison estimate. Values are in the caller's unit (cents here). */
export function IntervalChart({
  rows,
  height = 260,
  format,
  label,
}: {
  rows: IntervalRow[];
  height?: number;
  format: (v: number) => string;
  label: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const m = { l: 44, r: 12, t: 12, b: 34 };
  const iw = Math.max(10, width - m.l - m.r);
  const ih = height - m.t - m.b;
  const vals = rows.flatMap((r) => [r.lo, r.hi, r.second ?? r.mid, 0]);
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals), 5);
  const lo = Math.min(...vals, ticks[0] ?? 0);
  const hi = Math.max(...vals, ticks[ticks.length - 1] ?? 0);
  const sy = (v: number) => (hi === lo ? ih / 2 : ih - ((v - lo) / (hi - lo)) * ih);
  const bw = rows.length ? iw / rows.length : iw;
  const cxOf = (i: number) => bw * i + bw / 2;
  const h = hover != null ? rows[hover] : null;
  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 && rows.length > 0 && (
        <svg width={width} height={height} role="img" aria-label={label}>
          <g transform={`translate(${m.l},${m.t})`}>
            {ticks.map((v) => (
              <g key={v}>
                <line x1={0} x2={iw} y1={sy(v)} y2={sy(v)} stroke="var(--grid)" strokeWidth={1} />
                <text x={-8} y={sy(v)} dy="0.32em" textAnchor="end" className="num fill-muted text-[10px]">
                  {format(v)}
                </text>
              </g>
            ))}
            <line x1={0} x2={iw} y1={sy(0)} y2={sy(0)} stroke="var(--text-2)" strokeWidth={1} />
            {hover != null && <rect x={bw * hover} width={bw} height={ih} fill="var(--surface-2)" />}
            {rows.map((r, i) => (
              <g key={r.key}>
                <line x1={cxOf(i)} x2={cxOf(i)} y1={sy(r.hi)} y2={sy(r.lo)} stroke="var(--s1)" strokeWidth={2} strokeLinecap="round" />
                <circle cx={cxOf(i)} cy={sy(r.mid)} r={r.highlight ? 6 : 4.5} fill="var(--s1)" stroke="var(--surface)" strokeWidth={2} />
                {r.second != null && (
                  <circle cx={cxOf(i) + Math.min(14, bw * 0.25)} cy={sy(r.second)} r={4.5} fill="var(--surface)" stroke="var(--s2)" strokeWidth={2} />
                )}
                <text x={cxOf(i)} y={ih + 16} textAnchor="middle" className="num fill-muted text-[10px]">
                  {r.label}
                </text>
                <rect x={bw * i} width={bw} height={ih} fill="transparent" onPointerEnter={() => setHover(i)} onPointerLeave={() => setHover(null)} />
              </g>
            ))}
          </g>
        </svg>
      )}
      {h && hover != null && (
        <Tooltip x={m.l + cxOf(hover)} y={m.t + sy(h.hi)} width={width}>
          {h.detail}
        </Tooltip>
      )}
    </div>
  );
}
