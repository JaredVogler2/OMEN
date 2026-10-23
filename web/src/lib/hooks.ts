import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

/** Fetches on mount and every `intervalMs` while the tab is visible; pauses in background tabs. */
export function usePoll<T>(load: (signal: AbortSignal) => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const ctrl = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setLoading(true);
    try {
      const v = await loadRef.current(c.signal);
      if (!c.signal.aborted) {
        setData(v);
        setError(null);
        setUpdatedAt(Date.now());
      }
    } catch (e) {
      if (!c.signal.aborted) setError(e as Error);
    } finally {
      if (!c.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    if (!intervalMs) return () => ctrl.current?.abort();
    const tick = () => document.visibilityState === "visible" && refresh();
    const id = window.setInterval(tick, intervalMs);
    const onVis = () => document.visibilityState === "visible" && refresh();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      ctrl.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, loading, updatedAt, refresh, setData };
}

/** Re-renders every `ms` so relative times ("2 min ago") stay current. */
export function useNow(ms = 15000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}

function subscribeHash(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

/** "#/fees?series=KXBTC" → { path: "fees", query: URLSearchParams } */
export function useRoute(): { path: string; query: URLSearchParams } {
  const hash = useSyncExternalStore(subscribeHash, () => window.location.hash);
  const raw = hash.replace(/^#\/?/, "");
  const [path, q = ""] = raw.split("?");
  return { path: path || "plays", query: new URLSearchParams(q) };
}

export function navigate(path: string) {
  window.location.hash = `/${path}`;
}

export function useTheme(): [boolean, () => void] {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const toggle = useCallback(() => {
    setDark((d) => {
      const next = !d;
      document.documentElement.classList.toggle("dark", next);
      try {
        localStorage.setItem("omen-theme", next ? "dark" : "light");
      } catch {
        /* storage blocked */
      }
      const meta = document.querySelector('meta[name="theme-color"]');
      meta?.setAttribute("content", next ? "#0b0c0f" : "#f3f4f6");
      return next;
    });
  }, []);
  return [dark, toggle];
}

/** Reads a persisted value (falls back silently if storage is blocked). */
export function useStored<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (next: T) => {
      setV(next);
      try {
        localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* storage blocked */
      }
    },
    [key],
  );
  return [v, set];
}
