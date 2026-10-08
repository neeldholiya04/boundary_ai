"use client";

import { useEffect, useState } from "react";

import { ModeSwitch } from "@/components/mode-switch";
import { RuleEditor, STAGE_LABELS } from "@/components/rule-editor";
import { apiGet, apiSend } from "@/lib/api";
import {
  GuardMode,
  GuardPolicyStatus,
  GuardRule,
  GuardStage,
  GuardStat,
  GuardStats,
  GuardStatus
} from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const STAGE_ORDER: GuardStage[] = ["user_input", "tool_args", "tool_output", "final_output"];

const CHECK_LABELS: Record<string, string> = {
  keywords: "Keywords",
  pattern: "Pattern",
  topic: "Topic",
  llm_judge: "Plain-language policy",
  always: "Every call"
};

const ACTION_LABELS: Record<string, string> = {
  allow: "Allow",
  flag: "Log only",
  redact: "Redact",
  escalate: "Ask a human",
  block: "Block"
};

function pct(n: number | null | undefined): string {
  return n === null || n === undefined ? "n/a" : `${(n * 100).toFixed(0)}%`;
}

function ms(n: number | null | undefined): string {
  if (n === null || n === undefined) return "n/a";
  return `${n < 10 ? n.toFixed(1) : n.toFixed(0)} ms`;
}

function stages(list: string[]): string {
  return list.map((s) => STAGE_LABELS[s as GuardStage] ?? s).join(", ");
}

function Stats({ stat }: { stat: GuardStat | undefined }) {
  const actions = Object.entries(stat?.would_actions ?? {});
  return (
    <dl className="stat-row">
      <div>
        <dt>Checks</dt>
        <dd>{stat?.checks ?? 0}</dd>
      </div>
      <div>
        <dt>Would act</dt>
        <dd>
          {pct(stat?.would_fire_rate)}
          {actions.length > 0 && (
            <span className="muted"> ({actions.map(([a, c]) => `${ACTION_LABELS[a] ?? a} ${c}`).join(", ")})</span>
          )}
        </dd>
      </div>
      <div>
        <dt>p50</dt>
        <dd>{ms(stat?.p50_ms)}</dd>
      </div>
      <div>
        <dt>p99</dt>
        <dd>{ms(stat?.p99_ms)}</dd>
      </div>
      {stat?.errors ? (
        <div>
          <dt>Errors</dt>
          <dd className="text-danger">{stat.errors}</dd>
        </div>
      ) : null}
    </dl>
  );
}

