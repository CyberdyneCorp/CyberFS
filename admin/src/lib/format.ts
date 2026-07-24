// Presentation helpers. Pure, so the awkward cases are tested directly rather
// than inferred from a rendered page.

const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

/** Human-readable bytes. Exact for small values; one decimal above KB. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";

  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const decimals = unit === 0 ? 0 : 1;
  return `${value.toFixed(decimals)} ${UNITS[unit]}`;
}

export function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "0%";
  // Below 0.1% but non-zero reads as "<0.1%" rather than a misleading "0%".
  if (value > 0 && value < 0.1) return "<0.1%";
  return `${value.toFixed(value < 10 ? 1 : 0)}%`;
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "never";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "unknown";
  return parsed.toLocaleString();
}

export function formatRelative(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "never";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "unknown";

  const seconds = Math.round((now.getTime() - parsed.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

/** Quota pressure, for colouring a bar without hard-coding thresholds in markup. */
export function quotaSeverity(percentUsed: number, overQuota: boolean): "ok" | "warn" | "over" {
  if (overQuota) return "over";
  return percentUsed >= 80 ? "warn" : "ok";
}

/** Turns `grant.created` into `Grant created`, for a readable audit table. */
export function humanizeAction(action: string): string {
  const text = action.replace(/[._]/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}
