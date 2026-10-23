// Display formatting. Money is shown to the cent; the API keeps exact values for orders.

const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export function usd(v: number | null | undefined, cents = true): string {
  if (v == null || Number.isNaN(v)) return "—";
  return (cents ? usd2 : usd0).format(v);
}

export function signedUsd(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return (v > 0 ? "+" : v < 0 ? "−" : "") + usd2.format(Math.abs(v));
}

export function pct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v.toFixed(digits)}%`;
}

/** 0–1 probability → "62.4%". */
export function prob(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

/** Dollar price → cents, e.g. 0.37 → "37¢", 0.375 → "37.5¢". */
export function cents(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const c = Math.round(v * 1000) / 10;
  return `${Number.isInteger(c) ? c.toFixed(0) : c.toFixed(1)}¢`;
}

export function num(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return int.format(v);
}

export function short(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return compact.format(v);
}

export function price(v: number): string {
  if (v >= 1000) return int.format(v);
  if (v >= 1) return v.toFixed(2);
  return v.toPrecision(3);
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const s = Math.round((now - new Date(iso).getTime()) / 1000);
  if (s < 0) return until(iso, now);
  if (s < 45) return "just now";
  if (s < 90) return "1 min ago";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function until(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const s = Math.round((new Date(iso).getTime() - now) / 1000);
  if (s <= 0) return "now";
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400 * 2) return `in ${Math.round(s / 3600)} h`;
  if (s < 86400 * 60) return `in ${Math.round(s / 86400)} d`;
  return `in ${Math.round(s / (86400 * 30))} mo`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

const dt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const d = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return dt.format(new Date(iso));
}

export function day(iso: string | null | undefined): string {
  if (!iso) return "—";
  return d.format(new Date(iso));
}

export function kalshiUrl(eventTicker: string): string {
  const series = eventTicker.split("-")[0].toLowerCase();
  return `https://kalshi.com/markets/${series}/${eventTicker.toLowerCase()}`;
}

/** Kalshi titles carry markdown emphasis ("**gas prices**"); show them as plain text. */
export function clean(text: string | null | undefined): string {
  return (text ?? "").replace(/\*\*|__/g, "").replace(/\s+/g, " ").trim();
}

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
