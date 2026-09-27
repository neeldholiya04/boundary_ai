"use client";

import { useEffect, useState } from "react";

import { apiGet, apiSend } from "@/lib/api";
import { GuardStat, GuardStats, GuardStatus } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const MODES = ["off", "shadow", "enforce"] as const;

function pct(n: number | null): string {
  return n === null ? "–" : `${(n * 100).toFixed(0)}%`;
}

function ms(n: number | null): string {
  return n === null ? "–" : n < 10 ? n.toFixed(1) : n.toFixed(0);
}

export default function GuardrailsPage() {
  const [status, setStatus] = useState<GuardStatus | null>(null);
  const [stats, setStats] = useState<GuardStats | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function load() {
    const [s, st] = await Promise.all([
      apiGet<GuardStatus>("/api/guard/status"),
      apiGet<GuardStats>("/api/guard/stats")
    ]);
    setStatus(s);
    setStats(st);
  }

  useEffect(() => {
    load().catch((e) => setNote(String(e)));
  }, []);
  useLiveRefresh(() => {
    load().catch(() => undefined);
  });

  async function setMode(policyId: string, mode: string) {
    try {
      const r = await apiSend<{ config_hash: string }>(`/api/guard/policies/${policyId}`, {
        method: "PATCH",
        body: JSON.stringify({ mode })
      });
      setNote(`${policyId} → ${mode} (config ${r.config_hash})`);
      await load();
    } catch (e) {
      setNote(String(e));
    }
  }

  const statByPolicy = new Map<string, GuardStat>((stats?.policies ?? []).map((s) => [s.policy_id, s]));

  if (status && !status.enabled) {
    return (
      <div className="page">
        <header className="page-header">
          <h2>Guardrails</h2>
        </header>
        <p className="muted">The guard is not enabled (set GUARD_POLICY_PATH).</p>
      </div>
    );
  }

  return (
    <div className="page page-fixed">
      <header className="page-header">
        <div>
          <h2>Guardrails</h2>
          {note && <p className="page-status">{note}</p>}
        </div>
        <span className="badge mono">
          v{status?.version} · {status?.config_hash} · sampled {stats?.sampled ?? 0}
          {status?.dropped_async ? ` · dropped async ${status.dropped_async}` : ""}
        </span>
      </header>

      <section className="panel stack panel-fill">
        <div className="row wrap" style={{ justifyContent: "space-between" }}>
          <h3>Policies</h3>
          <button className="button secondary" onClick={() => load()}>
            Refresh
          </button>
        </div>
        <p className="muted">
          "Would fire" is how often each policy would have acted on recent traffic — check it in
          shadow before switching a policy to enforce.
        </p>
        <div className="list list-scroll">
          {(status?.policies ?? []).map((p) => {
            const st = statByPolicy.get(p.id);
            return (
              <article className="card" key={p.id}>
                <div className="row wrap" style={{ justifyContent: "space-between" }}>
                  <div>
                    <strong>{p.id}</strong>
                    <p className="muted">
                      {p.stages.join(", ")} · {p.detector} · action {p.action} · {p.execution}
                      {p.detects.length ? ` · detects ${p.detects.join(", ")}` : ""}
                    </p>
                  </div>
                  <div className="row" role="group" aria-label={`mode for ${p.id}`}>
                    {MODES.map((m) => (
                      <button
                        key={m}
                        className={`button ${p.mode === m ? "" : "secondary"}`}
                        aria-pressed={p.mode === m}
                        onClick={() => setMode(p.id, m)}
                        disabled={p.mode === m}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                </div>
                <p className="muted mono">
                  checks {st?.checks ?? 0} · would fire {pct(st?.would_fire_rate ?? null)}
                  {st && Object.keys(st.would_actions).length
                    ? ` (${Object.entries(st.would_actions)
                        .map(([a, c]) => `${a} ${c}`)
                        .join(", ")})`
                    : ""}
                  {" · "}p50 {ms(st?.p50_ms ?? null)}ms / p99 {ms(st?.p99_ms ?? null)}ms
                  {st?.errors ? ` · errors ${st.errors}` : ""}
                </p>
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}
