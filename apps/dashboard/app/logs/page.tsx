"use client";

import { useEffect, useState } from "react";

import { apiGet } from "@/lib/api";
import { AuditEvent } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

function LogRow({ event }: { event: AuditEvent }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <article className="card log-row">
      <div className="log-row-top">
        <div className="log-row-copy">
          <strong>{event.event_type}</strong>
          <p className="muted">
            conversation: {event.conversation_id ?? "n/a"} / run: {event.run_id ?? "n/a"}
          </p>
        </div>
        <div className="log-row-actions">
          <span className="badge mono">{event.created_at}</span>
          <button
            type="button"
            className="button secondary"
            aria-expanded={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded ? "Hide details" : "Show details"}
          </button>
        </div>
      </div>
      {expanded && <pre>{JSON.stringify(event.payload, null, 2)}</pre>}
    </article>
  );
}

export default function LogsPage() {
  const [logs, setLogs] = useState<AuditEvent[]>([]);
  const [status, setStatus] = useState<string | null>(null);

  async function loadLogs() {
    const data = await apiGet<AuditEvent[]>("/api/logs?limit=150");
    setLogs(data);
  }

  useEffect(() => {
    loadLogs().catch((error) => setStatus(String(error)));
  }, []);

  useLiveRefresh(() => {
    loadLogs().catch(() => undefined);
  });

  return (
    <div className="page page-fixed">
      <header className="page-header">
        <div>
          <h2>Logs</h2>
          {status && <p className="page-status">{status}</p>}
        </div>
        <span className="badge">{logs.length} events</span>
      </header>

      <section className="panel stack panel-fill">
        <div className="row wrap" style={{ justifyContent: "space-between" }}>
          <h3>Recent Events</h3>
          <button className="button secondary" onClick={() => loadLogs()}>
            Refresh
          </button>
        </div>
        <div className="list list-scroll">
          {logs.length === 0 && (
            <div className="empty-state">
              <p>No log events yet.</p>
            </div>
          )}
          {logs.map((event) => (
            <LogRow key={event.id} event={event} />
          ))}
        </div>
      </section>
    </div>
  );
}
