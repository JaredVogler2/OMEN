import { ArrowUpRight, CircleCheck, CircleX, Clock, Lock, RefreshCw, ShieldAlert, Sparkles, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { type Play, api } from "../api";
import { ago, cents, clean, cx, kalshiUrl, num, pct, prob, signedUsd, until, usd, when } from "../lib/format";
import { Button, CopyButton, Drawer, Kv, Pill, TierBadge } from "./ui";

export function SourcePill({ source }: { source: Play["source"] }) {
  return source === "pi" ? (
    <Pill title="Found by the always-on Pi scanner">Pi live</Pill>
  ) : source === "reverify" ? (
    <Pill tone="accent">re-verified</Pill>
  ) : (
    <Pill title="Found by an on-demand scan from this site">Web scan</Pill>
  );
}

export function PlayCard({ play, onOpen, now }: { play: Play; onOpen: () => void; now: number }) {
  const lock = play.tier === "LOCK";
  return (
    <button
      onClick={onOpen}
      className={cx(
        "card focus-ring group relative flex w-full flex-col overflow-hidden p-0 text-left transition hover:border-line-strong",
        play.suspicious && "opacity-80",
      )}
    >
      <span className={cx("absolute inset-y-0 left-0 w-[3px]", lock ? "bg-lock" : "bg-model")} aria-hidden />
      <div className="flex items-center gap-2 px-5 pt-4">
        <TierBadge tier={play.tier} />
        <span className="text-xs font-medium text-ink-2">{play.strategy_name}</span>
        <span className="ml-auto flex items-center gap-1.5">
          {play.suspicious && (
            <Pill tone="warn" title="Return above the plausibility limit: check the rules before trading">
              <TriangleAlert size={11} /> check rules
            </Pill>
          )}
          <SourcePill source={play.source} />
        </span>
      </div>
      <div className="px-5 pt-2.5">
        <h3 className="line-clamp-2 text-[15px] leading-snug font-semibold text-ink">{clean(play.title)}</h3>
        <p className="mono mt-0.5 truncate text-[11px] text-muted">{play.event_ticker}</p>
      </div>
      <div className="mt-4 grid grid-cols-3 gap-px border-t border-line bg-[var(--border)]">
        <Metric label={lock ? "Locked profit" : "Expected profit"} value={signedUsd(play.profit)} strong tone={lock ? "lock" : "model"} />
        <Metric label="Return" value={pct(play.return_pct)} sub={`${num(play.apr_pct)}% APR`} />
        <Metric label="Capital" value={usd(play.capital, false)} sub={`${play.legs.length} leg${play.legs.length === 1 ? "" : "s"}`} />
      </div>
      <div className="flex items-center gap-3 px-5 py-2.5 text-[11px] text-muted">
        <span className="inline-flex items-center gap-1">
          <Clock size={11} /> settles {until(play.settles_at, now)}
        </span>
        {!lock && play.probability != null && <span>p ≥ {prob(play.probability)}</span>}
        <span className="ml-auto">found {ago(play.first_seen || play.found_at, now)}</span>
      </div>
    </button>
  );
}

function Metric({ label, value, sub, strong, tone }: { label: string; value: string; sub?: string; strong?: boolean; tone?: "lock" | "model" }) {
  return (
    <div className="bg-surface px-5 py-3">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={cx("num mt-0.5 font-semibold tracking-tight", strong ? "text-lg" : "text-[15px]", tone === "lock" ? "text-lock" : tone === "model" ? "text-model" : "text-ink")}>
        {value}
      </div>
      {sub && <div className="num text-[11px] text-muted">{sub}</div>}
    </div>
  );
}

type Reverify = { state: "idle" } | { state: "busy" } | { state: "live"; play: Play; at: string } | { state: "gone"; reason: string; at: string } | { state: "error"; message: string };

export function PlayDrawer({ play, onClose, onUpdate }: { play: Play | null; onClose: () => void; onUpdate?: (p: Play) => void }) {
  const [rv, setRv] = useState<Reverify>({ state: "idle" });
  const [shown, setShown] = useState<Play | null>(null);
  if (play !== shown) {
    setShown(play);
    setRv({ state: "idle" });
  }
  if (!play) return null;
  const p = rv.state === "live" ? rv.play : play;
  const lock = p.tier === "LOCK";
  const orders = p.legs.map((l) => l.order).filter(Boolean);
  const canReverify = lock;

  const reverify = async () => {
    setRv({ state: "busy" });
    try {
      const r = await api.reverify(p.key);
      if (r.status === "live") {
        setRv({ state: "live", play: r.play, at: r.at });
        onUpdate?.(r.play);
      } else setRv({ state: "gone", reason: r.reason, at: r.at });
    } catch (e) {
      setRv({ state: "error", message: (e as Error).message });
    }
  };

  return (
    <Drawer
      open
      onClose={onClose}
      title={
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <TierBadge tier={p.tier} />
            <span className="text-xs font-medium text-ink-2">{p.strategy_name}</span>
            <SourcePill source={p.source} />
            <span className="mono text-[11px] text-muted">#{p.id}</span>
          </div>
          <h2 className="mt-2 text-lg leading-snug font-semibold text-ink">{clean(p.title)}</h2>
          <a
            href={kalshiUrl(p.event_ticker)}
            target="_blank"
            rel="noreferrer noopener"
            className="mono mt-1 inline-flex items-center gap-1 text-xs text-accent hover:underline"
          >
            {p.event_ticker} <ArrowUpRight size={12} />
          </a>
        </div>
      }
      footer={
        <div className="flex flex-wrap items-center gap-2">
          {canReverify && (
            <Button variant="primary" icon={<RefreshCw size={14} />} loading={rv.state === "busy"} onClick={reverify}>
              Re-verify now
            </Button>
          )}
          {orders.length > 0 && <CopyButton size="md" label={`Copy ${orders.length} order${orders.length === 1 ? "" : "s"} (JSON)`} text={JSON.stringify(orders, null, 2)} />}
          <a href={kalshiUrl(p.event_ticker)} target="_blank" rel="noreferrer noopener" className="ml-auto">
            <Button variant="ghost" icon={<ArrowUpRight size={14} />}>
              Open on Kalshi
            </Button>
          </a>
        </div>
      }
    >
      {rv.state === "live" && (
        <Banner tone="lock" icon={<CircleCheck size={16} />} title="Still live">
          Re-checked against the full order books {ago(rv.at)}. Numbers below are fresh.
        </Banner>
      )}
      {rv.state === "gone" && (
        <Banner tone="crit" icon={<CircleX size={16} />} title="Gone">
          {rv.reason}
        </Banner>
      )}
      {rv.state === "error" && (
        <Banner tone="crit" icon={<CircleX size={16} />} title="Could not re-verify">
          {rv.message}
        </Banner>
      )}
      {p.suspicious && (
        <Banner tone="warn" icon={<ShieldAlert size={16} />} title="Unusually high return: read the rules first">
          Returns this large are rare for real arbitrage. Confirm every leg's rules describe the same underlying outcome before trading.
        </Banner>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Big label={lock ? "Locked profit" : "Expected profit"} value={signedUsd(p.profit)} tone={lock ? "lock" : "model"} />
        <Big label="Capital" value={usd(p.capital)} />
        <Big label="Fees (all legs)" value={usd(p.fees)} />
        <Big label="Return" value={pct(p.return_pct)} />
        <Big label="Annualized" value={`${num(p.apr_pct)}%`} />
        <Big label="Settles" value={until(p.settles_at)} sub={when(p.settles_at)} />
      </div>

      <dl className="mt-5 divide-y divide-[var(--border)] rounded-xl border border-line px-4">
        <Kv k="Sets" v={<span className="num">{num(p.sets)}</span>} />
        <Kv k="Pays (worst → best case)" v={<span className="num">{usd(p.payout_min)} → {usd(p.payout_max)}</span>} />
        <Kv k="Cost before fees" v={<span className="num">{usd(p.cost)}</span>} />
        {lock ? (
          <Kv k="Confidence" v={<span className="inline-flex items-center gap-1"><Lock size={12} className="text-lock" /> structural (every outcome covered)</span>} />
        ) : (
          <>
            <Kv k="Conservative probability" v={<span className="num">{prob(p.probability)}</span>} />
            <Kv k="Model central probability" v={<span className="num">{prob(p.model_probability)}</span>} />
          </>
        )}
        <Kv k="First seen" v={when(p.first_seen || p.found_at)} />
        {p.alerted_at && <Kv k="Alerted" v={`${when(p.alerted_at)} · ${p.alert_channel ?? ""}`} />}
      </dl>

      {p.rationale.length > 0 && (
        <Section title="Why it works" icon={<Sparkles size={14} className="text-accent" />}>
          <ul className="space-y-1.5">
            {p.rationale.map((r, i) => (
              <li key={i} className="flex gap-2 text-[13px] leading-relaxed text-ink-2">
                <span className="mt-2 size-1 shrink-0 rounded-full bg-muted" />
                {r}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {p.risks.length > 0 && (
        <Section title="What could go wrong" icon={<TriangleAlert size={14} className="text-warn" />}>
          <ul className="space-y-1.5">
            {p.risks.map((r, i) => (
              <li key={i} className="flex gap-2 text-[13px] leading-relaxed text-ink-2">
                <span className="mt-2 size-1 shrink-0 rounded-full bg-warn" />
                {r}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title={`Orders (${p.legs.length} leg${p.legs.length === 1 ? "" : "s"})`}>
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[560px] text-left text-[12px]">
            <thead className="text-[11px] text-muted">
              <tr className="border-b border-line">
                <th className="px-1 py-2 font-medium">Buy</th>
                <th className="px-1 py-2 font-medium">Market</th>
                <th className="px-1 py-2 text-right font-medium">Qty</th>
                <th className="px-1 py-2 text-right font-medium">Limit</th>
                <th className="px-1 py-2 text-right font-medium">Avg fill</th>
                <th className="px-1 py-2 text-right font-medium">Fees</th>
                <th className="px-1 py-2 text-right font-medium">Depth</th>
              </tr>
            </thead>
            <tbody>
              {p.legs.map((l) => (
                <tr key={l.ticker + l.side} className="border-b border-line last:border-0">
                  <td className="px-1 py-2">
                    <Pill tone={l.side === "yes" ? "lock" : "crit"}>{l.side.toUpperCase()}</Pill>
                  </td>
                  <td className="max-w-[240px] px-1 py-2">
                    <div className="truncate text-ink" title={clean(l.sub_title || l.title)}>
                      {clean(l.sub_title || l.title)}
                    </div>
                    <div className="mono truncate text-[10px] text-muted">{l.ticker}</div>
                  </td>
                  <td className="num px-1 py-2 text-right text-ink">{num(l.count)}</td>
                  <td className="num px-1 py-2 text-right text-ink">{cents(l.limit)}</td>
                  <td className="num px-1 py-2 text-right text-ink-2">{cents(l.avg)}</td>
                  <td className="num px-1 py-2 text-right text-ink-2" title={l.fee_terms}>
                    {usd(l.fees)}
                  </td>
                  <td className="num px-1 py-2 text-right text-ink-2">{num(l.depth)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          Limit orders, immediate-or-cancel. Place every leg at or better than its limit or none at all; OMEN's executor does this
          automatically when live trading is enabled on the Pi.
        </p>
      </Section>

      <details className="group mt-6 rounded-xl border border-line">
        <summary className="cursor-pointer px-4 py-2.5 text-xs font-medium text-ink-2 select-none">Evidence (raw)</summary>
        <pre className="mono max-h-80 overflow-auto border-t border-line px-4 py-3 text-[11px] leading-relaxed text-ink-2">
          {JSON.stringify(p.evidence, null, 2)}
        </pre>
      </details>
    </Drawer>
  );
}

function Big({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "lock" | "model" }) {
  return (
    <div className="rounded-xl bg-surface-2 px-3.5 py-3">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={cx("num mt-0.5 text-lg font-semibold tracking-tight", tone === "lock" ? "text-lock" : tone === "model" ? "text-model" : "text-ink")}>
        {value}
      </div>
      {sub && <div className="text-[11px] text-muted">{sub}</div>}
    </div>
  );
}

function Section({ title, icon, children }: { title: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h3 className="mb-2.5 flex items-center gap-1.5 text-[13px] font-semibold text-ink">
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

function Banner({ tone, icon, title, children }: { tone: "lock" | "warn" | "crit"; icon: React.ReactNode; title: string; children: React.ReactNode }) {
  const c = { lock: "bg-lock-soft text-lock", warn: "bg-warn-soft text-warn", crit: "bg-crit-soft text-crit" }[tone];
  return (
    <div className={cx("mb-4 flex gap-2.5 rounded-xl px-4 py-3", c)}>
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div>
        <div className="text-[13px] font-semibold">{title}</div>
        <div className="mt-0.5 text-[13px] leading-relaxed text-ink-2">{children}</div>
      </div>
    </div>
  );
}
