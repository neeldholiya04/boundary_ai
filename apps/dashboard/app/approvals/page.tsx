"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { STAGE_NAMES, argsLine, timeAgo, timeLeft } from "@/lib/format";
import { Approval } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const STATUS_BADGE: Record<string, string> = {
  pending: "warn",
  approved: "ok",
  denied: "danger",
  expired: "danger",
  superseded: ""
};

function title(a: Approval): string {
  if (a.kind === "content_review") return `${STAGE_NAMES[a.stage ?? ""] ?? "Content"} held for review`;
  return `${a.tool_name} wants to run`;
}

function consequence(a: Approval): string {
  if (a.kind === "tool_call") return "Approve runs the tool call as shown. Deny stops it and ends the run.";
  if (a.stage === "tool_output") return "Approve passes this content to the agent. Deny withholds it; the run continues without it.";
  if (a.stage === "final_output") return "Approve sends this answer to the user. Deny withholds it.";
  return "Approve lets the request through to the agent. Deny ends the run.";
}

function Arguments({ args }: { args: Record<string, unknown> }) {
  const entries = Object.entries(args ?? {});
  if (entries.length === 0) return <p className="subtle small">No arguments.</p>;
  return (
    <dl className="kv">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd className="mono">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null);
  const [, setTick] = useState(0); // re-render every few seconds so "time left" stays true

  async function load() {
    setApprovals(await apiGet<Approval[]>("/api/approvals"));
  }

  useEffect(() => {
    load().catch((e) => setNote({ text: String(e), error: true }));
    const timer = window.setInterval(() => setTick((t) => t + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);
  useLiveRefresh(() => {
    load().catch(() => undefined);
  });

  async function decide(approval: Approval, decision: "approved" | "denied") {
    setBusy(approval.id);
    try {
      const response = await apiSend<{ status: string; assistant_message: string }>(
        `/api/approvals/${approval.id}/decision`,
        { method: "POST", body: JSON.stringify({ decision }) }
      );
      setNote({ text: `${decision === "approved" ? "Approved" : "Denied"}. The run is now ${response.status}.` });
      await load();
    } catch (e) {
      setNote({ text: String(e instanceof Error ? e.message : e), error: true });
    } finally {
      setBusy(null);
    }
  }

  const waiting = (approvals ?? []).filter((a) => a.status === "pending");
  const history = (approvals ?? []).filter((a) => a.status !== "pending").slice(0, 50);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Approvals</h1>
          <p>Tool calls and content the agent can&apos;t use until a person decides. Unanswered requests are denied when they expire.</p>
        </div>
      </header>

      {note && (
        <p className={`notice${note.error ? " error" : ""}`} role={note.error ? "alert" : "status"}>
          {note.text}
        </p>
      )}

      <section className="section" aria-labelledby="waiting-title">
        <div className="section-head">
          <h2 id="waiting-title">Waiting {waiting.length > 0 && <span className="badge warn">{waiting.length}</span>}</h2>
        </div>

        {approvals === null ? (
          <div className="surface surface-pad">
            <div className="skeleton" style={{ width: "40%" }} />
          </div>
        ) : waiting.length === 0 ? (
          <div className="surface empty">
            <strong>Nothing is waiting</strong>
            <p>When a rule or a tainted run needs a person, the request shows up here and in the sidebar.</p>
          </div>
        ) : (
          <div className="stack-list">
            {waiting.map((a) => (
              <article className="surface approval" key={a.id}>
                <div className="approval-head">
                  <div className="row-main">
                    <div className="row-title">
                      <span>{title(a)}</span>
                      {a.kind === "content_review" && <span className="badge">Content review</span>}
                    </div>
                    <p className="row-desc">{a.reason}</p>
                  </div>
                  <span className={`badge ${timeLeft(a.expires_at) === "expired" ? "danger" : "warn"}`}>
                    {timeLeft(a.expires_at)}
                  </span>
                </div>

                {a.kind === "content_review" ? (
                  <div className="field">
                    <span className="label">
                      {a.stage === "tool_output" ? `Result from ${a.tool_name}` : "Held content"}
                    </span>
                    {a.stage === "tool_output" && <p className="help mono">{argsLine(a.arguments)}</p>}
                    <pre className="code">{a.content}</pre>
                  </div>
                ) : (
                  <div className="field">
                    <span className="label">Arguments</span>
                    <Arguments args={a.arguments} />
                  </div>
                )}

                <div className="approval-foot">
                  <p className="help">{consequence(a)}</p>
                  <div className="actions">
                    <Link className="btn btn-ghost btn-sm" href={`/chat?c=${a.conversation_id}`}>
                      Open chat
                    </Link>
                    <button className="btn btn-danger" disabled={busy === a.id} onClick={() => decide(a, "denied")}>
                      Deny
                    </button>
                    <button className="btn btn-accent" disabled={busy === a.id} onClick={() => decide(a, "approved")}>
                      Approve
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="section" aria-labelledby="history-title">
        <div className="section-head">
          <h2 id="history-title">History</h2>
        </div>
        {history.length === 0 ? (
          <div className="surface empty">
            <p>Decisions you make, and requests that expire, are listed here.</p>
          </div>
        ) : (
          <div className="rows">
            {history.map((a) => (
              <div className="row" key={a.id}>
                <div className="row-main">
                  <div className="row-title">
                    <span>{title(a)}</span>
                  </div>
                  <p className="row-desc">{a.comment || a.reason}</p>
                </div>
                <div className="row-side">
                  <span className="subtle small">{timeAgo(a.created_at)}</span>
                  <span className={`badge ${STATUS_BADGE[a.status] ?? ""}`}>{a.status}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
