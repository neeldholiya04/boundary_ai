"use client";

import { ChatText, Plus, Wrench } from "@phosphor-icons/react";
import { useEffect, useState } from "react";

import { ModeSwitch } from "@/components/mode-switch";
import { RuleEditor, STAGE_LABELS } from "@/components/rule-editor";
import { ToolRuleEditor, describePolicy } from "@/components/tool-rule-editor";
import { apiGet, apiSend } from "@/lib/api";
import {
  GuardMode,
  GuardPolicyStatus,
  GuardRule,
  GuardStage,
  GuardStat,
  GuardStats,
  GuardStatus,
  Policy
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
  escalate: "Ask a person",
  block: "Block"
};

function ms(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  return `${n < 10 ? n.toFixed(1) : n.toFixed(0)} ms`;
}

function stages(list: string[]): string {
  return list.map((s) => STAGE_LABELS[s as GuardStage] ?? s).join(", ");
}

/** "212 checks · would act on 3% · 4.1 ms p50", or nothing before the first check. */
function StatLine({ stat }: { stat: GuardStat | undefined }) {
  if (!stat || stat.checks === 0) return <span className="meta">No checks yet</span>;
  const rate = stat.would_fire_rate ?? 0;
  return (
    <span className="meta">
      {stat.checks} checks
      <span aria-hidden> · </span>
      <span className={rate > 0 ? "text-warn" : undefined}>would act on {(rate * 100).toFixed(0)}%</span>
      {stat.p50_ms !== null && stat.p50_ms !== undefined && (
        <>
          <span aria-hidden> · </span>
          {ms(stat.p50_ms)} p50
        </>
      )}
      {stat.errors ? (
        <>
          <span aria-hidden> · </span>
          <span className="text-danger">{stat.errors} errors</span>
        </>
      ) : null}
    </span>
  );
}

