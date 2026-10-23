import { Check, CircleAlert, Copy, Info, LoaderCircle, TriangleAlert, X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import type { Notice, Tier } from "../api";
import { cx } from "../lib/format";

export function Card({
  title,
  subtitle,
  action,
  children,
  className,
  pad = true,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  pad?: boolean;
}) {
  return (
    <section className={cx("card min-w-0", className)}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-3 px-5 pt-4">
          <div className="min-w-0">
            {title && <h2 className="text-[13px] font-semibold tracking-tight text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
        </header>
      )}
      <div className={cx(pad && "p-5", pad && (title || action) ? "pt-3" : null)}>{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone,
  icon,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "lock" | "model" | "warn" | "crit";
  icon?: ReactNode;
}) {
  const toneText = { lock: "text-lock", model: "text-model", warn: "text-warn", crit: "text-crit" };
  return (
    <div className="card px-4 py-3.5">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted">
        {icon && <span className={cx("shrink-0", tone ? toneText[tone] : "text-muted")}>{icon}</span>}
        {label}
      </div>
      <div className="mt-1.5 text-2xl font-semibold tracking-tight text-ink">{value}</div>
      {sub && <div className="mt-0.5 truncate text-xs text-muted">{sub}</div>}
    </div>
  );
}

const pillTone = {
  lock: "bg-lock-soft text-lock",
  model: "bg-model-soft text-model",
  warn: "bg-warn-soft text-warn",
  crit: "bg-crit-soft text-crit",
  accent: "bg-accent-soft text-accent",
  neutral: "bg-surface-3 text-ink-2",
} as const;

export function Pill({
  tone = "neutral",
  children,
  className,
  title,
}: {
  tone?: keyof typeof pillTone;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold whitespace-nowrap",
        pillTone[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function TierBadge({ tier }: { tier: Tier }) {
  return tier === "LOCK" ? (
    <Pill tone="lock" title="Every outcome pays at least the cost after fees">
      LOCK
    </Pill>
  ) : (
    <Pill tone="model" title="Statistical edge from a calibrated model; can lose">
      MODEL
    </Pill>
  );
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
  icon?: ReactNode;
};

export function Button({ variant = "secondary", size = "md", loading, icon, children, className, disabled, ...rest }: ButtonProps) {
  const v = {
    primary: "bg-accent text-white hover:brightness-110 shadow-sm",
    secondary: "bg-surface-2 text-ink border border-line hover:bg-surface-3",
    ghost: "text-ink-2 hover:bg-surface-2 hover:text-ink",
    danger: "bg-crit-soft text-crit hover:brightness-110",
  }[variant];
  const s = size === "sm" ? "h-7 px-2.5 text-xs gap-1.5" : "h-9 px-3.5 text-[13px] gap-2";
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cx(
        "focus-ring inline-flex shrink-0 items-center justify-center rounded-lg font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        v,
        s,
        className,
      )}
    >
      {loading ? <LoaderCircle size={size === "sm" ? 13 : 15} className="animate-spin" /> : icon}
      {children}
    </button>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: ReactNode }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-lg border border-line bg-surface-2 p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={o.value === value}
          onClick={() => onChange(o.value)}
          className={cx(
            "focus-ring rounded-md px-2.5 py-1 text-xs font-medium transition",
            o.value === value ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Drawer({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
      prev?.focus?.();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-[2px]" onClick={onClose} />
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        className="relative flex h-full w-full max-w-[720px] flex-col border-l border-line bg-bg shadow-2xl outline-none"
      >
        <header className="flex items-start justify-between gap-4 border-b border-line px-6 py-4">
          <div className="min-w-0 flex-1">{title}</div>
          <button onClick={onClose} aria-label="Close" className="focus-ring rounded-md p-1 text-muted hover:bg-surface-2 hover:text-ink">
            <X size={18} />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>
        {footer && <footer className="border-t border-line px-6 py-3">{footer}</footer>}
      </div>
    </div>
  );
}

export function CopyButton({ text, label = "Copy", size = "sm" }: { text: string; label?: string; size?: "sm" | "md" }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      size={size}
      variant="secondary"
      icon={done ? <Check size={13} /> : <Copy size={13} />}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          window.setTimeout(() => setDone(false), 1400);
        } catch {
          /* clipboard blocked */
        }
      }}
    >
      {done ? "Copied" : label}
    </Button>
  );
}

/** Progress toward a limit; turns amber at 75% and red at 95% (labelled, never colour alone). */
export function Meter({ label, value, max, format }: { label: string; value: number; max: number; format: (v: number) => string }) {
  const ratio = max > 0 ? Math.min(1, value / max) : 0;
  const tone = ratio >= 0.95 ? "bg-crit" : ratio >= 0.75 ? "bg-warn" : "bg-accent";
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="text-muted">{label}</span>
        <span className="num text-ink-2">
          {format(value)} <span className="text-muted">/ {format(max)}</span>
        </span>
      </div>
      <div
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-3"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
      >
        <div className={cx("h-full rounded-full transition-all", tone)} style={{ width: `${Math.max(ratio * 100, ratio > 0 ? 2 : 0)}%` }} />
      </div>
    </div>
  );
}

export function EmptyState({ icon, title, children, action }: { icon: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center px-6 py-12 text-center">
      <div className="mb-3 grid size-11 place-items-center rounded-xl bg-surface-2 text-muted">{icon}</div>
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      {children && <div className="mt-1 max-w-md text-[13px] leading-relaxed text-muted">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("sheen rounded-md bg-surface-2", className)} />;
}

export function NoticeBanner({ notice, onDismiss }: { notice: Notice; onDismiss?: () => void }) {
  const tone =
    notice.severity === "critical"
      ? { box: "bg-crit-soft border-crit/30", icon: <CircleAlert size={16} className="text-crit" /> }
      : notice.severity === "warning"
        ? { box: "bg-warn-soft border-warn/30", icon: <TriangleAlert size={16} className="text-warn" /> }
        : { box: "bg-accent-soft border-accent/25", icon: <Info size={16} className="text-accent" /> };
  return (
    <div className={cx("flex items-start gap-3 rounded-xl border px-4 py-3", tone.box)}>
      <span className="mt-0.5 shrink-0">{tone.icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-semibold text-ink">{notice.title}</div>
        {notice.body && <div className="mt-0.5 text-[13px] leading-relaxed whitespace-pre-line text-ink-2">{notice.body}</div>}
      </div>
      {onDismiss && (
        <button onClick={onDismiss} aria-label="Dismiss" className="focus-ring rounded p-0.5 text-muted hover:text-ink">
          <X size={14} />
        </button>
      )}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-ink-2">{label}</span>
      <div className="mt-1.5">{children}</div>
      {hint && <span className="mt-1 block text-[11px] text-muted">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "focus-ring h-9 w-full rounded-lg border border-line bg-surface-2 px-3 text-[13px] text-ink placeholder:text-muted outline-none focus:border-line-strong";

export function Kv({ k, v, mono }: { k: ReactNode; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5 text-[13px]">
      <dt className="shrink-0 text-muted">{k}</dt>
      <dd className={cx("min-w-0 truncate text-right text-ink", mono && "mono")}>{v}</dd>
    </div>
  );
}

export function StatusDot({ tone, pulse }: { tone: "good" | "warn" | "crit" | "idle"; pulse?: boolean }) {
  const c = { good: "bg-lock", warn: "bg-warn", crit: "bg-crit", idle: "bg-muted" }[tone];
  return <span className={cx("inline-block size-2 shrink-0 rounded-full", c, pulse && "pulse")} />;
}