export default function GuardrailsPage() {
  const [status, setStatus] = useState<GuardStatus | null>(null);
  const [stats, setStats] = useState<GuardStats | null>(null);
  const [rules, setRules] = useState<GuardRule[]>([]);
  // undefined: editor closed; null: a new rule; a rule: editing it.
  const [editing, setEditing] = useState<GuardRule | null | undefined>(undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null);

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

  useEffect(() => {
    load().catch((e) => setNote({ text: String(e), error: true }));
  }, []);
  useLiveRefresh(() => {
    load().catch(() => undefined);
  });

  async function act(key: string, work: () => Promise<string>) {
    setBusy(key);
    try {
      setNote({ text: await work() });
      await load();
    } catch (e) {
      setNote({ text: String(e instanceof Error ? e.message : e), error: true });
    } finally {
      setBusy(null);
    }
  }

  function setPolicyMode(policyId: string, mode: GuardMode) {
    return act(policyId, async () => {
      await apiSend(`/api/guard/policies/${policyId}`, { method: "PATCH", body: JSON.stringify({ mode }) });
      return `${policyId} is now ${mode}.`;
    });
  }

  function setRuleMode(rule: GuardRule, mode: GuardMode) {
    return act(rule.policy_id, async () => {
      await apiSend(`/api/guard/rules/${rule.id}`, { method: "PATCH", body: JSON.stringify({ mode }) });
      return `${rule.spec.name} is now ${mode}.`;
    });
  }

  function deleteRule(rule: GuardRule) {
    if (!window.confirm(`Delete the rule "${rule.spec.name}"? Its history stays in the audit log.`)) return;
    return act(rule.policy_id, async () => {
      await apiSend(`/api/guard/rules/${rule.id}`, { method: "DELETE" });
      return `Deleted ${rule.spec.name}.`;
    });
  }

  if (status && !status.enabled) {
    return (
      <div className="page">
        <header className="page-header">
          <h2>Guardrails</h2>
        </header>
        <section className="panel empty-state">
          <h3>The guard is off</h3>
          <p>Start the agent with a policy file (GUARD_POLICY_PATH, or `boundary serve --policy`) to check traffic.</p>
        </section>
      </div>
    );
  }

  const policies = status?.policies ?? [];
  const statByPolicy = new Map<string, GuardStat>((stats?.policies ?? []).map((s) => [s.policy_id, s]));
  const policyById = new Map<string, GuardPolicyStatus>(policies.map((p) => [p.id, p]));
  const builtIns = policies.filter((p) => p.origin === "file");
  const counts = { enforce: 0, shadow: 0, off: 0 };
  for (const p of policies) counts[p.mode] += 1;
  for (const r of rules) if (!policyById.has(r.policy_id) && r.spec.mode === "off") counts.off += 1;

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h2>Guardrails</h2>
          <p className="page-lede">
            Every request, tool call, tool result and answer is checked by these policies. Shadow logs what a
            policy would do; enforce acts on it.
          </p>
        </div>
        <div className="row">
          <button className="button secondary" onClick={() => load()}>
            Refresh
          </button>
          <button className="button" onClick={() => setEditing(null)}>
            New rule
          </button>
        </div>
      </header>

      <dl className="summary-strip" aria-label="Guard summary">
        <div>
          <dt>Enforced</dt>
          <dd>{counts.enforce}</dd>
        </div>
        <div>
          <dt>Shadow</dt>
          <dd>{counts.shadow}</dd>
        </div>
        <div>
          <dt>Off</dt>
          <dd>{counts.off}</dd>
        </div>
        <div>
          <dt>Your rules</dt>
          <dd>{rules.length}</dd>
        </div>
        <div>
          <dt>Config (policy file v{status?.version ?? "?"})</dt>
          <dd className="mono" title={status?.config_hash}>
            {status?.config_hash ?? "…"}
          </dd>
        </div>
        <div>
          <dt>Recent checks</dt>
          <dd>{stats?.sampled ?? 0}</dd>
        </div>
      </dl>

      {note && (
        <p className={`notice${note.error ? " error" : ""}`} role={note.error ? "alert" : "status"}>
          {note.text}
        </p>
      )}

      <section className="stack" aria-labelledby="rules-title">
        <div className="section-head">
          <h3 id="rules-title">Your rules</h3>
          <p className="muted">Written here, applied live. New rules start in shadow.</p>
        </div>
        {rules.length === 0 ? (
          <div className="empty-state panel">
            <p>
              No rules yet. A rule can block a topic, redact a codename, send a tool for approval, or apply a
              policy written in plain words.
            </p>
            <button className="button" onClick={() => setEditing(null)}>
              New rule
            </button>
          </div>
        ) : (
          <div className="list">
            {rules.map((rule) => {
              const running = policyById.get(rule.policy_id);
              const { spec } = rule;
              return (
                <article className={`policy-item${rule.error ? " broken" : ""}`} key={rule.id}>
                  <div className="policy-main">
                    <div className="policy-title">
                      <strong>{spec.name}</strong>
                      <span className="badge">v{rule.version}</span>
                      {rule.error && <span className="badge danger">Not running</span>}
                    </div>
                    {spec.description && <p className="policy-desc">{spec.description}</p>}
                    <div className="chips">
                      <span className="chip">{stages(spec.stages)}</span>
                      {spec.tools && <span className="chip mono">{spec.tools.join(", ")}</span>}
                      <span className="chip">{CHECK_LABELS[spec.check.type] ?? spec.check.type}</span>
                      <span className="chip">{ACTION_LABELS[spec.action] ?? spec.action}</span>
                      {spec.taints_run && <span className="chip">Taints the run</span>}
                    </div>
                    {rule.error && <p className="text-danger small">{rule.error}</p>}
                    {running && <Stats stat={statByPolicy.get(rule.policy_id)} />}
                  </div>
                  <div className="policy-side">
                    <ModeSwitch
                      value={spec.mode}
                      label={`Mode for ${spec.name}`}
                      busy={busy === rule.policy_id}
                      onChange={(mode) => setRuleMode(rule, mode)}
                    />
                    <div className="row">
                      <button className="button secondary small" onClick={() => setEditing(rule)}>
                        {rule.error ? "Fix" : "Edit"}
                      </button>
                      <button className="button secondary small" onClick={() => deleteRule(rule)}>
                        Delete
                      </button>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="stack" aria-labelledby="builtin-title">
        <div className="section-head">
          <h3 id="builtin-title">Built-in policies</h3>
          <p className="muted">
            From the reviewed policy file and measured in CI. They can be switched between modes, not removed.
          </p>
        </div>
        {STAGE_ORDER.map((stage) => {
          const here = builtIns.filter((p) => p.stages[0] === stage);
          if (here.length === 0) return null;
          return (
            <div className="stage-group" key={stage}>
              <h4>{STAGE_LABELS[stage]}</h4>
              <div className="list">
                {here.map((p) => (
                  <article className="policy-item" key={p.id}>
                    <div className="policy-main">
                      <div className="policy-title">
                        <strong className="mono">{p.id}</strong>
                      </div>
                      {p.description && <p className="policy-desc">{p.description}</p>}
                      <div className="chips">
                        {p.stages.length > 1 && <span className="chip">Also: {stages(p.stages.slice(1))}</span>}
                        <span className="chip mono">{p.detector}</span>
                        <span className="chip">{ACTION_LABELS[p.action] ?? p.action}</span>
                        {p.execution === "async" && <span className="chip">Async</span>}
                      </div>
                      <Stats stat={statByPolicy.get(p.id)} />
                    </div>
                    <div className="policy-side">
                      <ModeSwitch
                        value={p.mode}
                        label={`Mode for ${p.id}`}
                        busy={busy === p.id}
                        onChange={(mode) => setPolicyMode(p.id, mode)}
                      />
                    </div>
                  </article>
                ))}
              </div>
            </div>
          );
        })}
      </section>

      {editing !== undefined && (
        <RuleEditor
          rule={editing}
          onClose={() => setEditing(undefined)}
          onSaved={(message) => {
            setEditing(undefined);
            setNote({ text: message });
            load().catch(() => undefined);
          }}
        />
      )}
    </div>
  );
}