export default function GuardrailsPage() {
  const [status, setStatus] = useState<GuardStatus | null>(null);
  const [stats, setStats] = useState<GuardStats | null>(null);
  const [rules, setRules] = useState<GuardRule[] | null>(null);
  const [toolRules, setToolRules] = useState<Policy[] | null>(null);
  // What the editor shows: nothing, the "what does it check?" chooser, or one of the two editors
  // (rule null = a new one).
  const [editor, setEditor] = useState<
    | { kind: "choose" }
    | { kind: "text"; rule: GuardRule | null }
    | { kind: "tool"; rule: Policy | null }
    | null
  >(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null);

  async function load() {
    const [s, st, r, t] = await Promise.all([
      apiGet<GuardStatus>("/api/guard/status"),
      apiGet<GuardStats>("/api/guard/stats"),
      apiGet<GuardRule[]>("/api/guard/rules").catch(() => [] as GuardRule[]),
      apiGet<Policy[]>("/api/policies")
    ]);
    setStatus(s);
    setStats(st);
    setRules(r);
    setToolRules(t);
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

  function setToolMode(policy: Policy, mode: GuardMode) {
    return act(policy.id, async () => {
      await apiSend(`/api/policies/${policy.id}`, { method: "PATCH", body: JSON.stringify({ mode }) });
      return `${policy.name} is now ${mode}.`;
    });
  }

  function deleteToolRule(policy: Policy) {
    const note = policy.action?.default ? " It's a default rule and won't come back on restart." : "";
    if (!window.confirm(`Delete the rule "${policy.name}"?${note}`)) return;
    return act(policy.id, async () => {
      await apiSend(`/api/policies/${policy.id}`, { method: "DELETE" });
      return `Deleted ${policy.name}.`;
    });
  }

  function deleteRule(rule: GuardRule) {
    if (!window.confirm(`Delete the rule "${rule.spec.name}"? Its history stays in the logs.`)) return;
    return act(rule.policy_id, async () => {
      await apiSend(`/api/guard/rules/${rule.id}`, { method: "DELETE" });
      return `Deleted ${rule.spec.name}.`;
    });
  }

  function saved(message: string) {
    setEditor(null);
    setNote({ text: message });
    load().catch(() => undefined);
  }

  const policies = status?.policies ?? [];
  const statByPolicy = new Map<string, GuardStat>((stats?.policies ?? []).map((s) => [s.policy_id, s]));
  const policyById = new Map<string, GuardPolicyStatus>(policies.map((p) => [p.id, p]));
  const builtIns = policies.filter((p) => p.origin === "file");
  const counts = { enforce: 0, shadow: 0, off: 0 };
  for (const p of policies) counts[p.mode] += 1;
  for (const r of rules ?? []) if (!policyById.has(r.policy_id) && r.spec.mode === "off") counts.off += 1;
  for (const p of toolRules ?? []) counts[p.mode] += 1;
  const guardOff = status !== null && !status.enabled;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Guardrails</h1>
          <p>
            What the agent may read, say and do. Text rules check what is said at four points; tool rules decide each
            tool call. Shadow logs what a rule would do, enforce acts on it.
          </p>
        </div>
        <div className="actions">
          <button className="btn btn-primary" onClick={() => setEditor(guardOff ? { kind: "tool", rule: null } : { kind: "choose" })}>
            <Plus size={14} weight="bold" aria-hidden /> New rule
          </button>
        </div>
      </header>

      {guardOff ? (
        <div className="notice warn">
          The content guard is off. Start the agent with a policy file (<span className="mono">boundary serve --policy</span>)
          to check text. Tool rules still apply.
        </div>
      ) : (
        <dl className="stat-strip" aria-label="Guard summary">
          <div>
            <dt>Enforced</dt>
            <dd>{status ? counts.enforce : "–"}</dd>
          </div>
          <div>
            <dt>Shadow</dt>
            <dd className={counts.shadow ? "text-warn" : undefined}>{status ? counts.shadow : "–"}</dd>
          </div>
          <div>
            <dt>Off</dt>
            <dd>{status ? counts.off : "–"}</dd>
          </div>
          <div>
            <dt>Recent checks</dt>
            <dd>{stats?.sampled ?? "–"}</dd>
          </div>
          <div>
            <dt>Config v{status?.version ?? "–"}</dt>
            <dd className="mono small-dd" title={status?.config_hash}>
              {status?.config_hash?.slice(0, 12) ?? "–"}
            </dd>
          </div>
        </dl>
      )}

      {note && (
        <p className={`notice${note.error ? " error" : ""}`} role={note.error ? "alert" : "status"}>
          {note.text}
        </p>
      )}

      <section className="section" aria-labelledby="rules-title">
        <div className="section-head">
          <div>
            <h2 id="rules-title">Your rules</h2>
            <p>
              Text rules check what is said; tool rules decide tool calls. Both are applied live and can be watched in
              shadow before they act.
            </p>
          </div>
        </div>
        {rules === null || toolRules === null ? (
          <div className="surface surface-pad">
            <div className="skeleton" style={{ width: "50%" }} />
          </div>
        ) : rules.length + toolRules.length === 0 ? (
          <div className="surface empty">
            <strong>No rules yet</strong>
            <p>Block a topic, redact a codename, ask before a tool runs, or cap what a conversation can spend.</p>
            <button
              className="btn btn-primary btn-sm"
              onClick={() => setEditor(guardOff ? { kind: "tool", rule: null } : { kind: "choose" })}
            >
              New rule
            </button>
          </div>
        ) : (
          <div className="rows">
            {rules.length > 0 && [
              <div className="group-label" key="text-label">
                Text rules
              </div>,
              ...rules.map((rule) => {
                const running = policyById.get(rule.policy_id);
                const { spec } = rule;
                return (
                  <article className="row" key={rule.id}>
                    <div className="row-main">
                      <div className="row-title">
                        <span>{spec.name}</span>
                        {rule.error && <span className="badge danger">Not running</span>}
                      </div>
                      {spec.description && <p className="row-desc">{spec.description}</p>}
                      <p className="meta">
                        {stages(spec.stages)}
                        {spec.tools && ` (${spec.tools.join(", ")})`}
                        <span aria-hidden> · </span>
                        {CHECK_LABELS[spec.check.type] ?? spec.check.type}
                        <span aria-hidden> · </span>
                        {ACTION_LABELS[spec.action] ?? spec.action}
                        {spec.taints_run && " · taints the run"}
                        <span aria-hidden> · </span>v{rule.version}
                      </p>
                      {rule.error && <p className="text-danger small">{rule.error}</p>}
                      {running && <StatLine stat={statByPolicy.get(rule.policy_id)} />}
                    </div>
                    <div className="row-side">
                      <ModeSwitch
                        value={spec.mode}
                        label={`Mode for ${spec.name}`}
                        busy={busy === rule.policy_id}
                        onChange={(mode) => setRuleMode(rule, mode)}
                      />
                      <button className="btn btn-sm" onClick={() => setEditor({ kind: "text", rule })}>
                        {rule.error ? "Fix" : "Edit"}
                      </button>
                      <button className="btn btn-ghost btn-sm" onClick={() => deleteRule(rule)}>
                        Delete
                      </button>
                    </div>
                  </article>
                );
              })
            ]}
            {toolRules.length > 0 && [
              <div className="group-label" key="tool-label" id="tool-policies">
                Tool rules
              </div>,
              ...toolRules.map((p) => (
                <article className="row" key={p.id}>
                  <div className="row-main">
                    <div className="row-title">
                      <span>{p.name}</span>
                      {Boolean(p.action?.default) && <span className="badge">Default</span>}
                    </div>
                    <p className="row-desc">{describePolicy(p)}</p>
                    {typeof p.action?.reason === "string" && <p className="meta">Shown as: {p.action.reason}</p>}
                  </div>
                  <div className="row-side">
                    <ModeSwitch
                      value={p.mode}
                      label={`Mode for ${p.name}`}
                      busy={busy === p.id}
                      onChange={(mode) => setToolMode(p, mode)}
                    />
                    <button className="btn btn-sm" onClick={() => setEditor({ kind: "tool", rule: p })}>
                      Edit
                    </button>
                    <button className="btn btn-ghost btn-sm" onClick={() => deleteToolRule(p)}>
                      Delete
                    </button>
                  </div>
                </article>
              ))
            ]}
          </div>
        )}
      </section>

      {!guardOff && (
        <section className="section" aria-labelledby="builtin-title">
          <div className="section-head">
            <div>
              <h2 id="builtin-title">Built-in policies</h2>
              <p>From the reviewed policy file and measured in CI. You can change their mode, not remove them.</p>
            </div>
          </div>
          {status === null ? (
            <div className="surface surface-pad">
              <div className="skeleton" style={{ width: "50%" }} />
            </div>
          ) : (
            <div className="rows">
              {STAGE_ORDER.map((stage) => {
                const here = builtIns.filter((p) => p.stages[0] === stage);
                if (here.length === 0) return null;
                return [
                  <div className="group-label" key={`${stage}-label`}>
                    {STAGE_LABELS[stage]}
                  </div>,
                  ...here.map((p) => (
                    <article className="row" key={p.id}>
                      <div className="row-main">
                        <div className="row-title">
                          <span className="mono">{p.id}</span>
                          {p.execution === "async" && <span className="badge">Async</span>}
                        </div>
                        {p.description && <p className="row-desc">{p.description}</p>}
                        <p className="meta">
                          {ACTION_LABELS[p.action] ?? p.action}
                          {p.stages.length > 1 && ` · also ${stages(p.stages.slice(1))}`}
                          <span aria-hidden> · </span>
                          <span className="mono">{p.detector}</span>
                        </p>
                        <StatLine stat={statByPolicy.get(p.id)} />
                      </div>
                      <div className="row-side">
                        <ModeSwitch
                          value={p.mode}
                          label={`Mode for ${p.id}`}
                          busy={busy === p.id}
                          onChange={(mode) => setPolicyMode(p.id, mode)}
                        />
                      </div>
                    </article>
                  ))
                ];
              })}
            </div>
          )}
        </section>
      )}

      {editor?.kind === "choose" && (
        <NewRuleChooser
          onClose={() => setEditor(null)}
          onPick={(kind) => setEditor(kind === "text" ? { kind: "text", rule: null } : { kind: "tool", rule: null })}
        />
      )}
      {editor?.kind === "text" && (
        <RuleEditor
          rule={editor.rule}
          onClose={() => setEditor(null)}
          onBack={editor.rule ? undefined : () => setEditor({ kind: "choose" })}
          onSaved={saved}
        />
      )}
      {editor?.kind === "tool" && (
        <ToolRuleEditor
          policy={editor.rule}
          onClose={() => setEditor(null)}
          onBack={editor.rule || guardOff ? undefined : () => setEditor({ kind: "choose" })}
          onSaved={saved}
        />
      )}
    </div>
  );
}

/** First step of "New rule": what the rule looks at decides which engine runs it. */
function NewRuleChooser({ onClose, onPick }: { onClose: () => void; onPick: (kind: "text" | "tool") => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal narrow"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chooser-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div>
            <h2 id="chooser-title">New rule</h2>
            <p className="help">What should the rule look at?</p>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="modal-body">
          <div className="pick-list">
            <button type="button" className="pick" onClick={() => onPick("text")} autoFocus>
              <ChatText size={20} aria-hidden />
              <span>
                <strong>What is said</strong>
                <span className="help">
                  Requests, tool results or answers: keywords, patterns, a topic, or a policy in plain words. Can block,
                  redact or ask a person.
                </span>
              </span>
            </button>
            <button type="button" className="pick" onClick={() => onPick("tool")}>
              <Wrench size={20} aria-hidden />
              <span>
                <strong>What a tool call does</strong>
                <span className="help">
                  A tool and its arguments: ask before it runs, block it, keep paths in a folder, react to flagged runs,
                  or cap spend.
                </span>
              </span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
