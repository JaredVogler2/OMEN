// Typed client for the OMEN API (same origin; session cookie + CSRF header on writes).

export type Tier = "LOCK" | "MODEL";

export interface Leg {
  ticker: string;
  event_ticker: string;
  side: "yes" | "no";
  title: string;
  sub_title: string;
  count: number;
  limit: number;
  avg: number;
  notional: number;
  fees: number;
  fee_terms: string;
  depth: number;
  close_time: string | null;
  order: Record<string, unknown> | null;
}

export interface Play {
  key: string;
  id: string;
  source: "pi" | "scan" | "reverify";
  strategy: string;
  strategy_name: string;
  tier: Tier;
  title: string;
  event_ticker: string;
  profit: number;
  capital: number;
  cost: number;
  fees: number;
  return_pct: number;
  apr_pct: number;
  payout_min: number;
  payout_max: number;
  sets: number;
  settles_at: string | null;
  found_at: string | null;
  confidence: number;
  probability: number | null;
  model_probability: number | null;
  rationale: string[];
  risks: string[];
  evidence: Record<string, unknown>;
  legs: Leg[];
  suspicious: boolean;
  first_seen?: string;
  last_seen?: string;
  alerted_at?: string | null;
  alert_channel?: string | null;
}

export interface Notice {
  severity: "info" | "warning" | "critical";
  title: string;
  body: string;
}

export interface NearMiss {
  strategy: string;
  event_ticker: string;
  title: string;
  edge: number;
  legs: number;
  detail?: string;
}

export interface ScanParams {
  horizon_hours: number | null;
  strategies: string[];
  min_profit_usd: number;
  min_return_pct: number;
  min_apr_pct: number;
  max_capital_usd: number;
  near_misses: boolean;
}

export interface ScanResult {
  id: string;
  at: string;
  params: ScanParams;
  trading_active: boolean;
  plays: Play[];
  failed: { strategy: string; title: string; event_ticker: string; top_edge: number; reason: string }[];
  near_misses: NearMiss[];
  rejected: { event_ticker: string; reason: string }[];
  notices: Notice[];
  stats: { markets: number; candidates: number; verified: number; requests: number; cpu_seconds?: number; seconds?: number };
}

export interface Activity {
  ts: string;
  kind: "alert" | "gone" | "fee" | "error";
  id?: string;
  tier?: Tier;
  title: string;
  channel?: string;
  profit?: number;
  ticker?: string;
  fee_kind?: string;
  detail?: string;
}

export interface PaperRow {
  id: string;
  strategy: string;
  tier: Tier;
  opened_at: string;
  settles_at: string | null;
  settled_at: string | null;
  status: "open" | "settled";
  sets: number;
  capital: number;
  fees: number;
  expected_profit: number;
  payout: number | null;
  pnl: number | null;
}

export interface Run {
  kind: string;
  started_at: string;
  finished_at: string | null;
  markets: number | null;
  candidates: number | null;
  opportunities: number | null;
  alerts: number | null;
  requests: number | null;
  error: string | null;
}

export interface Feed {
  version: 1;
  omen_version: string;
  generated_at: string;
  host: string;
  plays: Play[];
  activity: Activity[];
  paper: {
    open: PaperRow[];
    settled: PaperRow[];
    totals: { status: string; n: number; cap: number | null; pnl: number | null; exp: number | null }[];
  };
  models: { name: string; state: string; summary: string; passed?: boolean; gates?: unknown }[];
  runs: Run[];
  fees: {
    schedule_version: string;
    verified_on: string;
    upcoming: { scope: string; ticker: string; fee_type: string | null; multiplier: number | null; scheduled: string; describe: string }[];
    series_count: number;
  };
  health: {
    outbox: Record<string, number>;
    execution_mode: string;
    markets_in_store: number;
    cadence: { hot_seconds: number; hot_horizon_hours: number; full_seconds: number; model_seconds: number };
    thresholds: {
      min_profit_usd: number;
      min_return_pct: number;
      min_apr_pct: number;
      max_capital_usd: number;
      max_plausible_return_pct: number;
    };
  };
}

export interface Budget {
  scans_today: number;
  daily_scan_cap: number;
  cpu_seconds_month: number;
  monthly_cpu_budget_seconds: number;
  min_scan_gap_seconds: number;
  store: string;
}

export interface Dashboard {
  server_time: string;
  version: string;
  plays: Play[];
  feed: Feed | null;
  feed_meta: { received_at: string; bytes: number; host: string; omen_version: string } | null;
  feed_age_seconds: number | null;
  last_scan: ScanResult | null;
  budget: Budget;
  store_ok: boolean;
}

export interface HistoryPoint {
  t: string;
  source: "pi" | "scan";
  plays: number;
  best_profit: number;
  markets?: number;
  paper_pnl?: number;
}

export interface FeeInfo {
  schedule: {
    version: string;
    verified_on: string;
    source_url: string;
    rounding_increment: number;
    formulas: Record<string, { taker_rate: number; maker_rate: number; flat: boolean }>;
  };
  terms: { fee_type: string; multiplier: number; series: number }[];
  series_count: number;
  nonstandard: { ticker: string; title: string; category: string; fee_type: string; multiplier: number }[];
  upcoming: { scope: string; ticker: string; series_ticker: string | null; fee_type: string | null; multiplier: number | null; scheduled: string; describe: string }[];
  unknown_fee_types: string[];
  notices: Notice[];
  as_of: string;
}

