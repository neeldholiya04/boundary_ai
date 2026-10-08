"use client";

import { MagnifyingGlass } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import { apiGet } from "@/lib/api";
import { STAGE_NAMES, argsLine, clock, shortId, timeAgo } from "@/lib/format";
import { AuditEvent } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

type Category = "all" | "agent" | "guard" | "tools" | "approvals" | "rules";

const CATEGORIES: { key: Category; label: string }[] = [
  { key: "all", label: "All" },
  { key: "agent", label: "Agent" },
  { key: "guard", label: "Guard" },
  { key: "tools", label: "Tools" },
  { key: "approvals", label: "Approvals" },
  { key: "rules", label: "Rules" }
];

function categoryOf(type: string): Exclude<Category, "all"> {
  if (type.startsWith("guard.rule") || type.startsWith("policy.created") || type.startsWith("policy.updated") || type.startsWith("policy.deleted") || type === "policy.defaults_seeded" || type === "guard.mode_changed")
    return "rules";
  if (type.startsWith("guard.")) return "guard";
  if (type.startsWith("mcp.") || type.startsWith("policy.")) return "tools";
  if (type.startsWith("approval.")) return "approvals";
  return "agent";
}

const LABELS: Record<string, string> = {
  "chat.user_message": "User message",
  "agent.response": "Agent answered",
  "agent.planner_error": "Planner error",
  "agent.max_steps_reached": "Stopped at step limit",
  "guard.decision": "Guard checked",
  "guard.async_decision": "Guard checked (async)",
  "guard.run_tainted": "Run tainted",
  "guard.mode_changed": "Mode changed",
  "guard.rule_created": "Text rule created",
  "guard.rule_updated": "Text rule updated",
  "guard.rule_deleted": "Text rule deleted",
  "guard.rule_failed": "Rule failed to load",
  "mcp.tools_discovered": "Tools discovered",
  "mcp.tool_succeeded": "Tool ran",
  "mcp.tool_failed": "Tool failed",
  "policy.decision": "Tool rule decided",
  "policy.created": "Tool rule created",
  "policy.updated": "Tool rule updated",
  "policy.deleted": "Tool rule deleted",
  "policy.defaults_seeded": "Defaults checked",
  "approval.requested": "Approval requested",
  "approval.approved": "Approved",
  "approval.denied": "Denied",
  "approval.expired": "Approval expired",
  "approval.invalidated": "Approval invalidated"
};

type Payload = Record<string, unknown>;

/** One line a person can read, taken from the event payload. */
function summarize(event: AuditEvent): { text: string; tone?: "warn" | "danger" | "ok" } {
  const p = (event.payload ?? {}) as Payload;
  const s = (v: unknown) => (typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v));
  switch (event.event_type) {
    case "chat.user_message":
    case "agent.response":
      return { text: s(p.message) };
    case "guard.decision":
    case "guard.async_decision": {
      const decisions = (Array.isArray(p.decisions) ? p.decisions : []) as Payload[];
      const fired = decisions.filter((d) => d.would_action !== "allow");
      const where = `${STAGE_NAMES[s(p.stage)] ?? s(p.stage)}${p.tool_name ? ` (${s(p.tool_name)})` : ""}`;
      if (fired.length === 0) return { text: `${where}: passed ${decisions.length} policies` };
      const names = fired.map((d) => `${s(d.policy_id)} ${d.mode === "shadow" ? "would " : ""}${s(d.would_action)}`);
      return { text: `${where}: ${names.join(", ")}`, tone: p.action === "allow" ? "warn" : "danger" };
    }
    case "guard.run_tainted":
      return { text: s(p.reason), tone: "warn" };
    case "policy.decision": {
      const shadow = (Array.isArray(p.shadow) ? p.shadow : []) as Payload[];
      const would = shadow.map((x) => `would ${s(x.verdict).replace("_", " ")}`).join(", ");
      return {
        text: `${s(p.tool_name)}: ${s(p.verdict).replace("_", " ")}${would ? ` (shadow: ${would})` : ""}. ${argsLine(p.arguments as Payload, 90)}`,
        tone: p.verdict === "block" ? "danger" : p.verdict === "require_approval" || would ? "warn" : undefined
      };
    }
    case "policy.created":
    case "policy.updated":
    case "policy.deleted":
      return { text: `${s(p.name) || s(p.policy_id)}${p.mode ? ` (${s(p.mode)})` : ""}` };
    case "policy.defaults_seeded":
      return { text: `Default tool rules ${p.created ? "created" : "already present"}` };
    case "mcp.tool_succeeded":
      return { text: s(p.tool_name) };
    case "mcp.tool_failed":
      return { text: `${s(p.tool_name)}: ${s(p.error)}`, tone: "danger" };
    case "mcp.tools_discovered":
      return { text: `${s(p.count)} tools available` };
    case "approval.requested":
      return { text: `${s(p.tool_name) || s(p.kind)}: ${s(p.reason)}`, tone: "warn" };
    case "guard.rule_created":
    case "guard.rule_updated":
    case "guard.rule_deleted": {
      const spec = (p.spec ?? {}) as Payload;
      return { text: `${s(spec.name) || s(p.policy_id)} (v${s(p.version)}${spec.mode ? `, ${s(spec.mode)}` : ""})` };
    }
    case "guard.mode_changed":
      return { text: `${s(p.policy_id)}: ${s(p.from)} to ${s(p.to)}` };
    default: {
      const first = Object.entries(p).find(([, v]) => typeof v === "string");
      return { text: first ? `${first[0]}: ${first[1]}` : "" };
    }
  }
}

