"use client";

import { useEffect, useState } from "react";

import { RuleEditor, STAGE_LABELS } from "@/components/rule-editor";
import { apiGet, apiSend } from "@/lib/api";
import { GuardRule, GuardStage, GuardStat, GuardStats, GuardStatus } from "@/lib/types";
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
  const [rules, setRules] = useState<GuardRule[]>([]);
  // undefined: editor closed; null: a new rule; a rule: editing it.
  const [editing, setEditing] = useState<GuardRule | null | undefined>(undefined);
  const [note, setNote] = useState<string | null>(null);

  async function load() {
    const [s, st, r] = await Promise.all([
      apiGet<GuardStatus>("/api/guard/status"),
      apiGet<GuardStats>("/api/guard/stats"),
      apiGet<GuardRule[]>("/api/guard/rules").catch(() => [] as GuardRule[])
    ]);
    setStatus(s);
    setStats(st);
    setRules(r);
  }

  async function setRuleMode(rule: GuardRule, mode: string) {
    try {
      await apiSend(`/api/guard/rules/${rule.id}`, { method: "PATCH", body: JSON.stringify({ mode }) });
      setNote(`${rule.policy_id} → ${mode}`);
      await load();
    } catch (e) {
      setNote(String(e));
    }
  }

  async function deleteRule(rule: GuardRule) {
    try {
      await apiSend(`/api/guard/rules/${rule.id}`, { method: "DELETE" });
      setNote(`Deleted ${rule.policy_id}`);
      await load();
    } catch (e) {
      setNote(String(e));
    }
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
  const ruleByPolicy = new Map<string, GuardRule>(rules.map((r) => [r.policy_id, r]));
  // Rules that aren't running (switched off, or failed to load) aren't in the guard's status, so
  // they'd vanish from the list: show them first, with their mode and any error.
  const idleRules = rules.filter((r) => !r.active);

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
          <div className="row">
            <button className="button secondary" onClick={() => load()}>
              Refresh
            </button>
            <button className="button" onClick={() => setEditing(null)}>
              New rule
            </button>
          </div>
        </div>
        <p className="muted">
          "Would fire" is how often each policy would have acted on recent traffic — check it in
          shadow before switching a policy to enforce.
        </p>
        <div className="list list-scroll">
          {idleRules.map((r) => (
            <article className="card" key={r.id}>
              <div className="row wrap" style={{ justifyContent: "space-between" }}>
                <div>
                  <strong>{r.spec.name}</strong>{" "}
                  <span className="badge origin-badge">{r.error ? "rule · failed to load" : "rule · off"}</span>
                  {r.error ? (
                    <p className="error">{r.error}</p>
                  ) : (
                    <p className="muted">Switched off: not checking anything.</p>
                  )}
                </div>
                <div className="row" role="group" aria-label={`mode for ${r.policy_id}`}>
                  {MODES.map((m) => (
                    <button
                      key={m}
                      className={`button ${r.spec.mode === m ? "" : "secondary"}`}
                      aria-pressed={r.spec.mode === m}
                      onClick={() => setRuleMode(r, m)}
                      disabled={r.spec.mode === m && !r.error}
                    >
                      {m}
                    </button>
                  ))}
                </div>
              </div>
              <div className="row">
                <button className="button secondary" onClick={() => setEditing(r)}>
                  {r.error ? "Fix" : "Edit"}
                </button>
                <button className="button secondary" onClick={() => deleteRule(r)}>
                  Delete
                </button>
              </div>
            </article>
          ))}
          {(status?.policies ?? []).map((p) => {
            const st = statByPolicy.get(p.id);
            const rule = ruleByPolicy.get(p.id);
            return (
              <article className="card" key={p.id}>
                <div className="row wrap" style={{ justifyContent: "space-between" }}>
                  <div>
                    <strong>{rule ? rule.spec.name : p.id}</strong>{" "}
                    <span className="badge origin-badge">{p.origin === "rule" ? `rule v${rule?.version ?? "?"}` : "built-in"}</span>
                    <p className="muted">
                      {p.stages.map((s) => STAGE_LABELS[s as GuardStage] ?? s).join(", ")}
                      {p.tools ? ` (${p.tools.join(", ")})` : ""} · {p.detector} · action {p.action} · {p.execution}
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
                {rule && (
                  <div className="row">
                    <button className="button secondary" onClick={() => setEditing(rule)}>
                      Edit
                    </button>
                    <button className="button secondary" onClick={() => deleteRule(rule)}>
                      Delete
                    </button>
                  </div>
                )}
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

      {editing !== undefined && (
        <RuleEditor
          rule={editing}
          onClose={() => setEditing(undefined)}
          onSaved={(message) => {
            setEditing(undefined);
            setNote(message);
            load().catch(() => undefined);
          }}
        />
      )}
    </div>
  );
}
