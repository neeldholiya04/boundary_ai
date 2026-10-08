/** Shared display formatting: times people read, short ids, plain labels for API enums. */

// The API serialises naive UTC datetimes without a zone; read them as UTC, not local time.
export function parseTime(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`);
}

export function timeAgo(value: string, now = Date.now()): string {
  const seconds = Math.round((now - parseTime(value).getTime()) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return parseTime(value).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function timeLeft(value: string, now = Date.now()): string {
  const seconds = Math.round((parseTime(value).getTime() - now) / 1000);
  if (seconds <= 0) return "expired";
  if (seconds < 90) return `${seconds} s left`;
  return `${Math.round(seconds / 60)} min left`;
}

export function clock(value: string): string {
  return parseTime(value).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "";
}

export const STAGE_NAMES: Record<string, string> = {
  user_input: "User request",
  tool_args: "Tool call",
  tool_output: "Tool result",
  final_output: "Answer"
};

/** A compact one-line view of tool arguments for lists; long values are cut. */
export function argsLine(args: Record<string, unknown> | null | undefined, max = 120): string {
  if (!args) return "";
  const text = Object.entries(args)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join("  ");
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
