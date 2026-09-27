"use client";

import { useEffect, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { Approval } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [status, setStatus] = useState<string | null>(null);

  async function loadApprovals() {
    const data = await apiGet<Approval[]>("/api/approvals");
    setApprovals(data);
  }

  useEffect(() => {
    loadApprovals().catch((error) => setStatus(String(error)));
  }, []);

  useLiveRefresh(() => {
    loadApprovals().catch(() => undefined);
  });

  async function takeAction(approvalId: string, decision: "approved" | "denied") {
    try {
      const response = await apiSend<{ status: string; assistant_message: string }>(`/api/approvals/${approvalId}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision })
      });
      await loadApprovals();
      setStatus(`${response.status}: ${response.assistant_message}`);
    } catch (error) {
      setStatus(String(error));
    }
  }

  return (
    <div className="page page-fixed">
      <header className="page-header">
        <div>
          <h2>Approvals</h2>
          {status && <p className="page-status">{status}</p>}
        </div>
        <span className="badge warning">Queue</span>
      </header>

      <section className="panel stack panel-fill">
        <div className="row wrap" style={{ justifyContent: "space-between" }}>
          <h3>Approval Queue</h3>
          <button className="button secondary" onClick={() => loadApprovals()}>
            Refresh
          </button>
        </div>
        <div className="list list-scroll">
          {approvals.length === 0 && (
            <div className="empty-state">
              <p>No pending approvals.</p>
            </div>
          )}
          {approvals.map((approval) => (
            <div className="card" key={approval.id}>
              <div className="row wrap" style={{ justifyContent: "space-between" }}>
                <strong>
                  {approval.kind === "content_review"
                    ? `Content review: ${(approval.stage ?? "").replace("_", " ")}`
                    : approval.tool_name}
                </strong>
                <span
                  className={`badge ${
                    approval.status === "pending"
                      ? "warning"
                      : approval.status === "approved"
                        ? "success"
                        : approval.status === "denied" || approval.status === "expired" || approval.status === "superseded"
                          ? "danger"
                          : ""
                  }`}
                >
                  {approval.status}
                </span>
              </div>
              <p className="muted">
                Conversation <span className="mono">{approval.conversation_id}</span>
              </p>
              <p>{approval.reason}</p>
              {approval.kind === "content_review" ? (
                <>
                  {approval.stage === "tool_output" && (
                    <p className="muted">
                      From <span className="mono">{approval.tool_name}</span>{" "}
                      <span className="mono">{JSON.stringify(approval.arguments)}</span>
                    </p>
                  )}
                  <p className="muted">
                    Approving passes this content on; denying withholds it
                    {approval.stage === "tool_output" ? " and the run continues without it." : "."}
                  </p>
                  <pre>{approval.content}</pre>
                </>
              ) : (
                <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
              )}
              {approval.status === "pending" && (
                <div className="row wrap" style={{ marginTop: 12 }}>
                  <button className="button" onClick={() => takeAction(approval.id, "approved")}>
                    Approve
                  </button>
                  <button
                    className="button danger"
                    onClick={() => takeAction(approval.id, "denied")}
                  >
                    Deny
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
