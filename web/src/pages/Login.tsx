import { KeyRound, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { ApiError, api } from "../api";
import { Logo } from "../components/Logo";
import { Button, inputClass } from "../components/ui";

export default function Login({ setup, onDone }: { setup: boolean; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-[380px]">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo size={44} />
          <h1 className="mt-4 text-xl font-semibold tracking-tight text-ink">OMEN</h1>
          <p className="mt-1 text-[13px] text-muted">High-confidence Kalshi plays, after every fee.</p>
        </div>
        {setup ? (
          <div className="card p-6">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-ink">
              <ShieldCheck size={16} className="text-accent" /> One step left
            </div>
            <p className="mt-2 text-[13px] leading-relaxed text-ink-2">
              This site is private. In your Vercel project, open <b>Settings → Environment Variables</b>, add{" "}
              <code className="mono rounded bg-surface-2 px-1">OMEN_WEB_PASSWORD</code> (8+ characters), then redeploy.
            </p>
            <p className="mt-2 text-[13px] leading-relaxed text-muted">The full checklist is in docs/WEB.md in the repository.</p>
          </div>
        ) : (
          <form onSubmit={submit} className="card p-6">
            <label className="block">
              <span className="text-xs font-medium text-ink-2">Password</span>
              <div className="relative mt-1.5">
                <KeyRound size={15} className="absolute top-1/2 left-3 -translate-y-1/2 text-muted" />
                <input
                  type="password"
                  autoComplete="current-password"
                  autoFocus
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className={`${inputClass} pl-9`}
                  aria-invalid={!!error}
                />
              </div>
            </label>
            {error && <p className="mt-2 text-xs text-crit">{error}</p>}
            <Button type="submit" variant="primary" className="mt-4 w-full" loading={busy} disabled={!password}>
              Sign in
            </Button>
          </form>
        )}
        <p className="mt-6 text-center text-[11px] leading-relaxed text-muted">
          Not financial advice. Plays are suggestions; you place every order yourself unless live trading is enabled on your Pi.
        </p>
      </div>
    </div>
  );
}