function LogRow({ event }: { event: AuditEvent }) {
  const [open, setOpen] = useState(false);
  const { text, tone } = summarize(event);
  return (
    <div className={`log${open ? " open" : ""}`}>
      <button type="button" className="log-line" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="log-time mono" title={event.created_at}>
          {clock(event.created_at)}
        </span>
        <span className="log-type">{LABELS[event.event_type] ?? event.event_type}</span>
        <span className={`log-text${tone ? ` text-${tone === "ok" ? "success" : tone}` : ""}`}>{text}</span>
        <span className="log-run mono subtle">{shortId(event.run_id)}</span>
      </button>
      {open && (
        <div className="log-detail">
          <p className="help mono">
            {event.event_type} · run {event.run_id ?? "none"} · conversation {event.conversation_id ?? "none"}
          </p>
          <pre className="code">{JSON.stringify(event.payload, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

export default function LogsPage() {
  const [logs, setLogs] = useState<AuditEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [category, setCategory] = useState<Category>("all");
  const [query, setQuery] = useState("");

  // ?q=<run reference> from a chat notice opens the log filtered to that run.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("q");
    if (q) setQuery(q);
  }, []);

  async function load() {
    setLogs(await apiGet<AuditEvent[]>("/api/logs?limit=300"));
  }

  useEffect(() => {
    load().catch((e) => setError(String(e)));
  }, []);
  useLiveRefresh(() => {
    load().catch(() => undefined);
  });

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (logs ?? []).filter((e) => {
      if (category !== "all" && categoryOf(e.event_type) !== category) return false;
      if (!q) return true;
      return `${e.event_type} ${LABELS[e.event_type] ?? ""} ${summarize(e).text} ${e.run_id ?? ""}`
        .toLowerCase()
        .includes(q);
    });
  }, [logs, category, query]);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Logs</h1>
          <p>Everything the agent, the guard and the tools did, newest first. Select a row for the full record.</p>
        </div>
      </header>

      <div className="toolbar">
        <label className="search">
          <MagnifyingGlass size={16} aria-hidden />
          <span className="sr-only">Search logs</span>
          <input
            className="input"
            placeholder="Search events, tools, run ids"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="seg" role="tablist" aria-label="Event type">
          {CATEGORIES.map((c) => (
            <button
              key={c.key}
              type="button"
              role="tab"
              aria-selected={category === c.key}
              className="seg-option"
              onClick={() => setCategory(c.key)}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="notice error">{error}</p>}

      {logs === null ? (
        <div className="surface surface-pad">
          <div className="skeleton" style={{ width: "60%" }} />
        </div>
      ) : shown.length === 0 ? (
        <div className="surface empty">
          <strong>{logs.length === 0 ? "No events yet" : "No events match"}</strong>
          <p>{logs.length === 0 ? "Send a chat message and its trail shows up here." : "Try another filter or search."}</p>
        </div>
      ) : (
        <div className="rows logs" aria-live="polite">
          {shown.map((event) => (
            <LogRow key={event.id} event={event} />
          ))}
        </div>
      )}
      {logs && logs.length > 0 && (
        <p className="subtle small">
          Showing {shown.length} of the latest {logs.length} events. Oldest shown {timeAgo(logs[logs.length - 1].created_at)}.
        </p>
      )}
    </div>
  );
}
