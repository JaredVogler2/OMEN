export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <defs>
        <linearGradient id="omen-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#9085e9" />
          <stop offset="1" stopColor="#6250d6" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#omen-g)" />
      <circle cx="16" cy="16" r="8.5" fill="none" stroke="#fff" strokeWidth="2.4" />
      <circle cx="16" cy="16" r="3" fill="#fff" />
      <path d="M16 3.5v4M16 24.5v4" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" opacity=".55" />
    </svg>
  );
}
