import { Bitcoin, ChartLine, LogOut, Moon, Radar, Receipt, ScanSearch, Server, Sun } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { type Dashboard, api, onUnauthorized } from "./api";
import { Logo } from "./components/Logo";
import { StatusDot } from "./components/ui";
import { ago, cx, duration } from "./lib/format";
import { useNow, usePoll, useRoute, useTheme } from "./lib/hooks";
import Crypto from "./pages/Crypto";
import Fees from "./pages/Fees";
import Login from "./pages/Login";
import Performance from "./pages/Performance";
import Plays from "./pages/Plays";
import Scan from "./pages/Scan";
import System from "./pages/System";

const NAV = [
  { path: "plays", label: "Plays", icon: Radar },
  { path: "scan", label: "Scan", icon: ScanSearch },
  { path: "fees", label: "Fees", icon: Receipt },
  { path: "crypto", label: "Crypto model", icon: Bitcoin },
  { path: "performance", label: "Performance", icon: ChartLine },
  { path: "system", label: "System", icon: Server },
] as const;

type Auth = { state: "loading" } | { state: "out"; setup: boolean } | { state: "in" };

export default function App() {
  const [auth, setAuth] = useState<Auth>({ state: "loading" });

  useEffect(() => {
    api
      .session()
      .then((s) => setAuth(s.authenticated ? { state: "in" } : { state: "out", setup: s.setup_required }))
      .catch(() => setAuth({ state: "out", setup: false }));
    return onUnauthorized(() => setAuth({ state: "out", setup: false }));
  }, []);

  if (auth.state === "loading")
    return (
      <div className="grid min-h-screen place-items-center">
        <div className="pulse">
          <Logo size={36} />
        </div>
      </div>
    );
  if (auth.state === "out") return <Login setup={auth.setup} onDone={() => setAuth({ state: "in" })} />;
  return <Shell onLogout={() => setAuth({ state: "out", setup: false })} />;
}

/** How fresh the Pi's live feed is: the scanner pushes at least every few minutes while it runs. */
export function feedStatus(d: Dashboard | null): { tone: "good" | "warn" | "crit" | "idle"; label: string; detail: string } {
  if (!d || !d.feed_meta) return { tone: "idle", label: "Pi not connected", detail: "No live feed received yet" };
  const age = d.feed_age_seconds ?? Infinity;
  if (age < 600) return { tone: "good", label: "Pi live", detail: `Updated ${duration(age)} ago from ${d.feed_meta.host}` };
  if (age < 3600) return { tone: "warn", label: "Pi delayed", detail: `Last update ${duration(age)} ago` };
  return { tone: "crit", label: "Pi offline", detail: `Last update ${ago(d.feed_meta.received_at)}` };
}

function Shell({ onLogout }: { onLogout: () => void }) {
  const { path } = useRoute();
  const [dark, toggleTheme] = useTheme();
  const now = useNow(15000);
  const dash = usePoll((s) => api.dashboard(s), 30000);
  const status = feedStatus(dash.data);
  const refreshDash = dash.refresh;
  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      onLogout();
    }
  }, [onLogout]);

  let page: ReactNode;
  switch (path) {
    case "scan":
      page = <Scan dash={dash.data} onResult={refreshDash} />;
      break;
    case "fees":
      page = <Fees />;
      break;
    case "crypto":
      page = <Crypto />;
      break;
    case "performance":
      page = <Performance dash={dash.data} />;
      break;
    case "system":
      page = <System dash={dash.data} error={dash.error} />;
      break;
    default:
      page = <Plays dash={dash.data} loading={dash.loading && !dash.data} error={dash.error} now={now} onRefresh={refreshDash} updatedAt={dash.updatedAt} />;
  }

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[232px_1fr]">
      <aside className="sticky top-0 z-30 border-b border-line bg-bg/85 backdrop-blur lg:h-screen lg:border-r lg:border-b-0">
        <div className="flex items-center gap-2.5 px-4 py-3 lg:px-5 lg:py-5">
          <Logo />
          <div className="leading-tight">
            <div className="text-[15px] font-semibold tracking-tight text-ink">OMEN</div>
            <div className="text-[11px] text-muted">Kalshi play scanner</div>
          </div>
          <div className="ml-auto flex items-center gap-1 lg:hidden">
            <IconButton label={dark ? "Light theme" : "Dark theme"} onClick={toggleTheme}>
              {dark ? <Sun size={16} /> : <Moon size={16} />}
            </IconButton>
            <IconButton label="Sign out" onClick={logout}>
              <LogOut size={16} />
            </IconButton>
          </div>
        </div>
        <nav className="flex gap-1 overflow-x-auto px-3 pb-2 lg:flex-col lg:px-3 lg:pb-0" aria-label="Main">
          {NAV.map(({ path: p, label, icon: Icon }) => {
            const active = p === path || (p === "plays" && !NAV.some((n) => n.path === path));
            return (
              <a
                key={p}
                href={`#/${p}`}
                aria-current={active ? "page" : undefined}
                className={cx(
                  "focus-ring flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium transition",
                  active ? "bg-surface-2 text-ink shadow-[inset_0_0_0_1px_var(--border)]" : "text-muted hover:bg-surface-2/60 hover:text-ink",
                )}
              >
                <Icon size={16} className={active ? "text-accent" : undefined} />
                {label}
                {p === "plays" && dash.data && dash.data.plays.length > 0 && (
                  <span className="num ml-auto rounded-full bg-accent px-1.5 text-[10px] font-semibold text-white">{dash.data.plays.length}</span>
                )}
              </a>
            );
          })}
        </nav>
        <div className="absolute inset-x-0 bottom-0 hidden space-y-3 border-t border-line p-4 lg:block">
          <a href="#/system" className="focus-ring block rounded-lg p-1 -m-1">
            <div className="flex items-center gap-2 text-xs font-medium text-ink">
              <StatusDot tone={status.tone} pulse={status.tone === "good"} />
              {status.label}
            </div>
            <div className="mt-0.5 truncate pl-4 text-[11px] text-muted" title={status.detail}>
              {status.detail}
            </div>
          </a>
          <div className="flex items-center gap-1">
            <IconButton label={dark ? "Light theme" : "Dark theme"} onClick={toggleTheme}>
              {dark ? <Sun size={15} /> : <Moon size={15} />}
            </IconButton>
            <IconButton label="Sign out" onClick={logout}>
              <LogOut size={15} />
            </IconButton>
            <span className="ml-auto text-[11px] text-muted">v{dash.data?.version ?? "…"}</span>
          </div>
        </div>
      </aside>
      <main className="mx-auto w-full max-w-[1320px] min-w-0 px-4 py-5 sm:px-6 lg:px-8 lg:py-8" key={path}>
        {page}
      </main>
    </div>
  );
}

function IconButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button onClick={onClick} aria-label={label} title={label} className="focus-ring rounded-lg p-2 text-muted transition hover:bg-surface-2 hover:text-ink">
      {children}
    </button>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-[22px] font-semibold tracking-tight text-ink">{title}</h1>
        {subtitle && <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