export interface FeeQuote {
  series: string;
  event: string | null;
  price: number;
  count: number;
  fee_type: string;
  multiplier: number;
  source: string;
  taker_rate: number;
  maker_rate: number;
  taker_fee: number;
  maker_fee: number;
  taker_fee_unrounded: number;
  rounding_increment: number;
  cost: number;
  describe: string;
}

export interface SeriesHit {
  ticker: string;
  title: string;
  category: string;
  fee_type: string;
  multiplier: number;
}

export interface LadderRow {
  ticker: string;
  label: string;
  interval: string;
  lo: number | null;
  hi: number | null;
  x: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  p_central: number;
  p_low: number;
  p_high: number;
  signal: { side: "yes" | "no"; edge: number; p_conservative: number; would_alert: boolean } | null;
}

export interface CryptoView {
  asset: string;
  spot: number;
  sources: Record<string, number>;
  dispersion_pct: number;
  vol: { short: number | null; long: number | null; implied: number | null; central: number };
  settings: {
    min_edge: number;
    min_conservative_probability: number;
    vol_safety_multiplier: number;
    spot_basis_band_pct: number;
    student_t_dof: number;
  };
  series: {
    series: string;
    event_ticker: string;
    close_time: string;
    hours_to_close: number;
    kind: "range" | "threshold";
    title: string;
    markets: LadderRow[];
  }[];
  as_of: string;
}

export interface Health {
  ok: boolean;
  version: string;
  setup_required: boolean;
  store: string;
  ingest_enabled: boolean;
}

export type ScanEvent =
  | { type: "start"; params: ScanParams; at: string }
  | { type: "stage"; stage: string; label?: string; series?: number; markets?: number; candidates?: number }
  | { type: "progress"; stage: string; count: number }
  | { type: "result"; result: ScanResult }
  | { type: "error"; message: string };

export class ApiError extends Error {
  status: number;
  data: Record<string, unknown>;
  constructor(status: number, message: string, data: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

type Listener = () => void;
const unauthorized = new Set<Listener>();
export function onUnauthorized(fn: Listener): () => void {
  unauthorized.add(fn);
  return () => unauthorized.delete(fn);
}

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<Response> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      accept: "application/json",
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      ...(method === "GET" ? {} : { "x-omen-csrf": "1" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let data: Record<string, unknown> = {};
    try {
      data = await res.json();
    } catch {
      /* not JSON */
    }
    if (res.status === 401 && !path.startsWith("/api/auth/")) unauthorized.forEach((fn) => fn());
    const message = typeof data.error === "string" ? data.error : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, message, data);
  }
  return res as Response & { _t?: T };
}

async function json<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const res = await request<T>(method, path, body, signal);
  return (await res.json()) as T;
}

export const api = {
  health: () => json<Health>("GET", "/api/health"),
  session: () => json<{ authenticated: boolean; setup_required: boolean }>("GET", "/api/auth/session"),
  login: (password: string) => json<{ authenticated: boolean }>("POST", "/api/auth/login", { password }),
  logout: () => json<{ authenticated: boolean }>("POST", "/api/auth/logout", {}),
  dashboard: (signal?: AbortSignal) => json<Dashboard>("GET", "/api/dashboard", undefined, signal),
  history: (signal?: AbortSignal) => json<{ points: HistoryPoint[] }>("GET", "/api/history", undefined, signal),
  reverify: (key: string) =>
    json<{ status: "live"; play: Play; at: string } | { status: "gone"; reason: string; at: string }>(
      "POST",
      "/api/reverify",
      { key },
    ),
  fees: (signal?: AbortSignal) => json<FeeInfo>("GET", "/api/fees", undefined, signal),
  quote: (q: { series: string; event?: string; price: number; count: number }) =>
    json<FeeQuote>("POST", "/api/fees/quote", q),
  series: (q: string, signal?: AbortSignal) =>
    json<{ series: SeriesHit[] }>("GET", `/api/series?q=${encodeURIComponent(q)}`, undefined, signal),
  crypto: (asset: string, signal?: AbortSignal) =>
    json<CryptoView>("GET", `/api/crypto?asset=${encodeURIComponent(asset)}`, undefined, signal),
  atlas: (signal?: AbortSignal) => json<Atlas>("GET", "/api/atlas", undefined, signal),

  /** Streams NDJSON scan events; resolves when the stream ends. */
  async scan(params: Omit<Partial<ScanParams>, "horizon_hours"> & { horizon_hours?: number | "all" | null }, onEvent: (e: ScanEvent) => void, signal?: AbortSignal) {
    const res = await request("POST", "/api/scan", params, signal);
    const reader = res.body?.getReader();
    if (!reader) throw new ApiError(500, "streaming not supported by this browser");
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (value) buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) onEvent(JSON.parse(line) as ScanEvent);
      }
      if (done) break;
    }
    if (buf.trim()) onEvent(JSON.parse(buf) as ScanEvent);
  },
};

export interface CellStats {
  n: number;
  series: number;
  win_rate: number;
  avg_price: number;
  ev: number;
  ev_lo: number;
  ev_hi: number;
  excess: number;
}

export interface AtlasCell {
  category: string;
  horizon: "1h" | "6h" | "24h" | "72h";
  band: [number, number];
  discovery: CellStats;
  validation: CellStats | null;
  edge: boolean;
}

export interface Atlas {
  version: 1;
  generated_at?: string;
  markets: number;
  series?: number;
  observations?: number;
  validation_from?: string | null;
  cells: AtlasCell[];
  overall: AtlasCell[];
  edges: AtlasCell[];
  source: "pi" | "repository" | "none";
}

